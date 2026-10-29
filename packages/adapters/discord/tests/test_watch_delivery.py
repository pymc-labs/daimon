"""Watch reports into the owner's DM or a new thread: owner only, rechecked per chunk,
one thread per fire, and never picked up by the thread wake path."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.discord import watch_access, watch_delivery
from daimon.adapters.discord.watch_access import require_watch_parent_access
from daimon.adapters.discord.watch_delivery import (
    deliver_dm_report,
    open_report_thread,
    report_thread_name,
)
from daimon.core.config import DirectMessagePolicy
from daimon.core.continuity.wakes import list_dispatchable_wakes, list_due_wake_threads
from daimon.core.stores import watches
from daimon.core.stores.domain import TaskContinuationRow, WatchFiringRow, WatchRow
from daimon.core.stores.task_continuations import get_continuation, list_due_watch_report_rows
from daimon.core.stores.thread_sessions import get_live_thread_session, mark_dead
from daimon.core.turn.errors import AdmissionDenied
from sqlalchemy import text

from .watch_fixtures import WatchDiscordRest, enrolled_page_watch

NOW = datetime(2026, 10, 29, tzinfo=UTC)


def _watch(delivery: str = "dm", **changes: object) -> WatchRow:
    return WatchRow(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        owner_account_id=uuid.uuid4(),
        owner_external_id="555",
        platform="discord",
        parent_channel_id="4241",
        thread_id="4242",
        agent_id="ag_target",
        agent_name="target-agent",
        session_id=None,
        delivery=delivery,  # type: ignore[arg-type]
        instructions="Tell me about new   rows.",
        producer="watch:x",
        page_id=uuid.uuid4(),
        revision=1,
        source="poll",
        status="active",
        expires_at=NOW,
        max_turns_per_day=24,
        max_fires=None,
        fire_count=0,
        baseline={},
        rule={},
        source_state={},
        lease_owner=None,
        lease_expires_at=None,
        next_check_at=None,
        backoff_until=None,
        consecutive_errors=0,
    ).model_copy(update=changes)


def _row(watch: WatchRow) -> TaskContinuationRow:
    return TaskContinuationRow(
        id=uuid.uuid4(),
        tenant_id=watch.tenant_id,
        platform="discord",
        thread_id=watch.thread_id,
        parent_channel_id=watch.parent_channel_id,
        requester_account_id=watch.owner_account_id,
        requester_external_user_id=watch.owner_external_id,
        target_ma_agent_id=watch.agent_id,
        target_name=watch.agent_name,
        requested_work="Page evidence is ready.",
        reason="watch_report",
        status="claimed",
        skip_reason=None,
        idempotency_key=uuid.uuid4(),
        created_at=NOW,
        claimed_at=NOW,
        delivered_at=None,
    )


class _Sessions:
    """A sessionmaker stand-in: every store call it would reach is patched."""

    def __call__(self):
        return self._session()

    def begin(self):
        return self._session()

    @asynccontextmanager
    async def _session(self):
        yield MagicMock()


@pytest.fixture
def dm_store(monkeypatch):
    watch = _watch()
    state = SimpleNamespace(watch=watch, enabled=True, seed="seed", paused=[])
    firing = WatchFiringRow(
        wake_key=uuid.uuid4(),
        watch_id=watch.id,
        revision=1,
        generation=0,
        events=[],
        omitted=0,
        created_at=NOW,
    )

    async def pause(db, watch_id, *, reason="owner_paused"):
        state.paused.append(reason)

    monkeypatch.setattr(watch_delivery, "get_firing", AsyncMock(return_value=firing))
    monkeypatch.setattr(watch_delivery, "get_watch", AsyncMock(side_effect=lambda *_: state.watch))
    monkeypatch.setattr(
        watch_delivery,
        "get_tenant",
        AsyncMock(return_value=SimpleNamespace(archived_at=None, external_id="710000099")),
    )
    monkeypatch.setattr(
        watch_delivery, "dm_enabled", AsyncMock(side_effect=lambda *_a, **_k: state.enabled)
    )
    monkeypatch.setattr(
        watch_delivery, "watch_seed", AsyncMock(side_effect=lambda *_a, **_k: state.seed)
    )
    monkeypatch.setattr(watch_delivery, "pause_watch", pause)
    return state


def _ports(*, text="report", present=None, allowed=("555",)):
    dm = SimpleNamespace(send=AsyncMock())
    return dict(
        run_turn=AsyncMock(return_value=text),
        open_dm=AsyncMock(return_value=dm),
        member_present=AsyncMock(side_effect=present or (lambda *_: True)),
        dm_policy=lambda _tenant: DirectMessagePolicy(
            mode="allowlist", recipient_ids=list(allowed)
        ),
    ), dm


async def test_dm_report_goes_only_to_the_owner_with_mentions_off(dm_store):
    ports, dm = _ports(text="Two new rows. <@999>")
    sent = await deliver_dm_report(_Sessions(), _row(dm_store.watch), **ports)
    assert sent == 1
    ports["open_dm"].assert_awaited_once_with(710000099, 555)
    kwargs = dm.send.await_args.kwargs
    assert kwargs["content"] == "Two new rows. <@999>"
    assert kwargs["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()


async def test_dm_disabled_runs_no_turn_and_pauses_the_watch(dm_store):
    dm_store.enabled = False
    ports, dm = _ports()
    with pytest.raises(AdmissionDenied):
        await deliver_dm_report(_Sessions(), _row(dm_store.watch), **ports)
    ports["run_turn"].assert_not_awaited()
    dm.send.assert_not_awaited()
    assert dm_store.paused == ["Watch paused: Daimon DMs are off here."]


async def test_blocked_recipient_gets_nothing_and_the_watch_pauses(dm_store):
    ports, dm = _ports(allowed=("777",))
    with pytest.raises(AdmissionDenied):
        await deliver_dm_report(_Sessions(), _row(dm_store.watch), **ports)
    ports["run_turn"].assert_not_awaited()
    ports["open_dm"].assert_not_awaited()
    dm.send.assert_not_awaited()
    assert dm_store.paused


async def test_lost_membership_between_chunks_stops_the_rest(dm_store):
    answers = iter([True, True, False])
    ports, dm = _ports(text="word " * 1200, present=lambda *_: next(answers))
    with pytest.raises(AdmissionDenied):
        await deliver_dm_report(_Sessions(), _row(dm_store.watch), **ports)
    assert dm.send.await_count == 1
    assert not dm_store.paused  # the next poll's fresh membership check pauses it


async def test_dm_turned_off_mid_delivery_stops_and_pauses(dm_store):
    ports, dm = _ports(text="word " * 1200)

    async def send(**_kwargs):
        dm_store.enabled = False

    dm.send.side_effect = send
    with pytest.raises(AdmissionDenied):
        await deliver_dm_report(_Sessions(), _row(dm_store.watch), **ports)
    assert dm.send.await_count == 1
    assert dm_store.paused


async def test_paused_watch_sends_no_further_chunks(dm_store):
    ports, dm = _ports(text="word " * 1200)

    async def send(**_kwargs):
        dm_store.seed = None

    dm.send.side_effect = send
    with pytest.raises(AdmissionDenied):
        await deliver_dm_report(_Sessions(), _row(dm_store.watch), **ports)
    assert dm.send.await_count == 1


async def test_thread_delivery_never_reaches_the_dm_path(dm_store):
    dm_store.watch = _watch("thread", session_id="sess_watch")
    ports, _dm = _ports()
    with pytest.raises(AdmissionDenied):
        await deliver_dm_report(_Sessions(), _row(dm_store.watch), **ports)
    ports["run_turn"].assert_not_awaited()


def test_report_thread_name_is_short_and_from_the_watch():
    assert report_thread_name(_watch()) == "Watch report: Tell me about new rows."
    long = report_thread_name(_watch(instructions="x" * 500))
    assert len(long) == 90 and long.endswith("…")


def _thread(thread_id: int, parent_id: int = 4241) -> MagicMock:
    thread = MagicMock(spec=discord.Thread)
    thread.id = thread_id
    thread.parent_id = parent_id
    return thread


@pytest.fixture
def thread_store(monkeypatch):
    watch = _watch("new_thread")
    state = SimpleNamespace(watch=watch, recorded=None)

    async def firing(*_args):
        return WatchFiringRow(
            wake_key=uuid.uuid4(),
            watch_id=watch.id,
            revision=1,
            generation=0,
            events=[],
            omitted=0,
            created_at=NOW,
            delivery_thread_id=state.recorded,
        )

    async def record(db, wake_key, thread_id):
        state.recorded = state.recorded or thread_id
        return state.recorded

    monkeypatch.setattr(watch_delivery, "get_firing", firing)
    monkeypatch.setattr(watch_delivery, "get_watch", AsyncMock(return_value=watch))
    monkeypatch.setattr(watch_delivery, "record_delivery_thread", record)
    monkeypatch.setattr(watch_delivery, "require_watch_parent_access", AsyncMock())
    return state


async def test_new_thread_report_makes_one_public_thread_and_a_retry_reuses_it(thread_store):
    parent = MagicMock(spec=discord.TextChannel)
    made = _thread(9001)
    parent.create_thread = AsyncMock(return_value=made)
    client = MagicMock()
    client.get_channel = MagicMock(return_value=parent)
    client.fetch_channel = AsyncMock(return_value=made)
    row = _row(thread_store.watch)

    first = await open_report_thread(_Sessions(), row, client=client)
    retry = await open_report_thread(_Sessions(), row, client=client)

    assert first is made and retry is made
    parent.create_thread.assert_awaited_once()
    kwargs = parent.create_thread.await_args.kwargs
    assert kwargs["type"] is discord.ChannelType.public_thread
    assert kwargs["name"] == "Watch report: Tell me about new rows."
    client.fetch_channel.assert_awaited_once_with(9001)
    assert thread_store.recorded == "9001"


async def test_recorded_thread_moved_elsewhere_is_refused(thread_store):
    thread_store.recorded = "9001"
    client = MagicMock()
    client.fetch_channel = AsyncMock(return_value=_thread(9001, parent_id=1))
    with pytest.raises(AdmissionDenied):
        await open_report_thread(_Sessions(), _row(thread_store.watch), client=client)


@pytest.fixture
def parent_rest(monkeypatch):
    paused: list[str] = []

    async def pause(db, watch_id, *, reason="owner_paused"):
        paused.append(reason)

    monkeypatch.setattr(
        watch_access,
        "get_tenant",
        AsyncMock(return_value=SimpleNamespace(archived_at=None, external_id="710000099")),
    )
    monkeypatch.setattr(
        watch_access, "protection_state", AsyncMock(return_value=SimpleNamespace(may_post=True))
    )
    monkeypatch.setattr(
        watch_access, "get_firing", AsyncMock(return_value=SimpleNamespace(watch_id=uuid.uuid4()))
    )
    monkeypatch.setattr(watch_access, "pause_watch", pause)
    client = discord.Client(intents=discord.Intents.none())
    rest = WatchDiscordRest(client)
    rest.permissions = discord.Permissions(
        view_channel=True,
        send_messages=True,
        create_public_threads=True,
        send_messages_in_threads=True,
    )
    return SimpleNamespace(client=client, rest=rest, paused=paused)


async def test_owner_who_may_start_threads_passes(parent_rest):
    await require_watch_parent_access(_Sessions(), _row(_watch("new_thread")), parent_rest.client)
    assert not parent_rest.paused


@pytest.mark.parametrize("lost", ["create_public_threads", "send_messages", "view_channel"])
async def test_owner_losing_thread_rights_pauses_the_watch(parent_rest, lost):
    setattr(parent_rest.rest.permissions, lost, False)
    with pytest.raises(AdmissionDenied):
        await require_watch_parent_access(
            _Sessions(), _row(_watch("new_thread")), parent_rest.client
        )
    assert len(parent_rest.paused) == 1


async def test_channel_rules_blocking_output_pause_the_watch(parent_rest, monkeypatch):
    monkeypatch.setattr(
        watch_access, "protection_state", AsyncMock(return_value=SimpleNamespace(may_post=False))
    )
    with pytest.raises(AdmissionDenied):
        await require_watch_parent_access(
            _Sessions(), _row(_watch("new_thread")), parent_rest.client
        )
    assert parent_rest.paused


# Database-backed: the frozen firing SQL, the queue split and the seed shapes.


@pytest.mark.fresh_schema
@pytest.mark.parametrize("delivery", ["dm", "new_thread"])
async def test_report_wake_is_never_listed_by_the_thread_path(
    db_session_factory, monkeypatch, delivery
):
    factory, now, tenant, _, _, watch, key = await enrolled_page_watch(
        db_session_factory, monkeypatch, source="poll", delivery=delivery
    )
    assert watch.delivery == delivery and watch.session_id is None
    async with factory() as db:
        row = await get_continuation(db, idempotency_key=key)
        reports = await list_due_watch_report_rows(
            db, platform="discord", now=datetime.now(UTC), max_attempts=5, limit=10
        )
    assert row is not None and row.reason == "watch_report"
    assert row.requested_work == "Page evidence is ready."
    assert row.thread_id == watch.thread_id  # the place it was made, not where it reports
    assert [report.idempotency_key for report in reports] == [key]
    assert not await list_dispatchable_wakes(
        factory,
        tenant_id=tenant.id,
        platform="discord",
        thread_id=watch.thread_id,
        now=datetime.now(UTC),
    )
    assert not await list_due_wake_threads(factory, platform="discord", now=datetime.now(UTC))


@pytest.mark.fresh_schema
async def test_seed_refuses_a_reason_that_does_not_match_delivery(db_session_factory, monkeypatch):
    factory, now, *_rest, watch, key = await enrolled_page_watch(
        db_session_factory, monkeypatch, source="poll", delivery="dm"
    )
    async with factory() as db:
        row = await get_continuation(db, idempotency_key=key)
        assert row is not None
        assert await watches.watch_seed(db, row, now=datetime.now(UTC)) is not None
        forged = row.model_copy(update={"reason": "watch_fired"})
        assert await watches.watch_seed(db, forged, now=datetime.now(UTC)) is None
        elsewhere = row.model_copy(update={"thread_id": "999"})
        assert await watches.watch_seed(db, elsewhere, now=datetime.now(UTC)) is None


@pytest.mark.fresh_schema
async def test_thread_watch_refuses_a_report_reason(db_session_factory, monkeypatch):
    factory, now, *_rest, watch, key = await enrolled_page_watch(
        db_session_factory, monkeypatch, source="poll"
    )
    async with factory() as db:
        row = await get_continuation(db, idempotency_key=key)
        assert row is not None and row.reason == "watch_fired"
        assert await watches.watch_seed(db, row, now=datetime.now(UTC)) is not None
        forged = row.model_copy(update={"reason": "watch_report"})
        assert await watches.watch_seed(db, forged, now=datetime.now(UTC)) is None


@pytest.mark.fresh_schema
@pytest.mark.parametrize("delivery", ["thread", "dm"])
async def test_firing_sql_checks_the_live_session_only_for_thread_delivery(
    db_session_factory, monkeypatch, delivery
):
    factory, now, tenant, principal, mapping, watch, _key = await enrolled_page_watch(
        db_session_factory, monkeypatch, source="poll", delivery=delivery
    )
    async with factory.begin() as db:
        live = await get_live_thread_session(
            db,
            tenant_id=tenant.id,
            platform="discord",
            thread_id=watch.thread_id,
            account_id=principal.account_id,
        )
        assert live is not None
        await mark_dead(db, id=live.id)
        await db.execute(
            text("UPDATE watches SET next_check_at = now() WHERE id = :id"), {"id": watch.id}
        )
    async with factory.begin() as db:
        claimed = await watches.claim_poll_watches(db)
    assert claimed
    lease = claimed[0]["lease"]
    if delivery == "thread":
        with pytest.raises(Exception, match="poll binding refused"):
            async with factory.begin() as db:
                await watches.finish_poll(db, watch, lease, value={"n": 2}, payload='{"n":2}')
    else:
        async with factory.begin() as db:
            wake = await watches.finish_poll(db, watch, lease, value={"n": 2}, payload='{"n":2}')
        assert wake is not None
