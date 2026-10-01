"""Tests for the Details view's "Use from your coding tools" flow.

What matters here is what the click produces and what it refuses: a token
shown exactly once and never written to a log line, a member's click that
mints nothing, a revoke that only its minter can run, and a deployment that
cannot mint saying so instead of failing quietly.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import httpx
import structlog.testing
import yarl
from aioresponses import aioresponses as AioResponsesMock
from daimon.adapters.slack.agent_setup.coding_tools import (
    NOT_CONFIGURED_MESSAGE,
    TOKEN_REVOKED_MESSAGE,
    handle_coding_tools_click,
    handle_revoke_token_click,
    render_coding_tools_message,
)
from daimon.adapters.slack.agent_setup.panel_views import ACTION_REVOKE_TOKEN
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_auth import mint_agent_mcp_token
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.mcp_tokens import get_mcp_token
from daimon.testing.factories import make_tenant
from daimon.testing.ma import MARouter, build_fake_anthropic
from daimon.testing.ma_models import ma_agent
from pydantic import SecretStr
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_TEAM_ID = "T_CODING_TOOLS"
_USER_ID = "U_CODING_TOOLS"
_CHANNEL_ID = "C_CODING_TOOLS"
_AGENT_NAME = "analyst"
_MA_AGENT_ID = f"agent_{'c' * 24}"
_PUBLIC_URL = "https://daimon.test/mcp"
_RESPONSE_URL = "https://hooks.slack.test/actions/response"

_SLACK_API_BASE = "https://slack.com/api"
_USERS_INFO_PATTERN = re.compile(r"https://slack\.com/api/users\.info.*")
_EPHEMERAL_KEY = ("POST", yarl.URL(f"{_SLACK_API_BASE}/chat.postEphemeral"))


def _users_info_payload(*, is_admin: bool) -> dict[str, Any]:
    return {
        "ok": True,
        "user": {
            "id": _USER_ID,
            "name": "admin" if is_admin else "member",
            "is_admin": is_admin,
            "is_owner": False,
            "is_primary_owner": False,
        },
    }


class _RecordingHttpClient:
    """Stands in for the runtime's httpx client on the response_url path."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, Any]]] = []

    async def post(self, url: str, *, json: dict[str, Any]) -> httpx.Response:
        self.posts.append((url, json))
        return httpx.Response(200, text="ok")


def _build_runtime(
    db_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    configured: bool = True,
    http_client: Any = None,
) -> SlackRuntime:
    router = MARouter()
    router.add_agent_list(ma_agent(id=_MA_AGENT_ID, name=_AGENT_NAME, tenant_id=tenant_id))
    settings = MagicMock()
    settings.mcp.public_url = _PUBLIC_URL if configured else None
    settings.mcp.jwt_secret = SecretStr("jwt-secret") if configured else None
    return SlackRuntime(
        settings=settings,
        anthropic=build_fake_anthropic(router.dispatch),
        sessionmaker=db_factory,
        billing_config=None,
        http_client=http_client or MagicMock(spec=httpx.AsyncClient),
        resolver_cache=MagicMock(),  # pyright: ignore[reportArgumentType]  # stub, turn path not exercised
        turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]  # stub, turn path not exercised
        deployment_default=DeploymentDefault(),
    )


def _ephemerals(mock: AioResponsesMock) -> list[dict[str, Any]]:
    return [dict(kwargs.get("json") or {}) for _, kwargs in mock.requests.get(_EPHEMERAL_KEY, [])]


def test_render_coding_tools_message_carries_both_artifacts_and_a_revoke_button() -> None:
    """One tap copies the CLI line; another copies the `.mcp.json` block."""
    text, blocks = render_coding_tools_message(
        agent_name=_AGENT_NAME, public_url=_PUBLIC_URL, jwt="jwt-value", jti=uuid.uuid4()
    )

    rendered = "\n".join(
        str(block.get("text", {}).get("text", "")) for block in blocks if block["type"] == "section"
    )
    assert f'claude mcp add --transport http "daimon-{_AGENT_NAME}"' in rendered, (
        "the one-liner names the agent-scoped server"
    )
    assert "Authorization: Bearer jwt-value" in rendered, "the header carries the minted token"
    assert ACTION_REVOKE_TOKEN in json.dumps(blocks), (
        "the token can be revoked from where it is shown"
    )
    assert "jwt-value" not in text, (
        "the notification preview must not leak the token outside the message body"
    )


async def test_coding_tools_click_posts_the_one_liner_and_never_logs_the_token(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An admin's click mints once, shows the token once, and logs only its jti."""
    tenant = await make_tenant(db_session, platform="slack", workspace_id=_TEAM_ID)
    await db_session.commit()
    runtime = _build_runtime(db_session_factory, tenant_id=tenant.id)

    with AioResponsesMock() as mock:
        mock.get(_USERS_INFO_PATTERN, payload=_users_info_payload(is_admin=True), repeat=True)  # pyright: ignore[reportUnknownMemberType]
        mock.post(f"{_SLACK_API_BASE}/chat.postEphemeral", payload={"ok": True}, repeat=True)  # pyright: ignore[reportUnknownMemberType]
        with structlog.testing.capture_logs() as logs:
            await handle_coding_tools_click(
                runtime,
                AsyncWebClient(token="xoxb-test"),
                team_id=_TEAM_ID,
                tenant_id=tenant.id,
                agent_name=_AGENT_NAME,
                channel_id=_CHANNEL_ID,
                user_id=_USER_ID,
                trigger_id="TRIG",
            )

        posted = _ephemerals(mock)

    assert len(posted) == 1, "one click, one ephemeral"
    rendered = json.dumps(posted[0]["blocks"])
    assert "claude mcp add --transport http" in rendered, "the copyable one-liner is the payload"
    assert ACTION_REVOKE_TOKEN in rendered, "the revoke button travels with the token"

    token = rendered.split("Authorization: Bearer ")[1].split("\\")[0].split(" ")[0]
    assert token, "the message carries a signed token"
    assert token not in json.dumps(logs), "the token value must never reach a log line"
    assert any(entry.get("event") == "slack.coding_tools.minted" for entry in logs), (
        "the mint is recorded by its jti"
    )


async def test_coding_tools_click_by_a_member_refuses_and_mints_nothing(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The button is visible to everyone; the mint is not. Hiding is not gating."""
    tenant = await make_tenant(db_session, platform="slack", workspace_id=_TEAM_ID)
    await db_session.commit()
    runtime = _build_runtime(db_session_factory, tenant_id=tenant.id)

    with AioResponsesMock() as mock:
        mock.get(_USERS_INFO_PATTERN, payload=_users_info_payload(is_admin=False), repeat=True)  # pyright: ignore[reportUnknownMemberType]
        mock.post(f"{_SLACK_API_BASE}/chat.postEphemeral", payload={"ok": True}, repeat=True)  # pyright: ignore[reportUnknownMemberType]
        await handle_coding_tools_click(
            runtime,
            AsyncWebClient(token="xoxb-test"),
            team_id=_TEAM_ID,
            tenant_id=tenant.id,
            agent_name=_AGENT_NAME,
            channel_id=_CHANNEL_ID,
            user_id=_USER_ID,
            trigger_id="TRIG",
        )
        posted = _ephemerals(mock)

    assert len(posted) == 1, "a refused click is explained, never silent"
    assert "workspace admin" in posted[0]["text"], "the refusal names who can do it"
    assert "Bearer" not in json.dumps(posted[0]), "a refused click mints nothing"


async def test_coding_tools_click_when_unconfigured_posts_a_note(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Without an MCP URL and signing key there is nothing to hand out."""
    tenant = await make_tenant(db_session, platform="slack", workspace_id=_TEAM_ID)
    await db_session.commit()
    runtime = _build_runtime(db_session_factory, tenant_id=tenant.id, configured=False)

    with AioResponsesMock() as mock:
        mock.get(_USERS_INFO_PATTERN, payload=_users_info_payload(is_admin=True), repeat=True)  # pyright: ignore[reportUnknownMemberType]
        mock.post(f"{_SLACK_API_BASE}/chat.postEphemeral", payload={"ok": True}, repeat=True)  # pyright: ignore[reportUnknownMemberType]
        await handle_coding_tools_click(
            runtime,
            AsyncWebClient(token="xoxb-test"),
            team_id=_TEAM_ID,
            tenant_id=tenant.id,
            agent_name=_AGENT_NAME,
            channel_id=_CHANNEL_ID,
            user_id=_USER_ID,
            trigger_id="TRIG",
        )
        posted = _ephemerals(mock)

    assert [entry["text"] for entry in posted] == [NOT_CONFIGURED_MESSAGE], (
        "an unconfigured deployment says so, once"
    )


async def _mint_for(
    db_factory: async_sessionmaker[AsyncSession], *, tenant_id: uuid.UUID, user_id: str
) -> uuid.UUID:
    """Mint a token as `user_id` and return its jti."""
    async with db_factory() as session, session.begin():
        principal = await get_or_create_platform_principal(
            session, platform="slack", external_id=user_id, tenant_id=tenant_id
        )
        account_id = principal.account_id
    async with db_factory() as session, session.begin():
        token = await mint_agent_mcp_token(
            session,
            account_id=account_id,
            tenant_id=tenant_id,
            agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=_MA_AGENT_ID),
            label=_AGENT_NAME,
            secret=b"jwt-secret",
            now=datetime.now(UTC),
        )
    import jwt as pyjwt

    claims: dict[str, Any] = pyjwt.decode(token, options={"verify_signature": False})
    return uuid.UUID(str(claims["jti"]))


async def test_revoke_replaces_the_original_message_and_kills_the_token(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Revoking answers on the message the button lives on, and the row is dead."""
    tenant = await make_tenant(db_session, platform="slack", workspace_id=_TEAM_ID)
    await db_session.commit()
    jti = await _mint_for(db_session_factory, tenant_id=tenant.id, user_id=_USER_ID)
    http_client = _RecordingHttpClient()
    runtime = _build_runtime(db_session_factory, tenant_id=tenant.id, http_client=http_client)

    await handle_revoke_token_click(
        runtime,
        AsyncWebClient(token="xoxb-test"),
        tenant_id=tenant.id,
        jti=jti,
        user_id=_USER_ID,
        response_url=_RESPONSE_URL,
    )

    assert http_client.posts == [
        (_RESPONSE_URL, {"replace_original": True, "text": TOKEN_REVOKED_MESSAGE})
    ], "the revoked token's message is replaced where it stands"
    async with db_session_factory() as session:
        row = await get_mcp_token(session, jti=jti)
    assert row is not None and row.revoked_at is not None, "the registry row is revoked"


async def test_revoke_by_someone_other_than_the_minter_is_refused(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Only the account that minted a token can revoke it."""
    tenant = await make_tenant(db_session, platform="slack", workspace_id=_TEAM_ID)
    await db_session.commit()
    jti = await _mint_for(db_session_factory, tenant_id=tenant.id, user_id="U_MINTER")
    http_client = _RecordingHttpClient()
    runtime = _build_runtime(db_session_factory, tenant_id=tenant.id, http_client=http_client)

    await handle_revoke_token_click(
        runtime,
        AsyncWebClient(token="xoxb-test"),
        tenant_id=tenant.id,
        jti=jti,
        user_id="U_SOMEONE_ELSE",
        response_url=_RESPONSE_URL,
    )

    assert len(http_client.posts) == 1, "the refused click is answered"
    _url, body = http_client.posts[0]
    assert body["replace_original"] is False, "someone else's message is not replaced"
    assert "minted" in body["text"], "the refusal says who may revoke"
    async with db_session_factory() as session:
        row = await get_mcp_token(session, jti=jti)
    assert row is not None and row.revoked_at is None, "the token is still live"
