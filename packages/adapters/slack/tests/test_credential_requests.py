"""Tests for the Slack chat-initiated credential-request surface.

Behavioral assertions — grouped by unit:

Pure builders / evaluation:
  - build_credential_modal renders one required input per secret kind, the
    branch+token pair for the repo kind, and carries token/channel/message_ts
    through private_metadata under the kind's callback_id.
  - evaluate_credential_submission rejects an empty or oversized value with a
    response_action errors payload keyed to the input block, defaults a blank
    repo branch to main, and never copies the secret anywhere but the
    decision's own field.

handle_credential_request_click (real Postgres + FakeSlackWebClient):
  - A live request clicked by its requester opens the kind's modal.
  - Wrong requester / expired / already-used / unknown token each answer
    with an ephemeral and never open a modal.
  - The repo kind refuses a non-admin whose agent cannot be resolved
    (fail closed) and lets a workspace admin through before any MA read.

run_* submissions:
  - env: consume + agent_files write commit together; the button message is
    updated in place and the requester gets an ephemeral.
  - A second submission of a consumed row writes nothing.
  - mcp: missing daimon-mcp configuration refuses BEFORE the consume.
  - repo: non-admin refused before the consume; admin + public repo binds.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
import yarl
from cryptography.fernet import Fernet
from daimon.adapters.slack.credential_requests import (
    CRED_CALLBACK_PREFIX,
    build_credential_modal,
    evaluate_credential_submission,
    handle_credential_request_click,
    run_env_credential_submission,
    run_mcp_credential_submission,
    run_repo_bind_credential_submission,
)
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.credential_requests import mint_request_token
from daimon.core.defaults.provisioning import derive_guild_account_uuid
from daimon.core.github_credentials import build_multifernet, encrypt_token
from daimon.core.stores.credential_requests import (
    create_credential_request,
    peek_credential_request,
)
from daimon.core.stores.slack_bot_tokens import upsert_slack_bot_token
from daimon.testing.factories import make_tenant
from daimon.testing.ma import build_fake_anthropic, make_fake_ma_handler
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_TEAM_ID = "T_CRED"
_USER_ID = "U_REQUESTER"
_CHANNEL_ID = "C_CRED"
_MESSAGE_TS = "1700000002.000200"

_EPHEMERAL_URL = yarl.URL("https://slack.com/api/chat.postEphemeral")
_VIEWS_OPEN_URL = yarl.URL("https://slack.com/api/views.open")
_CHAT_UPDATE_URL = yarl.URL("https://slack.com/api/chat.update")

# ---------------------------------------------------------------------------
# build_credential_modal
# ---------------------------------------------------------------------------


def _modal(kind: str, *, target: str = "OPENAI_API_KEY") -> dict[str, Any]:
    return build_credential_modal(
        kind=kind,  # type: ignore[arg-type]
        token="tok_test",
        channel_id=_CHANNEL_ID,
        message_ts=_MESSAGE_TS,
        target=target,
    )


@pytest.mark.parametrize("kind", ["env", "mcp", "skill_repo"])
def test_secret_kinds_render_one_required_input(kind: str) -> None:
    view = _modal(kind)
    assert view["callback_id"] == f"{CRED_CALLBACK_PREFIX}{kind}"
    inputs = [b for b in view["blocks"] if b["type"] == "input"]
    assert len(inputs) == 1
    assert not inputs[0].get("optional", False)


def test_repo_kind_renders_branch_and_optional_token() -> None:
    view = _modal("repo", target="https://github.com/owner/repo")
    assert view["callback_id"] == f"{CRED_CALLBACK_PREFIX}repo"
    inputs = [b for b in view["blocks"] if b["type"] == "input"]
    assert len(inputs) == 2
    branch, pat = inputs
    assert branch["element"]["initial_value"] == "main"
    assert pat["optional"] is True


def test_modal_metadata_carries_token_channel_and_message_ts() -> None:
    view = _modal("env")
    meta = json.loads(view["private_metadata"])
    assert meta["token"] == "tok_test"
    assert meta["channel_id"] == _CHANNEL_ID
    assert meta["message_ts"] == _MESSAGE_TS


def test_modal_metadata_never_carries_the_target_secret_field() -> None:
    """The modal is built before any secret exists — nothing but routing
    handles may appear in private_metadata, ever."""
    view = _modal("env")
    meta = json.loads(view["private_metadata"])
    assert set(meta) <= {"token", "channel_id", "message_ts"}


# ---------------------------------------------------------------------------
# evaluate_credential_submission
# ---------------------------------------------------------------------------


def _submission(kind: str, values: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "view_submission",
        "team": {"id": _TEAM_ID},
        "user": {"id": _USER_ID},
        "view": {
            "callback_id": f"{CRED_CALLBACK_PREFIX}{kind}",
            "private_metadata": json.dumps(
                {"token": "tok_test", "channel_id": _CHANNEL_ID, "message_ts": _MESSAGE_TS},
                separators=(",", ":"),
            ),
            "state": {"values": values},
        },
    }


def _value_input(value: str) -> dict[str, Any]:
    return {
        "credential__value": {"credential__value": {"type": "plain_text_input", "value": value}}
    }


def test_empty_value_is_rejected_with_field_error() -> None:
    decision = evaluate_credential_submission(_submission("env", _value_input("   ")))
    assert decision.proceed is False
    assert decision.response_payload is not None
    assert decision.response_payload["response_action"] == "errors"
    assert "credential__value" in decision.response_payload["errors"]


def test_oversized_value_is_rejected_with_field_error() -> None:
    decision = evaluate_credential_submission(
        _submission("env", _value_input("é" * 3000))  # 6000 bytes, 3000 chars
    )
    assert decision.proceed is False
    assert decision.response_payload is not None
    assert "credential__value" in decision.response_payload["errors"]


def test_valid_value_proceeds_and_carries_routing_fields() -> None:
    decision = evaluate_credential_submission(_submission("env", _value_input("s3cr3t")))
    assert decision.proceed is True
    assert decision.response_payload is None
    assert decision.kind == "env"
    assert decision.value == "s3cr3t"
    assert decision.token == "tok_test"
    assert decision.channel_id == _CHANNEL_ID
    assert decision.message_ts == _MESSAGE_TS


def test_repo_submission_defaults_blank_branch_to_main() -> None:
    values = {
        "credential__branch": {"credential__branch": {"type": "plain_text_input", "value": "  "}},
        "credential__pat": {"credential__pat": {"type": "plain_text_input", "value": None}},
    }
    decision = evaluate_credential_submission(_submission("repo", values))
    assert decision.proceed is True
    assert decision.branch == "main"
    assert decision.value == ""


# ---------------------------------------------------------------------------
# handle_credential_request_click
# ---------------------------------------------------------------------------


async def _seed_team(session: AsyncSession, *, team_id: str = _TEAM_ID) -> tuple[uuid.UUID, str]:
    fernet_key = Fernet.generate_key().decode()
    fernet = build_multifernet((fernet_key,))
    tenant = await make_tenant(session, platform="slack", workspace_id=team_id)
    await upsert_slack_bot_token(
        session, team_id=team_id, encrypted_token=encrypt_token(fernet, "xoxb-test")
    )
    await session.flush()
    return tenant.id, fernet_key


async def _seed_request(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    kind: str = "env",
    target: str = "OPENAI_API_KEY",
    requester: str = _USER_ID,
    expires_in: timedelta = timedelta(minutes=30),
    mcp_server_url: str | None = None,
) -> str:
    token = mint_request_token()
    await create_credential_request(
        session,
        token=token,
        kind=kind,  # type: ignore[arg-type]
        tenant_id=tenant_id,
        agent_id=uuid.uuid4(),
        account_id=derive_guild_account_uuid(tenant_id=tenant_id),
        target=target,
        mcp_server_url=mcp_server_url,
        requester_platform_user_id=requester,
        channel_id=_CHANNEL_ID,
        expires_at=datetime.now(UTC) + expires_in,
    )
    await session.flush()
    return token


def _build_runtime(fernet_key: str, db_factory: async_sessionmaker[AsyncSession]) -> SlackRuntime:
    settings = MagicMock()
    settings.crypto.keys = (SecretStr(fernet_key),)
    settings.mcp.public_url = None
    settings.mcp.jwt_secret = None
    return SlackRuntime(
        settings=settings,
        anthropic=build_fake_anthropic(make_fake_ma_handler()),
        sessionmaker=db_factory,
        billing_config=None,
        http_client=MagicMock(spec=httpx.AsyncClient),
        resolver_cache=MagicMock(),  # pyright: ignore[reportArgumentType]  # stub, turn path not exercised
        turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]  # stub, turn path not exercised
    )


def _click_payload(token: str, *, user_id: str = _USER_ID) -> dict[str, Any]:
    return {
        "type": "block_actions",
        "team": {"id": _TEAM_ID},
        "user": {"id": user_id},
        "channel": {"id": _CHANNEL_ID},
        "container": {"message_ts": _MESSAGE_TS},
        "message": {"ts": _MESSAGE_TS},
        "trigger_id": "TRIGGER_TEST",
        "actions": [{"action_id": "credential_request", "value": token}],
    }


@pytest.mark.asyncio
async def test_live_request_clicked_by_requester_opens_the_kind_modal(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    tenant_id, fernet_key = await _seed_team(db_session)
    token = await _seed_request(db_session, tenant_id=tenant_id, kind="env")
    await db_session.commit()
    runtime = _build_runtime(fernet_key, db_session_factory)

    await handle_credential_request_click(runtime, _click_payload(token))

    opens = fake_slack_web_client.mock.requests.get(("POST", _VIEWS_OPEN_URL), [])
    assert len(opens) == 1, "a live request clicked by its requester must open the modal"
    view = opens[0].kwargs["json"]["view"]
    assert view["callback_id"] == f"{CRED_CALLBACK_PREFIX}env"
    meta = json.loads(view["private_metadata"])
    assert meta["token"] == token


@pytest.mark.asyncio
async def test_wrong_requester_gets_ephemeral_and_no_modal(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    tenant_id, fernet_key = await _seed_team(db_session)
    token = await _seed_request(db_session, tenant_id=tenant_id, requester="U_SOMEONE_ELSE")
    await db_session.commit()
    runtime = _build_runtime(fernet_key, db_session_factory)

    await handle_credential_request_click(runtime, _click_payload(token))

    assert ("POST", _VIEWS_OPEN_URL) not in fake_slack_web_client.mock.requests
    assert ("POST", _EPHEMERAL_URL) in fake_slack_web_client.mock.requests


@pytest.mark.asyncio
async def test_expired_request_gets_ephemeral_and_no_modal(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    tenant_id, fernet_key = await _seed_team(db_session)
    token = await _seed_request(db_session, tenant_id=tenant_id, expires_in=timedelta(minutes=-1))
    await db_session.commit()
    runtime = _build_runtime(fernet_key, db_session_factory)

    await handle_credential_request_click(runtime, _click_payload(token))

    assert ("POST", _VIEWS_OPEN_URL) not in fake_slack_web_client.mock.requests
    assert ("POST", _EPHEMERAL_URL) in fake_slack_web_client.mock.requests


@pytest.mark.asyncio
async def test_unknown_token_gets_ephemeral_and_no_modal(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    _tenant_id, fernet_key = await _seed_team(db_session)
    await db_session.commit()
    runtime = _build_runtime(fernet_key, db_session_factory)

    await handle_credential_request_click(runtime, _click_payload("never-minted"))

    assert ("POST", _VIEWS_OPEN_URL) not in fake_slack_web_client.mock.requests
    assert ("POST", _EPHEMERAL_URL) in fake_slack_web_client.mock.requests


@pytest.mark.asyncio
async def test_repo_kind_refuses_non_admin_when_agent_cannot_be_resolved(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """Fail closed: the row's derived agent uuid resolving to nothing means
    the agent was archived or deleted since the mint — a non-admin must not
    reach the modal."""
    tenant_id, fernet_key = await _seed_team(db_session)
    token = await _seed_request(
        db_session, tenant_id=tenant_id, kind="repo", target="https://github.com/o/r"
    )
    await db_session.commit()
    runtime = _build_runtime(fernet_key, db_session_factory)

    await handle_credential_request_click(runtime, _click_payload(token))

    assert ("POST", _VIEWS_OPEN_URL) not in fake_slack_web_client.mock.requests
    assert ("POST", _EPHEMERAL_URL) in fake_slack_web_client.mock.requests


# ---------------------------------------------------------------------------
# run_env_credential_submission
# ---------------------------------------------------------------------------


async def _agent_file_rows(session: AsyncSession) -> list[Any]:
    result = await session.execute(text("SELECT key, content FROM agent_files ORDER BY key"))
    return list(result.mappings())


@pytest.mark.asyncio
async def test_env_submission_consumes_row_and_writes_the_secret(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    tenant_id, fernet_key = await _seed_team(db_session)
    token = await _seed_request(db_session, tenant_id=tenant_id, kind="env")
    await db_session.commit()
    runtime = _build_runtime(fernet_key, db_session_factory)

    await run_env_credential_submission(
        runtime,
        team_id=_TEAM_ID,
        user_id=_USER_ID,
        channel_id=_CHANNEL_ID,
        message_ts=_MESSAGE_TS,
        token=token,
        value="s3cr3t-value",
    )

    async with db_session_factory() as s:
        rows = await _agent_file_rows(s)
        row = await peek_credential_request(s, token=token)
    assert rows and rows[0]["key"] == "OPENAI_API_KEY"
    assert rows[0]["content"] == "s3cr3t-value"
    assert row is not None and row.used_at is not None, "the consume must have committed"
    assert ("POST", _CHAT_UPDATE_URL) in fake_slack_web_client.mock.requests, (
        "the button message must be swapped for a consumed marker"
    )
    assert ("POST", _EPHEMERAL_URL) in fake_slack_web_client.mock.requests


@pytest.mark.asyncio
async def test_env_submission_of_consumed_row_writes_nothing(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    tenant_id, fernet_key = await _seed_team(db_session)
    token = await _seed_request(db_session, tenant_id=tenant_id, kind="env")
    await db_session.commit()
    runtime = _build_runtime(fernet_key, db_session_factory)

    common: dict[str, Any] = {
        "team_id": _TEAM_ID,
        "user_id": _USER_ID,
        "channel_id": _CHANNEL_ID,
        "message_ts": _MESSAGE_TS,
        "token": token,
    }
    await run_env_credential_submission(runtime, value="first", **common)
    await run_env_credential_submission(runtime, value="second", **common)

    async with db_session_factory() as s:
        rows = await _agent_file_rows(s)
    assert len(rows) == 1 and rows[0]["content"] == "first", (
        "a consumed row must never produce a second write"
    )


# ---------------------------------------------------------------------------
# run_mcp_credential_submission
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_submission_with_unconfigured_mcp_refuses_before_the_consume(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    tenant_id, fernet_key = await _seed_team(db_session)
    token = await _seed_request(
        db_session,
        tenant_id=tenant_id,
        kind="mcp",
        target="my-server",
        mcp_server_url="https://mcp.example.com",
    )
    await db_session.commit()
    runtime = _build_runtime(fernet_key, db_session_factory)  # mcp settings are None

    await run_mcp_credential_submission(
        runtime,
        team_id=_TEAM_ID,
        user_id=_USER_ID,
        channel_id=_CHANNEL_ID,
        message_ts=_MESSAGE_TS,
        token=token,
        value="mcp-token",
    )

    async with db_session_factory() as s:
        row = await peek_credential_request(s, token=token)
    assert row is not None and row.used_at is None, (
        "a config refusal must land before the consume so the request survives"
    )
    assert ("POST", _EPHEMERAL_URL) in fake_slack_web_client.mock.requests


# ---------------------------------------------------------------------------
# run_repo_bind_credential_submission
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repo_submission_refuses_non_admin_before_the_consume(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    tenant_id, fernet_key = await _seed_team(db_session)
    token = await _seed_request(
        db_session, tenant_id=tenant_id, kind="repo", target="https://github.com/o/r"
    )
    await db_session.commit()
    runtime = _build_runtime(fernet_key, db_session_factory)

    await run_repo_bind_credential_submission(
        runtime,
        team_id=_TEAM_ID,
        user_id=_USER_ID,
        channel_id=_CHANNEL_ID,
        message_ts=_MESSAGE_TS,
        token=token,
        value="",
        branch="main",
    )

    async with db_session_factory() as s:
        row = await peek_credential_request(s, token=token)
    assert row is not None and row.used_at is None, (
        "the admin gate is the authorization boundary and must precede the consume"
    )
    assert ("POST", _EPHEMERAL_URL) in fake_slack_web_client.mock.requests
    assert ("POST", _CHAT_UPDATE_URL) not in fake_slack_web_client.mock.requests
