"""Session transcripts obey the channel seal (security review C3).

A session's events hold everything its turns saw -- the channel backfill,
thread history and every sealed read result -- so a transcript tool is a
channel read by another name. These drive the transcript tools of the main
MCP server, agent chat and the hub against a real Postgres (tenant policy,
turn origins, thread mappings) and a faked Managed Agents API.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity, Role
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools import hub
from daimon.adapters.mcp.tools.agent_chat import (
    _continue_turn_impl,  # pyright: ignore[reportPrivateUsage]
    _list_events_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.agent_chat import (
    _list_sessions_impl as _list_my_sessions_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.sessions import (
    _get_session_impl,  # pyright: ignore[reportPrivateUsage]
    _list_session_events_impl,  # pyright: ignore[reportPrivateUsage]
    _list_sessions_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.thread_sessions import create_thread_session
from daimon.core.stores.turn_origins import create_origin
from daimon.testing import ma_agent, ma_session
from daimon.testing.factories import make_platform_principal, make_tenant
from daimon.testing.ma import MARouter, build_fake_anthropic, list_response
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_AGENT = "ag_acme"
_SEALED = "chan-acme"
_OPEN = "chan-b"
_SEALED_TOPIC = "the acme merger terms"


class _World:
    def __init__(
        self,
        db: AsyncSession,
        sessionmaker: async_sessionmaker[AsyncSession],
        tenant_id: uuid.UUID,
        account_id: uuid.UUID,
        platform: str,
    ) -> None:
        self.db = db
        self.sessionmaker = sessionmaker
        self.tenant_id = tenant_id
        self.account_id = account_id
        self.platform = platform
        self.sessions: dict[str, dict[str, Any]] = {}
        self.sent: list[str] = []

    def add_session(self, session_id: str, **stamps: str) -> None:
        self.sessions[session_id] = ma_session(
            id=session_id,
            agent_id=_AGENT,
            metadata={"daimon_account": str(self.account_id), **stamps},
        ).model_dump(mode="json")

    async def origin(self, channel: str, thread: str) -> str:
        now = dt.datetime.now(dt.UTC)
        origin = await create_origin(
            self.db,
            tenant_id=self.tenant_id,
            account_id=self.account_id,
            platform=self.platform,
            parent_channel_id=channel,
            thread_id=thread,
            responder_ma_agent_id=_AGENT,
            responder_name="acme-project",
            configuration_target_ma_agent_id=None,
            configuration_target_name=None,
            role=Role.USER,
            expires_at=now + dt.timedelta(minutes=10),
            now=now,
        )
        await self.db.commit()
        return str(origin.id)

    async def seal(self, *channel_ids: str) -> None:
        await set_access_policy(
            self.db,
            tenant_id=self.tenant_id,
            policy=TenantAccessPolicy(sealed_channel_ids=channel_ids),
        )
        await self.db.commit()

    def auth(self, **extra: Any) -> AuthIdentity:
        """An ordinary chat token executing as the agent, as a channel turn holds."""
        agent_uuid = derive_agent_uuid(tenant_id=self.tenant_id, ma_agent_id=_AGENT)
        return AuthIdentity(
            account_id=self.account_id,
            tenant_id=self.tenant_id,
            role=Role.USER,
            platform=self.platform,
            chat_agent_id=agent_uuid,
            **extra,
        )

    def agent_key_auth(self) -> AuthIdentity:
        """An agent-chat key (headless: plugin, another program)."""
        agent_uuid = derive_agent_uuid(tenant_id=self.tenant_id, ma_agent_id=_AGENT)
        return AuthIdentity(
            account_id=self.account_id,
            tenant_id=self.tenant_id,
            role=Role.USER,
            platform=self.platform,
            agent_id=agent_uuid,
        )

    def runtime(self) -> McpRuntime:
        router = MARouter()
        agent = ma_agent(
            id=_AGENT,
            name="acme-project",
            metadata={"daimon_tenant": str(self.tenant_id), "daimon_name": "acme-project"},
        ).model_dump(mode="json")
        router.add("GET", r"/v1/agents", lambda _r, _m: list_response([agent]))
        router.add(
            "GET",
            r"/v1/sessions",
            lambda _r, _m: list_response(list(self.sessions.values())),
        )

        def events(_r: httpx.Request, _m: re.Match[str]) -> httpx.Response:
            return list_response(
                [
                    {
                        "id": "sevt_1",
                        "type": "user.message",
                        "content": [{"type": "text", "text": _SEALED_TOPIC}],
                        "processed_at": "2026-09-30T10:00:00Z",
                    }
                ]
            )

        def send(_r: httpx.Request, m: re.Match[str]) -> httpx.Response:
            self.sent.append(m.group(1))
            return list_response(
                [
                    {
                        "id": "sevt_sent",
                        "type": "user.message",
                        "content": [{"type": "text", "text": "again"}],
                        "processed_at": None,
                    }
                ]
            )

        router.add("GET", r"/v1/sessions/([^/?]+)/events", events)
        router.add("POST", r"/v1/sessions/([^/?]+)/events", send)
        router.add(
            "GET",
            r"/v1/sessions/([^/?]+)",
            lambda _r, m: httpx.Response(200, json=self.sessions[m.group(1)]),
        )
        settings = MagicMock()
        settings.mcp.public_url = None
        settings.mcp.jwt_secret = None
        return McpRuntime(
            session_factory=self.sessionmaker,
            client=build_fake_anthropic(router.dispatch),  # type: ignore[arg-type]
            settings=settings,  # type: ignore[arg-type]
            deployment_default=DeploymentDefault(),
        )


@pytest.fixture
async def world(db_session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]) -> _World:
    return await _world(db_session, sessionmaker, platform="discord")


async def _world(
    db: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession], *, platform: str
) -> _World:
    tenant = await make_tenant(db, platform=platform, workspace_id=f"ws-{uuid.uuid4()}")  # pyright: ignore[reportArgumentType]
    principal = await make_platform_principal(
        db, platform=platform, external_id="consultant", tenant=tenant
    )
    await db.commit()
    return _World(db, sessionmaker, tenant.id, principal.account_id, platform)


def _events_text(page: Any) -> str:
    return " ".join(str(block) for item in page.items for block in item.content or [])


# --- main MCP server: list_sessions / get_session / list_session_events ------


async def test_sealed_transcript_is_refused_from_another_channel(world: _World) -> None:
    """The exploit: in #clientb, "show the events of my session from #acme"."""
    await world.seal(_SEALED)
    world.add_session("ses_acme", daimon_channel=_SEALED, daimon_thread="thr-1")
    outside = await world.origin(_OPEN, "thr-b")

    with pytest.raises(ToolError, match="sealed channel"):
        await _list_session_events_impl(
            world.runtime(), world.auth(), "ses_acme", None, None, None, outside
        )
    with pytest.raises(ToolError, match="sealed channel"):
        await _list_session_events_impl(world.runtime(), world.auth(), "ses_acme", None, None, None)
    with pytest.raises(ToolError, match="sealed channel"):
        await _get_session_impl(world.runtime(), world.auth(), "ses_acme", outside)


async def test_sealed_transcript_is_readable_from_inside_its_channel(world: _World) -> None:
    await world.seal(_SEALED)
    world.add_session("ses_acme", daimon_channel=_SEALED, daimon_thread="thr-1")
    inside = await world.origin(_SEALED, "thr-2")

    page = await _list_session_events_impl(
        world.runtime(), world.auth(), "ses_acme", None, None, None, inside
    )

    assert _SEALED_TOPIC in _events_text(page)


async def test_list_sessions_hides_sealed_conversations_outside_their_channel(
    world: _World,
) -> None:
    await world.seal(_SEALED)
    world.add_session("ses_acme", daimon_channel=_SEALED, daimon_thread="thr-1")
    world.add_session("ses_b", daimon_channel=_OPEN, daimon_thread="thr-b")
    world.add_session("ses_headless")
    outside = await world.origin(_OPEN, "thr-b")
    inside = await world.origin(_SEALED, "thr-9")

    seen_outside = await _list_sessions_impl(world.runtime(), world.auth(), None, None, outside)
    seen_inside = await _list_sessions_impl(world.runtime(), world.auth(), None, None, inside)

    assert sorted(s.id for s in seen_outside) == ["ses_b", "ses_headless"]
    assert sorted(s.id for s in seen_inside) == ["ses_acme", "ses_b", "ses_headless"]


async def test_sealing_a_channel_later_covers_its_old_conversations(world: _World) -> None:
    world.add_session("ses_acme", daimon_channel=_SEALED, daimon_thread="thr-1")
    await _list_session_events_impl(world.runtime(), world.auth(), "ses_acme", None, None, None)

    await world.seal(_SEALED)

    with pytest.raises(ToolError, match="sealed channel"):
        await _list_session_events_impl(world.runtime(), world.auth(), "ses_acme", None, None, None)


async def test_unsealing_does_not_open_a_transcript_written_under_the_seal(
    world: _World,
) -> None:
    await world.seal("some-other-channel")
    world.add_session(
        "ses_acme", daimon_channel=_SEALED, daimon_thread="thr-1", daimon_sealed="true"
    )

    with pytest.raises(ToolError, match="sealed channel"):
        await _list_session_events_impl(world.runtime(), world.auth(), "ses_acme", None, None, None)


async def test_a_session_from_before_the_stamp_fails_closed_in_a_sealed_tenant(
    world: _World,
) -> None:
    """Unstamped sessions a thread ran on: the parent channel is unknown."""
    await world.seal(_SEALED)
    world.add_session("ses_legacy")
    world.add_session("ses_headless")
    await create_thread_session(
        world.db,
        tenant_id=world.tenant_id,
        platform="discord",
        thread_id="thr-legacy",
        account_id=world.account_id,
        ma_session_id="ses_legacy",
        ma_agent_id=_AGENT,
    )
    await world.db.commit()
    same_thread = await world.origin(_SEALED, "thr-legacy")

    outside = await _list_sessions_impl(world.runtime(), world.auth(), None, None)
    in_thread = await _list_sessions_impl(world.runtime(), world.auth(), None, None, same_thread)

    assert [s.id for s in outside] == ["ses_headless"]
    assert sorted(s.id for s in in_thread) == ["ses_headless", "ses_legacy"]


async def test_a_sealed_slack_thread_is_its_own_seal(
    db_session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    world = await _world(db_session, sessionmaker, platform="slack")
    await world.seal("C1:1700000000.000100")
    world.add_session("ses_thread", daimon_channel="C1", daimon_thread="1700000000.000100")
    world.add_session("ses_sibling", daimon_channel="C1", daimon_thread="1700000000.000999")
    sibling = await world.origin("C1", "1700000000.000999")
    same = await world.origin("C1", "1700000000.000100")

    from_sibling = await _list_sessions_impl(world.runtime(), world.auth(), None, None, sibling)
    from_same = await _list_sessions_impl(world.runtime(), world.auth(), None, None, same)

    assert [s.id for s in from_sibling] == ["ses_sibling"]
    assert sorted(s.id for s in from_same) == ["ses_sibling", "ses_thread"]


# --- agent chat and the hub: headless callers, always outside ----------------


async def test_agent_key_cannot_read_or_continue_a_sealed_conversation(world: _World) -> None:
    await world.seal(_SEALED)
    world.add_session("ses_acme", daimon_channel=_SEALED, daimon_thread="thr-1")
    world.add_session("ses_mine")
    runtime = world.runtime()
    auth = world.agent_key_auth()

    with pytest.raises(ToolError, match="sealed channel"):
        await _list_events_impl(runtime, auth, "ses_acme", None, None, None)
    with pytest.raises(ToolError, match="sealed channel"):
        await _continue_turn_impl(runtime, auth, "ses_acme", "what did they say?")
    assert world.sent == [], "a refused follow-up must never reach the session"
    assert [s.id for s in await _list_my_sessions_impl(runtime, auth)] == ["ses_mine"]


async def test_hub_lists_no_sealed_conversation(world: _World) -> None:
    """A person removed from #acme keeps the hub: it must not list the session."""
    await world.seal(_SEALED)
    world.add_session("ses_acme", daimon_channel=_SEALED, daimon_thread="thr-1")
    world.add_session("ses_mine")
    ma = ma_agent(id=_AGENT, name="acme-project")

    listed = await hub._list_my_sessions_impl(world.runtime(), world.agent_key_auth(), ma)  # pyright: ignore[reportPrivateUsage]

    assert [s.id for s in listed] == ["ses_mine"]
