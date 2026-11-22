"""A failed routine's owner is told by DM on every chat platform: the agent's words, written
with no tools, or the plain card; and why not, where DMs are closed."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
import structlog
from daimon.core import owner_notices
from daimon.core.auxiliary_calls import AuxiliaryCompletion
from daimon.core.config import DirectMessagePolicy, Settings
from daimon.core.ops_catalog import CATALOG
from daimon.core.ops_events import OpsEvent
from daimon.core.owner_notices import OwnerNotice, build_owner_notifier, poll_owner_notices_once
from daimon.core.stores.direct_messages import set_dm_enabled
from daimon.core.stores.dm_access import set_grant
from daimon.core.stores.domain import RoutineRow
from daimon.core.stores.routines import create_routine
from daimon.core.turn.termination import TerminationReason
from daimon.testing.factories import make_tenant
from mux.contracts.completions import CompletionUsage
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def _routine(
    db_session: AsyncSession, platform: str, *, owner: str | None = "123", dms: bool = True
) -> RoutineRow:
    tenant = await make_tenant(db_session, platform=platform, workspace_id=f"{platform}-1")
    row = await create_routine(
        db_session,
        tenant_id=tenant.id,
        created_by_user_id=owner,
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
    await set_dm_enabled(db_session, tenant_id=tenant.id, enabled=dms)
    await db_session.commit()
    return row


class _World:
    """The notifier's calls out, recorded: the agent call, metering, DMs and issues."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, write_fails: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.metered: list[str] = []
        self.sent: list[OwnerNotice] = []
        self.events: list[OpsEvent] = []
        self.send_fails = False

        async def complete(*_args: Any, **kwargs: Any) -> AuxiliaryCompletion:
            self.calls.append(kwargs)
            if write_fails:
                raise RuntimeError("provider down")
            return AuxiliaryCompletion(
                "Your weekly readout failed.",
                CompletionUsage(input_tokens=10, output_tokens=5),
                False,
                "anthropic",
                "claude-opus-5-5",
            )

        async def meter(**kwargs: Any) -> None:
            self.metered.append(kwargs["purpose"])

        async def agent(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
            return SimpleNamespace(system="You are Analyst.", model=SimpleNamespace(id="claude-x"))

        async def no_links(*_args: Any) -> None:
            return None

        async def open_gate(*_args: Any, **_kwargs: Any) -> None:
            return None

        for name, fake in {
            "auxiliary_complete": complete,
            "record_auxiliary_usage": meter,
            "retrieve_agent": agent,
            "routine_links": no_links,
            "spend_gate_in_new_session": open_gate,
            "report_ops_event": lambda _sm, event, **_kw: self.events.append(event),
        }.items():
            monkeypatch.setattr(owner_notices, name, fake)

    async def dm(self, row: RoutineRow, notice: OwnerNotice) -> str | None:
        self.sent.append(notice)
        if self.send_fails:
            raise httpx.ConnectError("down")
        return None

    def notifier(self, sm: async_sessionmaker[AsyncSession], row: RoutineRow) -> Any:
        settings = SimpleNamespace(
            direct_message_policies={row.tenant_id: DirectMessagePolicy(mode="members")},
            billing=SimpleNamespace(markup=Decimal("1.0")),
        )
        return build_owner_notifier(
            sm=sm, settings=cast(Settings, settings), anthropic=cast(Any, None), discord_dm=self.dm
        )


@pytest.mark.parametrize(
    ("case", "reason", "written"),
    [
        ("agent", TerminationReason.UPSTREAM, "agent"),
        ("write_fails", TerminationReason.UPSTREAM, "plain"),
        ("budget_refused", TerminationReason.ADMISSION_CHANNEL_BUDGET_EXCEEDED, "plain"),
        ("refused_fire", "owner_not_allowed", "plain"),
        ("send_fails", TerminationReason.UPSTREAM, None),
    ],
)
async def test_the_owner_gets_the_agents_words_or_the_plain_card(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    reason: str,
    written: str | None,
) -> None:
    """A refusal gets the card with no call; a failed send is an issue, never resent."""
    row = await _routine(db_session, "discord")
    world = _World(monkeypatch, write_fails=case == "write_fails")
    world.send_fails = case == "send_fails"

    await world.notifier(db_session_factory, row)(row, reason, True)

    calls = 1 if case in ("agent", "write_fails", "send_fails") else 0
    assert len(world.calls) == calls, "the agent writes only for a run that failed"
    if calls:
        call = world.calls[0]
        assert call["system"] == "You are Analyst." and "tools" not in call, "its own, no tools"
        assert "Weekly readout" in call["prompt"], "from the facts"
    assert world.metered == (["owner_notice"] if case in ("agent", "send_fails") else [])
    [notice] = world.sent
    assert (notice.text is not None) == (case in ("agent", "send_fails")), "the card otherwise"
    assert notice.title == 'Your routine "Weekly readout" failed'
    codes = [(e.code, e.reason) for e in world.events]
    if written is None:
        assert codes == [("routine.owner_notice_failed", "ConnectError")]
    else:
        assert codes == [], "a told owner is a log line, not an issue"
    assert "routine.owner_notified" not in CATALOG, "no catalog code for a sent notice"
    assert "routine.owner_notice_failed" in CATALOG, "a notice that failed stays an issue"


async def test_a_slack_owners_notice_waits_for_the_slack_adapter_and_goes_once(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = await _routine(db_session, "slack")
    world = _World(monkeypatch)

    await world.notifier(db_session_factory, row)(row, TerminationReason.UPSTREAM, False)
    assert world.sent == [], "the scheduler cannot reach a Slack person"

    sent = await poll_owner_notices_once(db_session_factory, platform="slack", send=world.dm)
    again = await poll_owner_notices_once(db_session_factory, platform="slack", send=world.dm)

    assert (sent, again) == (1, 0), "taken once"
    assert [n.text for n in world.sent] == ["Your weekly readout failed."]
    assert world.events == [], "a sent notice records no issue"


@pytest.mark.parametrize(
    ("owner", "dms", "policy", "skipped"),
    [
        (None, True, "members", "no_owner"),
        ("123", False, "members", "dms_off"),
        ("123", True, "disabled", "dm_policy_denies"),
    ],
)
async def test_where_dms_are_closed_the_owner_is_not_told_and_the_reason_is_logged(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    owner: str | None,
    dms: bool,
    policy: str,
    skipped: str,
) -> None:
    row = await _routine(db_session, "slack", owner=owner, dms=dms)
    world = _World(monkeypatch)
    notify = world.notifier(db_session_factory, row)
    if policy == "disabled":
        settings = SimpleNamespace(
            direct_message_policies={row.tenant_id: DirectMessagePolicy(mode="disabled")},
            billing=SimpleNamespace(markup=Decimal("1.0")),
        )
        notify = build_owner_notifier(
            sm=db_session_factory,
            settings=cast(Settings, settings),
            anthropic=cast(Any, None),
            discord_dm=world.dm,
        )

    with structlog.testing.capture_logs() as logs:
        await notify(row, TerminationReason.UPSTREAM, False)
    sent = await poll_owner_notices_once(db_session_factory, platform="slack", send=world.dm)

    assert (sent, world.sent, world.calls) == (0, [], []), "nothing written or sent"
    assert [log["reason"] for log in logs if log["event"] == "routine.owner_notice_skipped"] == [
        skipped
    ]


async def test_an_owner_the_agent_no_longer_admits_gets_the_card_and_no_paid_call(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The access gate runs before the model call, so nothing is billed or written for them."""
    row = await _routine(db_session, "discord")
    await set_grant(
        db_session,
        tenant_id=row.tenant_id,
        agent_name="analyst",
        who="listed",
        user_ids=["999"],
        group_ids=[],
        set_by=None,
        now=datetime.now(UTC),
    )
    await db_session.commit()
    world = _World(monkeypatch)

    await world.notifier(db_session_factory, row)(row, TerminationReason.UPSTREAM, True)

    assert (world.calls, world.metered) == ([], []), "no model call for a closed owner"
    assert [n.text for n in world.sent] == [None], "the plain card goes, never agent text"


async def test_a_notice_that_hangs_is_skipped_once_and_the_next_still_goes(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hung chat API stalls the poll loop for the timeout only, and is never retried."""
    first = await _routine(db_session, "slack")
    world = _World(monkeypatch)
    second = await create_routine(
        db_session,
        tenant_id=first.tenant_id,
        created_by_user_id="123",
        agent_id="ag_1",
        agent_name="analyst",
        name="Daily digest",
        cron_expr="0 9 * * *",
        timezone_="UTC",
        trigger_message="digest",
        next_fire_at=datetime.now(UTC),
        destination_kind="channel",
        destination_id="456",
    )
    await db_session.commit()
    notify = world.notifier(db_session_factory, first)
    await notify(first, TerminationReason.UPSTREAM, False)
    await notify(second, TerminationReason.UPSTREAM, False)
    monkeypatch.setattr(owner_notices, "_SEND_TIMEOUT_S", 0.05)
    hung = [True, False]

    async def send(row: RoutineRow, notice: OwnerNotice) -> str | None:
        if hung.pop(0):
            await asyncio.sleep(5)
        return await world.dm(row, notice)

    with structlog.testing.capture_logs() as logs:
        taken = await poll_owner_notices_once(db_session_factory, platform="slack", send=send)
        again = await poll_owner_notices_once(db_session_factory, platform="slack", send=send)

    assert (taken, again) == (2, 0), "both notices were taken once and none is resent"
    assert len(world.sent) == 1, "the second notice still went after the first hung"
    assert [log["reason"] for log in logs if log["event"] == "routine.owner_notice_skipped"] == [
        "timeout"
    ], "the timed-out notice counts as sent and logs why"
    assert world.events == [], "a timeout is no issue"
