"""Gate C: `require_channel_writable` refuses a post into a room outsiders can read.

A held credential posts in its own room and its threads. Any other credential, an
open-room turn or a direct-message turn included, is refused, before the guard's early
returns. `off` reads nothing, `shadow` logs, and a failed read refuses the post.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock

import discord
import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._channel_policy import require_channel_writable
from daimon.adapters.mcp.tools.discord._visibility import (
    _require_discord_channel_writable,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core import audience_gate
from daimon.core.channel_audience import ChannelAudience
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.domain import Role
from daimon.core.turn_hold import TurnHold
from daimon.testing.factories import make_tenant
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs


class _World:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.mode: Any = "on"
        self.contained: set[str] = set()
        self.reads = 0

        async def mode(session: object, place: object) -> str:
            if isinstance(self.mode, Exception):
                raise self.mode
            return self.mode

        async def fact(
            session: object, *, tenant_id: object, platform: str, channel_id: str
        ) -> ChannelAudience:
            self.reads += 1
            return ChannelAudience(
                contained=channel_id in self.contained, known=True, narrow=False, source="live"
            )

        monkeypatch.setattr(audience_gate, "audience_mode", mode)
        monkeypatch.setattr(audience_gate, "channel_has_outsiders", fact)


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> _World:
    return _World(monkeypatch)


async def _setup(
    db: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession], platform: str = "discord"
) -> tuple[McpRuntime, uuid.UUID]:
    tenant = await make_tenant(db, platform=platform, workspace_id=f"ws-{uuid.uuid4()}")  # pyright: ignore[reportArgumentType]
    await db.commit()
    runtime = McpRuntime(
        session_factory=sessionmaker,
        client=MagicMock(),
        settings=MagicMock(),
        deployment_default=DeploymentDefault(),
    )
    return runtime, tenant.id


def _auth(tenant_id: uuid.UUID, platform: str = "discord", hold: TurnHold | None = None) -> Any:
    return AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=tenant_id,
        role=Role.ADMIN,
        platform=platform,
        platform_user_id="u1",
        is_admin=True,
        hold=hold,
    )


async def test_an_unheld_turn_is_refused_in_a_contained_room_even_with_no_rules(
    world: _World,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    runtime, tenant_id = await _setup(db_session, db_session_factory)
    world.contained = {"room-1"}
    auth = _auth(tenant_id)
    # The room that a turn started in, a direct-message turn and an open-room turn all hold
    # the same credential, so one refusal covers each of them.
    with pytest.raises(ToolError, match="outside the team can read this channel"):
        await require_channel_writable(runtime, auth, channel_id="room-1")
    with pytest.raises(ToolError, match="outside the team can read this channel"):
        await require_channel_writable(
            runtime, auth, channel_id="thread-9", parent_channel_id="room-1"
        )
    await require_channel_writable(runtime, auth, channel_id="room-2")


async def test_a_held_turn_posts_in_its_room_and_its_thread_only(
    world: _World,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    runtime, tenant_id = await _setup(db_session, db_session_factory)
    world.contained = {"room-1", "room-2"}
    auth = _auth(tenant_id, hold=TurnHold("discord", "room-1", client=False))
    await require_channel_writable(runtime, auth, channel_id="room-1")
    await require_channel_writable(runtime, auth, channel_id="thread-9", parent_channel_id="room-1")
    with pytest.raises(ToolError):
        await require_channel_writable(runtime, auth, channel_id="room-2")


async def test_the_discord_guard_applies_to_a_channel_and_a_thread(
    world: _World,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Every Discord post and edit tool reaches `require_channel_writable` through this guard."""
    runtime, tenant_id = await _setup(db_session, db_session_factory)
    world.contained = {"1001"}
    auth = _auth(tenant_id)
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 1001
    channel.category_id = None
    with pytest.raises(ToolError, match="outside the team"):
        await _require_discord_channel_writable(runtime, auth, channel)
    thread = MagicMock(spec=discord.Thread)
    thread.id = 2002
    thread.parent = channel
    with pytest.raises(ToolError, match="outside the team"):
        await _require_discord_channel_writable(runtime, auth, thread)
    open_channel = MagicMock(spec=discord.TextChannel)
    open_channel.id = 1002
    open_channel.category_id = None
    await _require_discord_channel_writable(runtime, auth, open_channel)


async def test_mode_off_reads_no_fact_and_slack_is_never_read(
    world: _World,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    runtime, tenant_id = await _setup(db_session, db_session_factory)
    world.contained = {"room-1"}
    world.mode = "off"
    await require_channel_writable(runtime, _auth(tenant_id), channel_id="room-1")
    world.mode = "on"
    slack_runtime, slack_tenant = await _setup(db_session, db_session_factory, "slack")
    await require_channel_writable(slack_runtime, _auth(slack_tenant, "slack"), channel_id="room-1")
    assert world.reads == 0


async def test_shadow_posts_and_logs_without_text(
    world: _World,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    runtime, tenant_id = await _setup(db_session, db_session_factory)
    world.contained = {"room-1"}
    world.mode = "shadow"
    with capture_logs() as logs:
        await require_channel_writable(runtime, _auth(tenant_id), channel_id="room-1")
    records = [log for log in logs if log["event"] == "audience.shadow"]
    assert [(r["kind"], r["channel_id"], r["reason"], r["agent"]) for r in records] == [
        ("post_refused", "room-1", "audience_contained", None)
    ]


async def test_a_failed_read_refuses_the_post(
    world: _World,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    runtime, tenant_id = await _setup(db_session, db_session_factory)
    world.mode = RuntimeError("database down")
    with pytest.raises(ToolError, match="could not be checked"):
        await require_channel_writable(runtime, _auth(tenant_id), channel_id="room-1")
