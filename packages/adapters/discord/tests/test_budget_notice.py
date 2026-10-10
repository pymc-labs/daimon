"""Discord delivery of the channel budget notice."""

from __future__ import annotations

import uuid
from dataclasses import MISSING, fields
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from daimon.adapters.discord.budget_notice import discord_budget_notifier, with_budget_notifier
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.channel_budget_notice import BudgetNotice
from daimon.core.config import DirectMessagePolicy
from daimon.core.turn.deps import TurnDeps

_TENANT = uuid.uuid4()


def _required(cls: Any, **given: object) -> dict[str, object]:
    needed = [f.name for f in fields(cls) if f.default is MISSING and f.default_factory is MISSING]
    return {name: MagicMock() for name in needed} | given


def _runtime(policies: dict[uuid.UUID, DirectMessagePolicy]) -> DiscordRuntime:
    settings = MagicMock()
    settings.direct_message_policies = policies
    return DiscordRuntime(**_required(DiscordRuntime, settings=settings, turn_deps=MagicMock()))


def _notice() -> BudgetNotice:
    return BudgetNotice(
        tenant_id=_TENANT,
        workspace_id="111",
        platform="discord",
        channel_id="222",
        recipient_ids=("1", "2", "3"),
        limit_line="$5.00",
        monthly=True,
        budget_id=uuid.uuid4(),
        window_key="window",
    )


async def test_each_allowed_recipient_gets_a_dm_and_one_failure_skips_only_them() -> None:
    dms = {"1": AsyncMock(), "3": AsyncMock()}

    async def open_dm(guild_id: int, user_id: int) -> Any:
        assert guild_id == 111
        if str(user_id) not in dms:
            raise LookupError("not a member")
        return dms[str(user_id)]

    policy = DirectMessagePolicy(mode="allowlist", recipient_ids=["1", "2"])
    notifier = discord_budget_notifier(_runtime({_TENANT: policy}), open_dm, lambda: False)
    delivered = await notifier(_notice())
    assert delivered == 1, "only a DM that landed counts"

    dms["1"].send.assert_awaited_once()
    text = dms["1"].send.await_args.args[0]
    assert text.startswith("<#222> has used its $5.00 monthly budget.\n\n"), text
    dms["3"].send.assert_not_awaited()  # the DM policy leaves 3 out


async def test_a_closed_client_counts_as_undelivered_instead_of_raising() -> None:
    """After shutdown closes the HTTP session, a DM fails with aiohttp's RuntimeError."""
    closed = [False]
    sent = AsyncMock()

    async def open_dm(_guild_id: int, user_id: int) -> Any:
        if user_id == 2:
            closed[0] = True
            raise RuntimeError("Session is closed")
        return sent

    notifier = discord_budget_notifier(_runtime({}), open_dm, lambda: closed[0])
    delivered = await notifier(_notice())

    assert delivered == 1, "the DM sent before the close counts; the rest are undelivered"
    assert sent.send.await_count == 1, "nothing is tried once the client is closed"


async def test_a_runtime_error_on_an_open_client_still_raises() -> None:
    """Only a closed client's failure is an undelivered DM; anything else keeps the window."""

    async def open_dm(_guild_id: int, _user_id: int) -> Any:
        raise RuntimeError("unexpected")

    notifier = discord_budget_notifier(_runtime({}), open_dm, lambda: False)
    with pytest.raises(RuntimeError, match="unexpected"):
        await notifier(_notice())


def test_a_real_runtime_gets_the_notifier_and_a_stand_in_is_left_alone() -> None:
    deps = TurnDeps(**_required(TurnDeps))
    runtime = DiscordRuntime(**_required(DiscordRuntime, turn_deps=deps))
    wired = with_budget_notifier(runtime, AsyncMock(), lambda: False)
    assert wired.turn_deps.budget_notifier is not None
    assert runtime.turn_deps.budget_notifier is None, "the original runtime is not mutated"
    stand_in = MagicMock()
    assert with_budget_notifier(stand_in, AsyncMock(), lambda: False) is stand_in
