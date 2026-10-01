"""Shared seeding and runtime helpers for the Slack credential-request tests.

`test_credential_forms.py`, `test_credential_requests.py` and
`test_credential_submissions.py` all drive the same surface from different
ends, so the workspace/request seeding, the runtime builder and the fake
Slack client readers live here rather than being spelled out three times.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import httpx
import yarl
from cryptography.fernet import Fernet
from daimon.adapters.slack.credential_requests import (
    ContinuationTrigger,
)
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.credential_requests import (
    mint_request_token,
)
from daimon.core.defaults.provisioning import derive_guild_account_uuid
from daimon.core.github_credentials import build_multifernet, encrypt_token
from daimon.core.stores.credential_requests import (
    create_credential_request,
)
from daimon.core.stores.slack_bot_tokens import upsert_slack_bot_token
from daimon.testing import build_fake_anthropic, list_response, make_fake_ma_handler
from daimon.testing.factories import make_account, make_tenant
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .harness import build_slack_runtime

_TEAM_ID = "T_CRED"
_USER_ID = "U_REQUESTER"
_CHANNEL_ID = "C_CRED"
_MESSAGE_TS = "1700000002.000200"

_EPHEMERAL_URL = yarl.URL("https://slack.com/api/chat.postEphemeral")
_VIEWS_OPEN_URL = yarl.URL("https://slack.com/api/views.open")
_CHAT_UPDATE_URL = yarl.URL("https://slack.com/api/chat.update")

_USERS_INFO_PATTERN = re.compile(r"https://slack\.com/api/users\.info.*")


def _override_users_info_admin(mock: Any) -> None:
    """Replace the conftest non-admin users.info stub with an admin one.

    aioresponses matches by insertion order and the conftest baseline is
    registered with repeat=True, so a plain append never wins — the existing
    users.info matchers have to be dropped first.
    """
    to_remove = [
        k
        for k, v in mock._matches.items()  # type: ignore[attr-defined]
        if getattr(v, "url_or_pattern", None) == _USERS_INFO_PATTERN
    ]
    for k in to_remove:
        del mock._matches[k]  # type: ignore[attr-defined]
    mock.get(  # pyright: ignore[reportUnknownMemberType]
        _USERS_INFO_PATTERN,
        payload={
            "ok": True,
            "user": {
                "id": _USER_ID,
                "name": "admin",
                "is_admin": True,
                "is_owner": False,
                "is_primary_owner": False,
            },
        },
        repeat=True,
    )


async def _seed_team(session: AsyncSession, *, team_id: str = _TEAM_ID) -> tuple[uuid.UUID, str]:
    fernet_key = Fernet.generate_key().decode()
    fernet = build_multifernet((fernet_key,))
    tenant = await make_tenant(session, platform="slack", workspace_id=team_id)
    # The guild account row the request rows point at: `set_binding` writes
    # `RepoAccessProof.account_id` with an FK to accounts.id, so any test that
    # reaches a binding write needs it to exist.
    await make_account(session, tenant=tenant, id=derive_guild_account_uuid(tenant_id=tenant.id))
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
    agent_id: uuid.UUID | None = None,
    posted_message_id: str | None = _MESSAGE_TS,
    origin_thread_id: str | None = None,
    requested_work: str | None = None,
    replaces_updated_at: datetime | None = None,
    target_ma_agent_id: str = "ag_test",
) -> str:
    token = mint_request_token()
    await create_credential_request(
        session,
        token=token,
        kind=kind,  # type: ignore[arg-type]
        tenant_id=tenant_id,
        agent_id=agent_id if agent_id is not None else uuid.uuid4(),
        account_id=derive_guild_account_uuid(tenant_id=tenant_id),
        target=target,
        mcp_server_url=mcp_server_url,
        requester_platform_user_id=requester,
        channel_id=_CHANNEL_ID,
        platform="slack",
        parent_channel_id=_CHANNEL_ID,
        origin_thread_id=origin_thread_id,
        posted_message_id=posted_message_id,
        expires_at=datetime.now(UTC) + expires_in,
        idempotency_key=uuid.uuid4(),
        target_ma_agent_id=target_ma_agent_id,
        target_name="tester",
        responder_name="Daimon",
        requested_work=requested_work,
        replaces_updated_at=replaces_updated_at,
    )
    await session.flush()
    return token


async def _noop_dispatch() -> None:
    """The continuation trigger for the tests that are not about dispatching."""


def _recording_trigger(
    seen: list[int], *, client: Any = None, raises: Exception | None = None
) -> ContinuationTrigger:
    """A trigger that records how many card edits had landed when it ran.

    Recording the edit count is what lets a test assert the ORDER — the
    receipt is on the card before the turn it unblocks is dispatched — and
    `raises` stages a dispatch that fails after a save that already committed.
    """

    async def _trigger() -> None:
        seen.append(len(_chat_updates(client)) if client is not None else 0)
        if raises is not None:
            raise raises

    return _trigger


def _build_runtime(
    fernet_key: str,
    db_factory: async_sessionmaker[AsyncSession],
    *,
    anthropic_handler: Any = None,
    mcp_configured: bool = False,
) -> SlackRuntime:
    settings = MagicMock()
    settings.mcp.public_url = "https://mcp.example.com/mcp" if mcp_configured else None
    settings.mcp.jwt_secret = SecretStr("x" * 32) if mcp_configured else None
    settings.github.oauth_scopes = ("repo",)
    return build_slack_runtime(
        fernet_key,
        db_factory,
        anthropic=build_fake_anthropic(anthropic_handler or make_fake_ma_handler()),
        settings=settings,
    )


def _ephemeral_texts(fake_slack_web_client: Any) -> list[str]:
    posts = fake_slack_web_client.mock.requests.get(("POST", _EPHEMERAL_URL), [])
    return [str((p.kwargs.get("json") or {}).get("text") or "") for p in posts]


def _agents_handler(live_agent: Any) -> Callable[[httpx.Request], httpx.Response]:
    """Serve one live MA agent on /v1/agents; every other MA call is a bug."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/agents":
            return list_response([live_agent.model_dump(mode="json")])
        raise AssertionError(f"Unexpected MA request: {request.method} {request.url.path}")

    return handler


def _chat_updates(fake_slack_web_client: Any) -> list[dict[str, Any]]:
    return [
        post.kwargs["json"]
        for post in fake_slack_web_client.mock.requests.get(("POST", _CHAT_UPDATE_URL), [])
    ]
