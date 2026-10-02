"""Operator tokens on Who answers where: admins mint (shown once) and revoke; members can't."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from daimon.adapters.slack.agent_setup import operator_tokens
from daimon.adapters.slack.agent_setup.operator_tokens import (
    OperatorTokenSubmission,
    evaluate_operator_token_submission,
    handle_operator_token_revoke,
    run_operator_token_submission,
)
from daimon.adapters.slack.agent_setup.panel_views import (
    OPERATOR_LABEL_INPUT_ID,
    OPERATOR_SCOPES_INPUT_ID,
    build_operator_token_form,
)
from daimon.adapters.slack.agent_setup.state import PanelMetadata, encode_panel_metadata
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.mcp_tokens import list_mcp_tokens
from daimon.core.stores.security_audit import list_events
from daimon.testing.factories import make_tenant
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

TEAM, USER = "T0OPS", "U0ADMIN"
META = PanelMetadata(team_id=TEAM, channel_id="C0GEN", view="operator_token", root_view_id="V1")


def _payload(scopes: list[str], label: str = "ci") -> dict[str, Any]:
    values = {
        OPERATOR_SCOPES_INPUT_ID: {
            OPERATOR_SCOPES_INPUT_ID: {"selected_options": [{"value": s} for s in scopes]}
        },
        OPERATOR_LABEL_INPUT_ID: {OPERATOR_LABEL_INPUT_ID: {"value": label}},
    }
    return {"view": {"private_metadata": encode_panel_metadata(META), "state": {"values": values}}}


def _runtime(factory: async_sessionmaker[AsyncSession]) -> MagicMock:
    runtime = MagicMock()
    runtime.sessionmaker = factory
    runtime.settings.mcp.jwt_secret = SecretStr("jwt-secret")
    return runtime


def _client() -> MagicMock:
    client = MagicMock()
    client.chat_postEphemeral = AsyncMock()
    client.views_update = AsyncMock()
    return client


async def _setup(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch, *, admin: bool
) -> None:
    async with factory.begin() as session:
        await make_tenant(session, platform="slack", workspace_id=TEAM)
    monkeypatch.setattr(operator_tokens, "resolve_is_admin", AsyncMock(return_value=admin))
    monkeypatch.setattr(operator_tokens, "load_routing_view", AsyncMock(return_value={"v": 1}))


async def _state(factory: async_sessionmaker[AsyncSession]) -> tuple[list[Any], list[Any]]:
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=TEAM)
    async with factory() as session:
        tokens = await list_mcp_tokens(session, now=dt.datetime.now(dt.UTC), tenant_id=tenant_id)
        events = await list_events(session, tenant_id=tenant_id)
    return tokens, [(e.tool_name, e.outcome) for e in events]


def test_the_form_offers_only_tenant_scopes_and_reads_back_the_pick() -> None:
    form = build_operator_token_form(meta=META)
    offered = [o["value"] for o in form["blocks"][0]["element"]["options"]]
    assert offered == ["tenant:read", "channels:write", "promo:redeem"], "never promo:create"
    submission = evaluate_operator_token_submission(_payload(["tenant:read"]))
    assert submission == OperatorTokenSubmission(meta=META, scopes=("tenant:read",), label="ci")


async def test_an_admin_mints_a_token_shown_once_then_revokes_it(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    await _setup(db_session_factory, monkeypatch, admin=True)
    client = _client()
    submission = evaluate_operator_token_submission(_payload(["tenant:read", "promo:redeem"]))
    assert submission is not None
    await run_operator_token_submission(
        _runtime(db_session_factory), client, team_id=TEAM, user_id=USER, submission=submission
    )
    (token,), _ = await _state(db_session_factory)
    assert set(token.scopes) == {"tenant:read", "promo:redeem"} and token.kind == "operator"
    shown = client.chat_postEphemeral.call_args.kwargs
    assert shown["user"] == USER and "one time" in shown["text"], "shown once, to the minter"
    client.views_update.assert_awaited_once_with(view_id="V1", view={"v": 1})

    await handle_operator_token_revoke(
        _runtime(db_session_factory),
        _client(),
        meta=META,
        user_id=USER,
        is_admin=True,
        jti=token.jti,
        view_id="V1",
    )
    tokens, events = await _state(db_session_factory)
    assert tokens == [], "the revoked token is no longer live"
    assert events == [
        ("panel:operator_token_mint", "allowed"),
        ("panel:operator_token_revoke", "allowed"),
    ]


async def test_a_member_mints_nothing_and_revokes_nothing(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    await _setup(db_session_factory, monkeypatch, admin=False)
    submission = evaluate_operator_token_submission(_payload(["tenant:read"]))
    assert submission is not None
    await run_operator_token_submission(
        _runtime(db_session_factory), _client(), team_id=TEAM, user_id=USER, submission=submission
    )
    await handle_operator_token_revoke(
        _runtime(db_session_factory),
        _client(),
        meta=META,
        user_id=USER,
        is_admin=False,
        jti=uuid.uuid4(),
        view_id="V1",
    )
    tokens, events = await _state(db_session_factory)
    assert tokens == []
    assert events == [
        ("panel:operator_token_mint", "denied"),
        ("panel:operator_token_revoke", "denied"),
    ], "both refusals are audited"


async def test_a_forged_deployment_scope_is_refused(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    await _setup(db_session_factory, monkeypatch, admin=True)
    client = _client()
    submission = evaluate_operator_token_submission(_payload(["promo:create"]))
    assert submission is not None
    await run_operator_token_submission(
        _runtime(db_session_factory), client, team_id=TEAM, user_id=USER, submission=submission
    )
    tokens, _ = await _state(db_session_factory)
    assert (
        tokens == [] and "mint-operator-token" in client.chat_postEphemeral.call_args.kwargs["text"]
    )
