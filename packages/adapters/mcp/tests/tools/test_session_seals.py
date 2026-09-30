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
from daimon.adapters.mcp.server import create_mcp_app
from daimon.adapters.mcp.tools import hub
from daimon.adapters.mcp.tools.agent_chat import (
    _ask_impl,  # pyright: ignore[reportPrivateUsage]
    _continue_turn_impl,  # pyright: ignore[reportPrivateUsage]
    _list_events_impl,  # pyright: ignore[reportPrivateUsage]
    _start_turn_impl,  # pyright: ignore[reportPrivateUsage]
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
from daimon.core.config import AnthropicSettings, DatabaseSettings, McpSettings, Settings
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_auth import mint_jwt
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.thread_sessions import create_thread_session
from daimon.core.stores.turn_origins import create_origin
from daimon.testing import ma_agent, ma_session
from daimon.testing.asgi import call_mcp_tool
from daimon.testing.factories import make_platform_principal, make_tenant
from daimon.testing.ma import MARouter, build_fake_anthropic, list_response
from fastmcp.exceptions import ToolError
from pydantic import HttpUrl, PostgresDsn, SecretStr
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
    await world.seal(_SEALED)
    world.add_session(
        "ses_acme", daimon_channel=_SEALED, daimon_thread="thr-1", daimon_sealed=_SEALED
    )
    outside = await world.origin(_OPEN, "thr-b")
    inside = await world.origin(_SEALED, "thr-2")

    await world.seal("some-other-channel")

    with pytest.raises(ToolError, match="sealed channel"):
        await _list_session_events_impl(
            world.runtime(), world.auth(), "ses_acme", None, None, None, outside
        )
    page = await _list_session_events_impl(
        world.runtime(), world.auth(), "ses_acme", None, None, None, inside
    )
    assert _SEALED_TOPIC in _events_text(page)


async def test_a_thread_sealed_on_its_own_stays_its_own_after_unseal(
    world: _World,
) -> None:
    """The parent was never sealed: only the sealed thread itself is inside."""
    await world.seal("thr-1")
    world.add_session(
        "ses_thread", daimon_channel=_OPEN, daimon_thread="thr-1", daimon_sealed="thr-1"
    )
    sibling = await world.origin(_OPEN, "thr-2")
    same = await world.origin(_OPEN, "thr-1")

    await world.seal("some-other-channel")

    with pytest.raises(ToolError, match="sealed channel"):
        await _list_session_events_impl(
            world.runtime(), world.auth(), "ses_thread", None, None, None, sibling
        )
    await _list_session_events_impl(
        world.runtime(), world.auth(), "ses_thread", None, None, None, same
    )


async def test_a_sealed_slack_thread_stays_its_own_after_unseal(
    db_session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    world = await _world(db_session, sessionmaker, platform="slack")
    await world.seal("C1:1700000000.000100")
    world.add_session(
        "ses_thread",
        daimon_channel="C1",
        daimon_thread="1700000000.000100",
        daimon_sealed="C1:1700000000.000100",
    )
    sibling = await world.origin("C1", "1700000000.000999")

    await world.seal()

    assert await _list_sessions_impl(world.runtime(), world.auth(), None, None, sibling) == []


async def test_an_agent_key_cannot_claim_a_turn_origin_on_the_session_tools(
    world: _World,
) -> None:
    """Origins belong to chat turns; an agent key is always outside."""
    await world.seal(_SEALED)
    world.add_session("ses_acme", daimon_channel=_SEALED, daimon_thread="thr-1")
    inside = await world.origin(_SEALED, "thr-2")

    with pytest.raises(ToolError, match="sealed channel"):
        await _list_session_events_impl(
            world.runtime(), world.agent_key_auth(), "ses_acme", None, None, None, inside
        )
    assert (
        await _list_sessions_impl(world.runtime(), world.agent_key_auth(), None, None, inside) == []
    )


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


# --- a sealed turn can't open or drive another session -----------------------


async def test_a_chat_turn_cannot_start_ask_or_continue_through_agent_chat(
    world: _World,
) -> None:
    """A session opened or continued from a sealed turn would carry sealed
    content out under no seal stamp, so a chat turn's credential is refused
    before anything reaches Managed Agents."""
    await world.seal(_SEALED)
    world.add_session("ses_open", daimon_channel=_OPEN, daimon_thread="thr-b")
    runtime = world.runtime()
    chat_turn = world.auth(agent_id=world.agent_key_auth().agent_id)

    with pytest.raises(ToolError, match="chat turn"):
        await _start_turn_impl(runtime, chat_turn, _SEALED_TOPIC)
    with pytest.raises(ToolError, match="chat turn"):
        await _continue_turn_impl(runtime, chat_turn, "ses_open", _SEALED_TOPIC)
    with pytest.raises(ToolError, match="chat turn"):
        await _ask_impl(runtime, chat_turn, _SEALED_TOPIC, handle="ses_open")
    assert world.sent == []


_SECRET = b"s" * 32


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("start_turn", {"message": _SEALED_TOPIC}),
        ("ask", {"message": _SEALED_TOPIC}),
        ("continue_turn", {"handle": "ses_open", "message": _SEALED_TOPIC}),
    ],
)
async def test_a_chat_turn_token_does_not_reach_the_agent_chat_turn_tools(
    world: _World, tool: str, arguments: dict[str, object]
) -> None:
    """The token a sealed channel turn holds: account + chat_agent_id."""
    app = create_mcp_app(
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
            anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
            mcp=McpSettings(
                jwt_secret=SecretStr(_SECRET.decode()), public_url=HttpUrl("https://x/mcp")
            ),
            _env_file=None,  # type: ignore[call-arg]  # isolate from repo .env
        ),
        sessionmaker=world.sessionmaker,
    )
    token = mint_jwt(
        account_id=world.account_id,
        secret=_SECRET,
        now=dt.datetime.now(dt.UTC),
        chat_agent_id=derive_agent_uuid(tenant_id=world.tenant_id, ma_agent_id=_AGENT),
    )

    result = await call_mcp_tool(app, token=token, name=tool, arguments=arguments)

    payload = result.get("result", result)
    assert payload.get("isError") or "error" in result, f"{tool} must be refused: {result!r}"
    assert "Unknown tool" in str(result) or "not found" in str(result).lower(), result
    assert world.sent == []


# --- a recorded seal holds with nothing sealed any more ----------------------


async def test_with_every_seal_removed_the_recorded_seal_still_decides(world: _World) -> None:
    """No seal left in the policy: the inside reader still reads, others don't."""
    world.add_session(
        "ses_acme", daimon_channel=_SEALED, daimon_thread="thr-1", daimon_sealed="thr-1"
    )
    same = await world.origin(_SEALED, "thr-1")
    sibling = await world.origin(_SEALED, "thr-2")
    outside = await world.origin(_OPEN, "thr-b")

    page = await _list_session_events_impl(
        world.runtime(), world.auth(), "ses_acme", None, None, None, same
    )
    assert _SEALED_TOPIC in _events_text(page)
    for origin in (sibling, outside, None):
        with pytest.raises(ToolError, match="sealed channel"):
            await _list_session_events_impl(
                world.runtime(), world.auth(), "ses_acme", None, None, None, origin
            )


async def test_a_successor_stamped_with_an_inherited_seal_is_refused_outside(
    world: _World,
) -> None:
    """The stamp a replacement inherits (see test_session_preparation), with the
    channel unsealed since: list, get, events and continue all refuse."""
    from daimon.core.session_seal import origin_stamp

    world.add_session(
        "ses_successor", **origin_stamp(channel_id=_SEALED, thread_id="thr-1", seal={_SEALED})
    )
    outside = await world.origin(_OPEN, "thr-b")
    runtime = world.runtime()

    assert await _list_sessions_impl(runtime, world.auth(), None, None, outside) == []
    with pytest.raises(ToolError, match="sealed channel"):
        await _get_session_impl(runtime, world.auth(), "ses_successor", outside)
    with pytest.raises(ToolError, match="sealed channel"):
        await _list_session_events_impl(
            runtime, world.auth(), "ses_successor", None, None, None, outside
        )
    with pytest.raises(ToolError, match="sealed channel"):
        await _continue_turn_impl(runtime, world.agent_key_auth(), "ses_successor", "hi")
    assert world.sent == []


async def test_a_session_stamped_sealed_by_the_first_release_stays_sealed_to_its_channel(
    world: _World,
) -> None:
    """#337 wrote a bare "true": it means the stamped channel."""
    world.add_session(
        "ses_acme", daimon_channel=_SEALED, daimon_thread="thr-1", daimon_sealed="true"
    )
    inside = await world.origin(_SEALED, "thr-2")
    outside = await world.origin(_OPEN, "thr-b")

    await _list_session_events_impl(
        world.runtime(), world.auth(), "ses_acme", None, None, None, inside
    )
    with pytest.raises(ToolError, match="sealed channel"):
        await _list_session_events_impl(
            world.runtime(), world.auth(), "ses_acme", None, None, None, outside
        )


# --- a thread sealed twice: under its parent's seal and on its own -----------


async def test_unsealing_the_parent_keeps_a_thread_sealed_on_its_own_sealed(
    world: _World,
) -> None:
    """Parent and thread both sealed when the conversation ran; the parent is
    unsealed later. The thread's own seal still holds: siblings are refused."""
    from daimon.core.session_seal import origin_stamp

    world.add_session(
        "ses_thread",
        **origin_stamp(channel_id=_SEALED, thread_id="thr-1", seal={_SEALED, "thr-1"}),
    )
    same = await world.origin(_SEALED, "thr-1")
    sibling = await world.origin(_SEALED, "thr-2")
    await world.seal("thr-1")

    with pytest.raises(ToolError, match="sealed channel"):
        await _list_session_events_impl(
            world.runtime(), world.auth(), "ses_thread", None, None, None, sibling
        )
    await _list_session_events_impl(
        world.runtime(), world.auth(), "ses_thread", None, None, None, same
    )


async def test_unsealing_the_parent_keeps_a_slack_thread_sealed_on_its_own_sealed(
    db_session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    from daimon.core.session_seal import origin_stamp

    world = await _world(db_session, sessionmaker, platform="slack")
    world.add_session(
        "ses_thread",
        **origin_stamp(
            channel_id="C1",
            thread_id="1700000000.000100",
            seal={"C1", "C1:1700000000.000100"},
        ),
    )
    same = await world.origin("C1", "1700000000.000100")
    sibling = await world.origin("C1", "1700000000.000999")
    await world.seal("C1:1700000000.000100")

    assert await _list_sessions_impl(world.runtime(), world.auth(), None, None, sibling) == []
    listed = await _list_sessions_impl(world.runtime(), world.auth(), None, None, same)
    assert [s.id for s in listed] == ["ses_thread"]


async def test_unsealing_the_thread_keeps_the_parent_seal(world: _World) -> None:
    from daimon.core.session_seal import origin_stamp

    world.add_session(
        "ses_thread",
        **origin_stamp(channel_id=_SEALED, thread_id="thr-1", seal={_SEALED, "thr-1"}),
    )
    sibling = await world.origin(_SEALED, "thr-2")
    outside = await world.origin(_OPEN, "thr-b")
    await world.seal(_SEALED)

    for origin in (sibling, outside):
        with pytest.raises(ToolError, match="sealed channel"):
            await _list_session_events_impl(
                world.runtime(), world.auth(), "ses_thread", None, None, None, origin
            )
