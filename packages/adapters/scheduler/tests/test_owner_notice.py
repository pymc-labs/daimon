"""The owner of a failed routine gets the agent's own message, or the plain card when the
agent's turn cannot run."""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from daimon.adapters.scheduler import owner_notice
from daimon.core.config import DirectMessagePolicy
from daimon.core.errors import TurnError
from daimon.core.membership import CachedMembershipResolver, Membership
from daimon.core.stores.direct_messages import set_dm_enabled
from daimon.core.stores.routines import create_routine
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.termination import TerminationReason
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .test_main import _make_test_settings  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("turn_runs", [True, False])
async def test_owner_gets_the_agents_message_or_the_plain_card(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    turn_runs: bool,
) -> None:
    tenant = await make_tenant(db_session, platform="discord", workspace_id="424242")
    row = await create_routine(
        db_session,
        tenant_id=tenant.id,
        created_by_user_id="123",
        agent_id="ag_1",
        agent_name="analyst",
        name="Weekly readout",
        cron_expr="0 9 * * 1",
        timezone_="UTC",
        trigger_message="summarise",
        next_fire_at=datetime.now(UTC),
        destination_kind="channel",
        destination_id="456",
    )
    await set_dm_enabled(db_session, tenant_id=tenant.id, enabled=True)
    await db_session.commit()
    settings = _make_test_settings(monkeypatch).model_copy(
        update={"direct_message_policies": {tenant.id: DirectMessagePolicy(mode="members")}}
    )
    prompts: list[str] = []

    async def dm_turn(*_args: Any, prompt: str, **_kwargs: Any) -> SimpleNamespace:
        prompts.append(prompt)
        if not turn_runs:
            raise TurnError(kind="upstream", message="provider down")
        return SimpleNamespace(text="Your weekly readout failed.")

    async def member(*_args: Any) -> Membership:
        return Membership(is_member=True)

    monkeypatch.setattr(owner_notice, "run_dm_routine", dm_turn)
    texts: list[tuple[str, str]] = []
    cards: list[tuple[str, str, str]] = []

    async def send_text(user: str, text: str) -> None:
        texts.append((user, text))

    async def send_card(user: str, title: str, body: str) -> None:
        cards.append((user, title, body))

    notify = owner_notice.build_owner_notifier(
        sm=db_session_factory,
        settings=settings,
        deps=cast(TurnDeps, SimpleNamespace(membership_resolver=CachedMembershipResolver(member))),
        send_card=send_card,
        send_text=send_text,
    )
    await notify(row, TerminationReason.UPSTREAM, True)

    assert len(prompts) == 1 and "Weekly readout" in prompts[0], "the agent gets the facts"
    if turn_runs:
        assert texts == [("123", "Your weekly readout failed.")] and not cards, "agent's words"
    else:
        assert not texts and [c[:2] for c in cards] == [
            ("123", 'Your routine "Weekly readout" failed')
        ], "the plain card when the turn cannot run"
