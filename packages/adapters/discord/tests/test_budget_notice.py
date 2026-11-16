"""Discord delivery of the channel budget notice."""

from __future__ import annotations

import uuid
from dataclasses import MISSING, fields, replace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from daimon.adapters.discord import budget_notice
from daimon.adapters.discord.budget_notice import discord_budget_notifier, with_budget_notifier
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.adapters.discord.theme import COLOR_RED
from daimon.core.channel_budget_notice import BudgetNotice
from daimon.core.config import DirectMessagePolicy
from daimon.core.turn.deps import TurnDeps
from daimon.testing.turn_fakes import deps_for

_TENANT = uuid.uuid4()


@pytest.fixture(autouse=True)
def default_agent_grant(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """The default agent's grant admits everyone unless a test says otherwise."""
    admits = AsyncMock(return_value=True)
    monkeypatch.setattr(budget_notice, "sender_admits", admits)
    return admits


def _required(cls: Any, **given: object) -> dict[str, object]:
    needed = [f.name for f in fields(cls) if f.default is MISSING and f.default_factory is MISSING]
    return {name: MagicMock() for name in needed} | given


def _runtime(policies: dict[uuid.UUID, DirectMessagePolicy]) -> DiscordRuntime:
    settings = MagicMock()
    settings.direct_message_policies = policies
    return DiscordRuntime(
        **_required(
            DiscordRuntime, settings=settings, turn_deps=replace(deps_for(), turn_path=None)
        )
    )


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
    text = dms["1"].send.await_args.kwargs["embed"].description
    assert dms["1"].send.await_args.kwargs["embed"].color.value == COLOR_RED
    assert text.startswith("<#222> has used its $5.00 monthly budget.\n\n"), text
    dms["3"].send.assert_not_awaited()  # the DM policy leaves 3 out


async def test_a_recipient_the_default_agent_does_not_admit_gets_no_dm(
    default_agent_grant: AsyncMock,
) -> None:
    """The workspace default agent's grant holds a budget notice like any agent-sent DM."""
    default_agent_grant.side_effect = lambda *_a, user_id, **_k: user_id == "1"
    sent = AsyncMock()

    async def open_dm(_guild_id: int, _user_id: int) -> Any:
        return sent

    notifier = discord_budget_notifier(_runtime({}), open_dm, lambda: False)
    assert await notifier(_notice()) == 1, "only the admitted recipient is messaged"
    sent.send.assert_awaited_once()


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


async def test_the_default_agents_grant_reads_the_recipients_server_roles(
    default_agent_grant: AsyncMock,
) -> None:
    """A grant listing a role admits its holders: the notice looks the roles up."""
    from daimon.core.membership import CachedMembershipResolver, Membership

    async def membership(_tenant: uuid.UUID, guild_id: str, user_id: str) -> Membership:
        assert guild_id == "111"
        return Membership(is_member=True, role_ids=("R1",) if user_id == "1" else ())

    seen: dict[str, frozenset[str]] = {}

    async def admits(*_a: object, user_id: str, groups: Any, **_k: object) -> bool:
        seen[user_id] = await groups(user_id, frozenset({"R1", "R2"}))
        return "R1" in seen[user_id]

    default_agent_grant.side_effect = admits
    plain = _runtime({})
    deps = replace(plain.turn_deps, membership_resolver=CachedMembershipResolver(membership))
    runtime = replace(plain, turn_deps=deps)
    sent = AsyncMock()

    async def open_dm(_guild_id: int, _user_id: int) -> Any:
        return sent

    assert await discord_budget_notifier(runtime, open_dm, lambda: False)(_notice()) == 1
    assert seen == {"1": frozenset({"R1"}), "2": frozenset(), "3": frozenset()}
