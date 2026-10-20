"""Self-hosted Discord and Teams participation never consults an Anthropic client."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import discord
from anthropic import AsyncAnthropic
from daimon.adapters.discord.thread_participation import ThreadParticipant
from daimon.adapters.teams.identity import TeamsInbound
from daimon.adapters.teams.participation import TeamsParticipation
from daimon.adapters.teams.thread_reader import ThreadReader
from daimon.core.channel_backend import channel_ref, set_channel_backend
from daimon.core.config import ThreadParticipationSettings
from daimon.core.participation_gates import ParticipationGates
from daimon.core.scope import DeploymentDefault
from daimon.core.thread_participation import ParticipationMode, ResolvedParticipation
from daimon.testing.db import (  # noqa: F401
    db_clean,
    db_engine,
    db_schema,
)
from daimon.testing.db import (
    db_session as db_session,
)
from daimon.testing.db import (
    db_session_factory as db_session_factory,
)
from daimon.testing.factories import make_tenant
from mux.contracts.config import BackendConfig
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class NoAnthropic:
    def __getattribute__(self, name: str) -> object:
        raise AssertionError(f"selfhosted flow accessed Anthropic.{name}")


async def test_selfhosted_discord_and_teams_followed_threads_stay_mention_only(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    client = cast(AsyncAnthropic, NoAnthropic())
    settings = ThreadParticipationSettings(mode=ParticipationMode.ON, quiet_seconds=0.01)
    tenants = {}
    for platform in ("discord", "teams"):
        tenant = await make_tenant(db_session, platform=platform, workspace_id=platform)
        tenants[platform] = tenant
        await set_channel_backend(
            db_session,
            channel_ref(tenant.id, platform, "4200"),
            BackendConfig(backend="selfhosted", profile="selfhosted.loop", model="gpt-5"),
        )
    await db_session.commit()

    participant = ThreadParticipant(
        settings=settings,
        sessionmaker=db_session_factory,
        anthropic=client,
        bot_user_id=999,
        bot_display_name="Daimon",
        billing_config=None,
        markup=Decimal("1"),
        deployment_default=DeploymentDefault(),
    )
    thread = MagicMock(spec=discord.Thread)
    thread.id, thread.parent_id = 4242, 4200
    thread.history = MagicMock(side_effect=AssertionError("history must not be read"))
    message = cast(
        discord.Message,
        SimpleNamespace(id=1, author=SimpleNamespace(id=2, display_name="user"), content="private"),
    )
    assert not await participant.should_respond(
        thread,
        [message],
        trigger=message,
        tenant_id=tenants["discord"].id,
        resolved=ResolvedParticipation(ParticipationMode.ON, "deployment"),
    )

    gates = ParticipationGates(
        platform="teams",
        settings=settings,
        sessionmaker=db_session_factory,
        anthropic=client,
        bot_display_name="Daimon",
        billing_config=None,
        markup=Decimal("1"),
        deployment_default=DeploymentDefault(),
    )
    tasks: list[asyncio.Task[None]] = []

    def spawn(coro: Coroutine[Any, Any, None], *, name: str) -> asyncio.Task[None]:
        task = asyncio.create_task(coro, name=name)
        tasks.append(task)
        return task

    fire = AsyncMock()
    reader = MagicMock(spec=ThreadReader)
    reader.read_window = AsyncMock(side_effect=AssertionError("history must not be read"))
    teams = TeamsParticipation(
        gates=gates,
        settings=settings,
        reader=reader,
        spawn=spawn,
        fire=fire,
        is_busy=lambda _: False,
    )
    inbound = TeamsInbound(
        kind="channel",
        entra_tenant_id="tenant",
        user_id="user",
        conversation_id="thread",
        channel_id="4200",
        activity_id="1",
        text="private",
        service_url=None,
        unprompted=True,
    )
    await teams.observe(inbound, tenants["teams"].id, admitted=AsyncMock(return_value=inbound))
    await asyncio.gather(*tasks)
    reader.read_window.assert_not_awaited()
    fire.assert_not_awaited()
