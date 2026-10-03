"""Teams tidy tools: an agent edits and deletes only what it posted itself.

Messages go out through the real ``send_message`` impl so the ownership
record is written as in production. Bot Framework is faked at the HTTP layer.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from cryptography.fernet import Fernet
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.teams._client import TeamsBotClient
from daimon.adapters.mcp.tools.teams._send import (  # pyright: ignore[reportPrivateUsage]
    _teams_send_message_impl,
)
from daimon.adapters.mcp.tools.teams._tidy import (  # pyright: ignore[reportPrivateUsage]
    _teams_delete_message_impl,
    _teams_edit_message_impl,
)
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.channel_tidy import content_hash, derive_content_key
from daimon.core.config import (
    AnthropicSettings,
    CryptoSettings,
    DatabaseSettings,
    McpSettings,
    Settings,
)
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.domain import Role
from daimon.core.stores.security_audit import SecurityAuditRow, list_events
from daimon.core.stores.turn_origins import create_origin
from daimon.testing import ma_agent
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import MARouter, build_fake_anthropic, list_response
from fastmcp.exceptions import ToolError
from pydantic import PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_ENTRA = "99999999-8888-7777-6666-555555555555"
_CALLER = "11111111-2222-3333-4444-555555555555"
_CHANNEL = "19:abc@thread.tacv2"
_THREAD = f"{_CHANNEL};messageid=1700000000000"
_BASE = "https://smba.trafficmanager.net/teams/v3/conversations"
_AGENT = "ag_acme"
_OTHER_AGENT = "ag_other"


class _Teams:
    """Bot Framework: every roster check passes; posts get ids act-1, act-2, ..."""

    def __init__(self) -> None:
        self.writes: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith("https://login.microsoftonline.com/"):
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        if "/members/" in url:
            return httpx.Response(200, json={"aadObjectId": _CALLER})
        self.writes.append(request)
        return httpx.Response(200, json={"id": f"act-{len(self.writes)}"})

    def edits_and_deletes(self) -> list[tuple[str, str]]:
        return [
            (r.method, str(r.url).removeprefix(_BASE))
            for r in self.writes
            if r.method in ("PUT", "DELETE")
        ]


@dataclass
class _World:
    runtime: McpRuntime
    teams: _Teams
    tenant_id: uuid.UUID
    account_id: uuid.UUID
    sessionmaker: async_sessionmaker[AsyncSession]
    content_key: bytes

    async def turn(self, *, agent: str = _AGENT) -> tuple[AuthIdentity, str]:
        now = datetime.now(UTC)
        async with self.sessionmaker.begin() as s:
            origin = await create_origin(
                s,
                tenant_id=self.tenant_id,
                account_id=self.account_id,
                platform="teams",
                parent_channel_id=_CHANNEL,
                thread_id=_THREAD,
                responder_ma_agent_id=agent,
                responder_name=agent,
                configuration_target_ma_agent_id=None,
                configuration_target_name=None,
                role=Role.USER,
                expires_at=now + timedelta(hours=1),
                now=now,
            )
        auth = AuthIdentity(
            account_id=self.account_id,
            tenant_id=self.tenant_id,
            role=Role.USER,
            platform="teams",
            external_id=_ENTRA,
            platform_user_id=_CALLER,
            chat_agent_id=derive_agent_uuid(tenant_id=self.tenant_id, ma_agent_id=agent),
        )
        return auth, str(origin.id)

    async def post(self, auth: AuthIdentity, content: str = "first draft") -> str:
        row = await _teams_send_message_impl(
            self.runtime, auth, channel_id=_THREAD, content=content
        )
        return row.activity_id

    async def audit(self) -> list[SecurityAuditRow]:
        async with self.sessionmaker() as s:
            return await list_events(s, tenant_id=self.tenant_id)


async def _world(sessionmaker: async_sessionmaker[AsyncSession]) -> _World:
    fernet_key = SecretStr(Fernet.generate_key().decode("ascii"))
    async with sessionmaker.begin() as s:
        tenant = await make_tenant(s, platform="teams", workspace_id=_ENTRA)
        account = await make_account(s, tenant=tenant)
    router = MARouter()
    agents: list[dict[str, Any]] = [
        ma_agent(
            id=agent_id,
            name=agent_id,
            metadata={"daimon_tenant": str(tenant.id), "daimon_name": agent_id},
        ).model_dump(mode="json")
        for agent_id in (_AGENT, _OTHER_AGENT)
    ]
    router.add("GET", r"/v1/agents", lambda _r, _m: list_response(agents))
    teams = _Teams()
    runtime = McpRuntime(
        session_factory=sessionmaker,
        client=build_fake_anthropic(router.dispatch),  # type: ignore[arg-type]
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
            anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
            crypto=CryptoSettings(keys=(fernet_key,)),
            mcp=McpSettings(),
        ),
        deployment_default=DeploymentDefault(),
        teams_client=TeamsBotClient(
            httpx.AsyncClient(transport=httpx.MockTransport(teams)),
            client_id="app-id",
            client_secret="secret",
            tenant_id=_ENTRA,
        ),
    )
    return _World(
        runtime,
        teams,
        tenant.id,
        account.id,
        sessionmaker,
        derive_content_key(fernet_key.get_secret_value()),
    )


async def test_an_agent_edits_then_deletes_its_own_teams_message(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Edit and delete reach the posted activity; each is audited with a hash, never text."""
    world = await _world(committing_sessionmaker)
    auth, origin = await world.turn()
    activity = await world.post(auth)

    edited = await _teams_edit_message_impl(
        world.runtime,
        auth,
        channel_id=_THREAD,
        message_id=activity,
        content="second draft",
        origin_context_id=origin,
    )
    deleted = await _teams_delete_message_impl(
        world.runtime, auth, channel_id=_THREAD, message_id=activity, origin_context_id=origin
    )

    assert (edited.action, deleted.action) == ("edited", "deleted"), "both actions report back"
    path = f"/{_THREAD}/activities/{activity}"
    assert world.teams.edits_and_deletes() == [("PUT", path), ("DELETE", path)], (
        "the edit replaces the posted activity in place, then the delete removes it"
    )
    put = next(r for r in world.teams.writes if r.method == "PUT")
    assert json.loads(put.content)["text"] == "second draft", "the edit carries the new text"
    rows = [r for r in await world.audit() if r.target_message_id == activity]
    assert [(r.tool_name, r.outcome) for r in rows] == [
        ("edit_message", "allowed"),
        ("delete_message", "allowed"),
    ], "each action writes one allowed audit row"
    assert rows[0].content_hmac == content_hash("first draft", world.content_key), (
        "the edit row records a hash of the text it replaced"
    )


async def test_teams_messages_not_posted_by_the_agent_are_refused_without_a_call(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A person's message has no record and another agent's is theirs: both are refused."""
    world = await _world(committing_sessionmaker)
    other, _ = await world.turn(agent=_OTHER_AGENT)
    auth, origin = await world.turn()
    theirs = await world.post(other)

    with pytest.raises(ToolError, match="not posted by you"):
        await _teams_delete_message_impl(
            world.runtime, auth, channel_id=_THREAD, message_id="act-99", origin_context_id=origin
        )
    with pytest.raises(ToolError, match="another agent posted this message"):
        await _teams_edit_message_impl(
            world.runtime,
            auth,
            channel_id=_THREAD,
            message_id=theirs,
            content="rewritten",
            origin_context_id=origin,
        )
    assert world.teams.edits_and_deletes() == [], "a refused call never reaches Teams"
    reasons = {r.reason for r in await world.audit() if r.outcome == "denied"}
    assert reasons == {"not_posted_by_agent", "other_agent"}, "each refusal is audited"


async def test_a_protected_teams_channel_refuses_tidying(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Protection set after the post still stops the edit: the policy is read at call time."""
    world = await _world(committing_sessionmaker)
    auth, origin = await world.turn()
    activity = await world.post(auth)
    async with committing_sessionmaker.begin() as s:
        await set_access_policy(
            s,
            tenant_id=world.tenant_id,
            policy=TenantAccessPolicy(protected_channel_ids=(_CHANNEL,)),
        )

    with pytest.raises(ToolError, match="this channel is protected"):
        await _teams_edit_message_impl(
            world.runtime,
            auth,
            channel_id=_THREAD,
            message_id=activity,
            content="second draft",
            origin_context_id=origin,
        )
    assert world.teams.edits_and_deletes() == [], "nothing reaches Teams"
