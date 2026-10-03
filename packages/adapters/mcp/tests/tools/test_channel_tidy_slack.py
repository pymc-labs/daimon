"""Slack tidy tools: an agent edits and deletes only what it posted itself.

Messages go out through the real ``send_message``/``create_thread`` impls so
the ownership record is written as in production. Slack is faked at the HTTP
layer with ``aioresponses``.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from aioresponses import aioresponses
from cryptography.fernet import Fernet
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools import _tidy as tidy_module
from daimon.adapters.mcp.tools._channel_policy import ChannelReadRefused
from daimon.adapters.mcp.tools.slack._send import (  # pyright: ignore[reportPrivateUsage]
    _slack_create_thread_impl,
    _slack_send_message_impl,
)
from daimon.adapters.mcp.tools.slack._tidy import (  # pyright: ignore[reportPrivateUsage]
    _slack_delete_message_impl,
    _slack_delete_thread_impl,
    _slack_edit_message_impl,
)
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.channel_tidy import content_hash, derive_content_key
from daimon.core.config import (
    AnthropicSettings,
    CryptoSettings,
    DatabaseSettings,
    McpSettings,
    Settings,
    SupportSettings,
)
from daimon.core.github_credentials import build_multifernet, encrypt_token
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.domain import Role
from daimon.core.stores.security_audit import SecurityAuditRow, list_events
from daimon.core.stores.slack_bot_tokens import upsert_slack_bot_token
from daimon.core.stores.turn_origins import create_origin
from daimon.testing import ma_agent
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import MARouter, build_fake_anthropic, list_response
from fastmcp.exceptions import ToolError
from pydantic import PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from yarl import URL

_TEAM = "T_TIDY"
_CHANNEL = "C1"
_CALLER = "U_CALLER"
_AGENT = "ag_acme"
_OTHER_AGENT = "ag_other"

_CONVERSATIONS_INFO = re.compile(r"https://slack\.com/api/conversations\.info.*")
_CONVERSATIONS_REPLIES = re.compile(r"https://slack\.com/api/conversations\.replies.*")
_USERS_INFO = re.compile(r"https://slack\.com/api/users\.info.*")
_CHAT_POST = "https://slack.com/api/chat.postMessage"
_CHAT_UPDATE = "https://slack.com/api/chat.update"
_CHAT_DELETE = re.compile(r"https://slack\.com/api/chat\.delete.*")


@dataclass
class _World:
    runtime: McpRuntime
    tenant_id: uuid.UUID
    account_id: uuid.UUID
    sessionmaker: async_sessionmaker[AsyncSession]
    content_key: bytes

    async def turn(
        self, *, agent: str = _AGENT, parent: str = _CHANNEL
    ) -> tuple[AuthIdentity, str]:
        now = datetime.now(UTC)
        async with self.sessionmaker.begin() as s:
            origin = await create_origin(
                s,
                tenant_id=self.tenant_id,
                account_id=self.account_id,
                platform="slack",
                parent_channel_id=parent,
                thread_id="1700000000.000001",
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
            platform="slack",
            external_id=_TEAM,
            platform_user_id=_CALLER,
            chat_agent_id=derive_agent_uuid(tenant_id=self.tenant_id, ma_agent_id=agent),
        )
        return auth, str(origin.id)

    async def audit(self) -> list[SecurityAuditRow]:
        async with self.sessionmaker() as s:
            return await list_events(s, tenant_id=self.tenant_id)

    async def set_policy(self, policy: TenantAccessPolicy) -> None:
        async with self.sessionmaker.begin() as s:
            await set_access_policy(s, tenant_id=self.tenant_id, policy=policy)


async def _world(
    sessionmaker: async_sessionmaker[AsyncSession], *, escalation_channel_id: str | None = None
) -> _World:
    fernet_key = SecretStr(Fernet.generate_key().decode("ascii"))
    fernet = build_multifernet((fernet_key.get_secret_value(),))
    async with sessionmaker.begin() as s:
        tenant = await make_tenant(s, platform="slack", workspace_id=_TEAM)
        account = await make_account(s, tenant=tenant)
        await upsert_slack_bot_token(
            s, team_id=_TEAM, encrypted_token=encrypt_token(fernet, "xoxb-secret")
        )
    settings = Settings(
        database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
        anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
        crypto=CryptoSettings(keys=(fernet_key,)),
        mcp=McpSettings(),
        support=SupportSettings(slack_escalation_channel_id=escalation_channel_id),
    )
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
    runtime = McpRuntime(
        session_factory=sessionmaker,
        client=build_fake_anthropic(router.dispatch),  # type: ignore[arg-type]
        settings=settings,
        deployment_default=DeploymentDefault(),
        fernet=fernet,
    )
    return _World(
        runtime,
        tenant.id,
        account.id,
        sessionmaker,
        derive_content_key(fernet_key.get_secret_value()),
    )


def _channel_access(m: aioresponses, *, times: int = 10) -> None:
    for _ in range(times):
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _CONVERSATIONS_INFO,
            payload={"ok": True, "channel": {"id": _CHANNEL, "name": "g", "is_private": False}},
        )
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _USERS_INFO, payload={"ok": True, "user": {"id": _CALLER, "is_restricted": False}}
        )


async def _post(
    world: _World, auth: AuthIdentity, m: aioresponses, ts: str, *, to: str = _CHANNEL
) -> str:
    m.post(_CHAT_POST, payload={"ok": True, "ts": ts})  # pyright: ignore[reportUnknownMemberType]
    if ":" in to:
        m.get(_CONVERSATIONS_REPLIES, payload={"ok": True, "messages": []})  # pyright: ignore[reportUnknownMemberType]
    await _slack_send_message_impl(
        world.runtime,
        auth,
        channel_id=to,
        content="first draft",
        attachments=None,
        file_handles=None,
    )
    return ts


def _calls(m: aioresponses, url: str) -> list[dict[str, Any]]:
    return [call.kwargs["json"] for call in m.requests.get(("POST", URL(url)), [])]


def _deletes(m: aioresponses) -> list[dict[str, Any]]:
    """chat.delete goes out as query parameters, one request key per message."""
    return [
        dict(call.kwargs["params"])
        for (method, url), calls in m.requests.items()
        if method == "POST" and url.path == "/api/chat.delete"
        for call in calls
    ]


async def test_an_agent_edits_then_deletes_its_own_message_and_each_is_audited_without_text(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(committing_sessionmaker)
    auth, origin = await world.turn()
    with aioresponses() as m:
        _channel_access(m)
        ts = await _post(world, auth, m, "1700000001.000100")
        m.post(_CHAT_UPDATE, payload={"ok": True, "ts": ts})  # pyright: ignore[reportUnknownMemberType]
        m.post(_CHAT_DELETE, payload={"ok": True, "ts": ts})  # pyright: ignore[reportUnknownMemberType]
        edited = await _slack_edit_message_impl(
            world.runtime,
            auth,
            channel_id=_CHANNEL,
            message_id=ts,
            content="second draft",
            origin_context_id=origin,
        )
        deleted = await _slack_delete_message_impl(
            world.runtime, auth, channel_id=_CHANNEL, message_id=ts, origin_context_id=origin
        )
        update_body = _calls(m, _CHAT_UPDATE)[0]
        delete_body = _deletes(m)[0]

    assert (edited.action, deleted.action) == ("edited", "deleted"), "both actions report back"
    assert update_body["ts"] == ts and update_body["blocks"] == [
        {"type": "markdown", "text": "second draft"}
    ], "chat.update carries the new text in one markdown block"
    assert delete_body == {"channel": _CHANNEL, "ts": ts}, "chat.delete targets the one message"
    rows = [r for r in await world.audit() if r.target_message_id == ts]
    assert [(r.tool_name, r.outcome) for r in rows] == [
        ("edit_message", "allowed"),
        ("delete_message", "allowed"),
    ], "each action writes one allowed audit row"
    assert rows[0].content_hmac == content_hash("first draft", world.content_key), (
        "the edit row records a hash of the text it replaced"
    )
    for row in rows:
        dumped = row.model_dump_json()
        assert "first draft" not in dumped and "second draft" not in dumped, (
            "an audit row never carries message text"
        )


async def test_human_and_other_agent_messages_are_refused_without_a_slack_call(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(committing_sessionmaker)
    other_auth, _ = await world.turn(agent=_OTHER_AGENT)
    auth, origin = await world.turn()
    with aioresponses() as m:
        _channel_access(m)
        theirs = await _post(world, other_auth, m, "1700000002.000100")
        with pytest.raises(ToolError, match="not posted by you"):
            await _slack_delete_message_impl(
                world.runtime,
                auth,
                channel_id=_CHANNEL,
                message_id="1700000009.000100",  # a person's message: no record
                origin_context_id=origin,
            )
        with pytest.raises(ToolError, match="another agent posted this message"):
            await _slack_edit_message_impl(
                world.runtime,
                auth,
                channel_id=_CHANNEL,
                message_id=theirs,
                content="rewritten",
                origin_context_id=origin,
            )
        assert not _deletes(m) and not _calls(m, _CHAT_UPDATE), "a refused call never reaches Slack"
    reasons = {r.reason for r in await world.audit() if r.outcome == "denied"}
    assert reasons == {"not_posted_by_agent", "other_agent"}, "each refusal is audited"


async def test_the_slack_escalation_channel_is_never_tidied(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(committing_sessionmaker, escalation_channel_id=_CHANNEL)
    auth, origin = await world.turn()
    with aioresponses() as m:
        _channel_access(m)
        ts = await _post(world, auth, m, "1700000003.000100")
        with pytest.raises(ToolError, match="support-escalation channel"):
            await _slack_delete_message_impl(
                world.runtime, auth, channel_id=_CHANNEL, message_id=ts, origin_context_id=origin
            )
        assert not _deletes(m), "nothing was deleted"


async def test_protected_and_sealed_channels_refuse_tidying(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(committing_sessionmaker)
    auth, _ = await world.turn()
    outside_auth, outside = await world.turn(parent="C_ELSEWHERE")
    with aioresponses() as m:
        _channel_access(m)
        ts = await _post(world, auth, m, "1700000004.000100")
        async with committing_sessionmaker.begin() as s:
            await set_access_policy(
                s,
                tenant_id=world.tenant_id,
                policy=TenantAccessPolicy(sealed_channel_ids=(_CHANNEL,)),
            )
        with pytest.raises(ChannelReadRefused):
            await _slack_delete_message_impl(
                world.runtime,
                outside_auth,
                channel_id=_CHANNEL,
                message_id=ts,
                origin_context_id=outside,
            )
        async with committing_sessionmaker.begin() as s:
            await set_access_policy(
                s,
                tenant_id=world.tenant_id,
                policy=TenantAccessPolicy(protected_channel_ids=(_CHANNEL,)),
            )
        _, origin = await world.turn()
        with pytest.raises(ToolError, match="writers to none"):
            await _slack_delete_message_impl(
                world.runtime, auth, channel_id=_CHANNEL, message_id=ts, origin_context_id=origin
            )
        assert not _deletes(m), "nothing was deleted"


async def test_delete_thread_needs_every_message_to_be_the_agents_own(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(committing_sessionmaker)
    auth, origin = await world.turn()
    root = "1700000005.000100"
    with aioresponses() as m:
        _channel_access(m)
        m.post(_CHAT_POST, payload={"ok": True, "ts": root})  # pyright: ignore[reportUnknownMemberType]
        await _slack_create_thread_impl(world.runtime, auth, channel_id=_CHANNEL, content="root")
        reply = await _post(world, auth, m, "1700000005.000200", to=f"{_CHANNEL}:{root}")
        human = {"ts": "1700000005.000300", "user": _CALLER}
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _CONVERSATIONS_REPLIES,
            payload={"ok": True, "messages": [{"ts": root}, {"ts": reply}, human]},
        )
        with pytest.raises(ToolError, match="replies that are not yours"):
            await _slack_delete_thread_impl(
                world.runtime, auth, thread_id=f"{_CHANNEL}:{root}", origin_context_id=origin
            )
        assert not _deletes(m), "a thread with a person's reply survives"

        m.get(  # pyright: ignore[reportUnknownMemberType]
            _CONVERSATIONS_REPLIES,
            payload={"ok": True, "messages": [{"ts": root}, {"ts": reply}]},
        )
        m.post(_CHAT_DELETE, payload={"ok": True}, repeat=True)  # pyright: ignore[reportUnknownMemberType]
        result = await _slack_delete_thread_impl(
            world.runtime, auth, thread_id=f"{_CHANNEL}:{root}", origin_context_id=origin
        )
        deleted = [body["ts"] for body in _deletes(m)]

    assert deleted == [reply, root], "replies go first, then the root"
    assert result.messages_deleted == 2, "the result counts what was deleted"
    rows = [r for r in await world.audit() if r.tool_name == "delete_thread"]
    assert [(r.outcome, r.target_message_id) for r in rows] == [
        ("allowed", reply),
        ("allowed", root),
    ], "each message delete is audited"


_POLICY_CHANGES = {
    "protect": TenantAccessPolicy(protected_channel_ids=(_CHANNEL,)),
    "pin": TenantAccessPolicy(agent_channel_pins={_AGENT: ("C_ELSEWHERE",)}),
}


@pytest.mark.parametrize("change", sorted(_POLICY_CHANGES))
@pytest.mark.parametrize("action", ["edit", "delete", "delete_thread"])
async def test_a_policy_change_during_final_identity_io_stops_the_slack_call(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    change: str,
) -> None:
    world = await _world(committing_sessionmaker)
    auth, origin = await world.turn()
    original = tidy_module.find_agent_by_derived_uuid

    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        result = await original(*args, **kwargs)
        await world.set_policy(_POLICY_CHANGES[change])
        return result

    with aioresponses() as m:
        _channel_access(m)
        ts = await _post(world, auth, m, "1700000006.000100")
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _CONVERSATIONS_REPLIES, payload={"ok": True, "messages": [{"ts": ts}]}
        )
        monkeypatch.setattr(tidy_module, "find_agent_by_derived_uuid", wrapped)
        with pytest.raises(ToolError, match="writers to none|runs it only in certain channels"):
            if action == "edit":
                await _slack_edit_message_impl(
                    world.runtime,
                    auth,
                    channel_id=_CHANNEL,
                    message_id=ts,
                    content="late",
                    origin_context_id=origin,
                )
            elif action == "delete":
                await _slack_delete_message_impl(
                    world.runtime,
                    auth,
                    channel_id=_CHANNEL,
                    message_id=ts,
                    origin_context_id=origin,
                )
            else:
                await _slack_delete_thread_impl(
                    world.runtime, auth, thread_id=f"{_CHANNEL}:{ts}", origin_context_id=origin
                )
        assert not _deletes(m) and not _calls(m, _CHAT_UPDATE), (
            "no edit or delete reached Slack after the policy changed"
        )
    rows = await world.audit()
    assert [(r.outcome, r.reason) for r in rows if r.outcome != "allowed"] == [
        ("denied", "policy_changed")
    ], "the begun action is closed with a denied row"
