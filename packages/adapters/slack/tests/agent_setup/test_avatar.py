"""Slack picture reset, and the refusal a stale upload form gets."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from daimon.adapters.slack.agent_setup import avatar as avatar_module
from daimon.adapters.slack.agent_setup.avatar import (
    evaluate_avatar_submission,
    may_edit_avatar,
    reset_agent_avatar,
)
from daimon.adapters.slack.agent_setup.state import PanelMetadata, encode_panel_metadata
from daimon.core.agent_identity import CUSTOM_PICTURES_OFF
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.agent_avatars import get_avatar_by_token, get_or_create_avatar
from daimon.core.stores.security_audit import list_events
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_META = PanelMetadata(team_id="T1", channel_id="C1", view="avatar_upload", agent_name="Ada")


def _payload(files: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "view": {
            "id": "V_UPLOAD",
            "private_metadata": encode_panel_metadata(_META),
            "state": {
                "values": {
                    "agent_setup__avatar_file": {"agent_setup__avatar_file": {"files": files}}
                }
            },
        }
    }


def test_stale_upload_submission_gets_the_refusal() -> None:
    response = evaluate_avatar_submission(_payload([{"id": "F1", "size": 42}]))
    assert response is not None
    assert response["response_action"] == "update"
    assert CUSTOM_PICTURES_OFF == "Custom pictures are turned off."
    assert response["view"]["blocks"] == [
        {"type": "section", "text": {"type": "mrkdwn", "text": CUSTOM_PICTURES_OFF}}
    ]
    assert evaluate_avatar_submission({"view": {"private_metadata": "garbage"}}) is None


@pytest.mark.asyncio
async def test_avatar_edit_requires_admin_and_non_builtin_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = SimpleNamespace(
        anthropic=MagicMock(),
        deployment_default=DeploymentDefault(agent_name="Main"),
        settings=SimpleNamespace(agent_identity=SimpleNamespace(enabled=True)),
    )
    client = AsyncMock()
    admin = AsyncMock(return_value=False)
    lookup = AsyncMock(return_value=SimpleNamespace(name="Ada", metadata={}))
    monkeypatch.setattr(avatar_module, "resolve_is_admin", admin)
    monkeypatch.setattr(avatar_module, "find_agent_by_daimon_tag", lookup)
    tenant_id = uuid.uuid4()
    assert not await may_edit_avatar(
        runtime, client, tenant_id=tenant_id, team_id="T1", user_id="U", agent_name="Ada"
    )  # type: ignore[arg-type]
    lookup.assert_not_awaited()
    admin.return_value = True
    assert await may_edit_avatar(
        runtime, client, tenant_id=tenant_id, team_id="T1", user_id="U", agent_name="Ada"
    )  # type: ignore[arg-type]
    lookup.return_value = SimpleNamespace(name="Main", metadata={})
    assert not await may_edit_avatar(
        runtime, client, tenant_id=tenant_id, team_id="T1", user_id="U", agent_name="Main"
    )  # type: ignore[arg-type]
    runtime.settings.agent_identity.excluded_slack_team_ids = ["T1"]
    admin.reset_mock()
    assert not await may_edit_avatar(
        runtime, client, tenant_id=tenant_id, team_id="T1", user_id="U", agent_name="Ada"
    )  # type: ignore[arg-type]
    admin.assert_not_awaited()
    lookup.return_value = SimpleNamespace(name="Ada", metadata={})
    assert await may_edit_avatar(
        runtime, client, tenant_id=tenant_id, team_id="T2", user_id="U", agent_name="Ada"
    )  # type: ignore[arg-type]
    runtime.settings.agent_identity.enabled = False
    assert not await may_edit_avatar(
        runtime, client, tenant_id=tenant_id, team_id="T1", user_id="U", agent_name="Ada"
    )  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_reset_rotates_token_and_restores_default(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await make_tenant(db_session, platform="slack", workspace_id="T1")
    old = await get_or_create_avatar(db_session, tenant_id=tenant.id, agent_name="Ada")
    await db_session.commit()
    runtime = SimpleNamespace(sessionmaker=db_session_factory)
    client = AsyncMock()
    monkeypatch.setattr(avatar_module, "may_edit_avatar", AsyncMock(return_value=True))
    monkeypatch.setattr(avatar_module, "_refresh_details", AsyncMock())
    await reset_agent_avatar(
        runtime, client, meta=_META, team_id="T1", user_id="U_ADMIN", view_id="V1"
    )  # type: ignore[arg-type]
    async with db_session_factory() as session:
        new = await get_or_create_avatar(session, tenant_id=tenant.id, agent_name="Ada")
    assert new.token != old.token and new.source == "default"
    async with db_session_factory() as session:
        assert await get_avatar_by_token(session, token=old.token, sha12=old.sha256[:12]) is None
        events = await list_events(session, tenant_id=tenant.id)
    assert any(
        event.operation == "agent_avatar_reset" and event.agent_name == "Ada" for event in events
    )


@pytest.mark.asyncio
async def test_reset_rejects_metadata_from_another_team(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = AsyncMock(return_value=True)
    monkeypatch.setattr(avatar_module, "may_edit_avatar", gate)
    await reset_agent_avatar(
        SimpleNamespace(), AsyncMock(), meta=_META, team_id="OTHER", user_id="U", view_id="V1"
    )  # type: ignore[arg-type]
    gate.assert_not_awaited()
