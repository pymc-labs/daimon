"""The mirror outbox: queueing once, claiming per session, settling, pausing and purge."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from daimon.core._models import MirrorDelivery
from daimon.core.config import MirrorsSettings
from daimon.core.mirror_delivery import (
    MirrorJob,
    MirrorOutcome,
    MirrorPoster,
    poll_mirror_deliveries_once,
    record_conversation_event,
    sweep_mirror_deliveries,
)
from daimon.core.mirror_events import ActorRef, ConversationEventV1, SourceRef
from daimon.core.privacy import collect_purge_preview
from daimon.core.purge import purge_account
from daimon.core.stores import mirrors as store
from daimon.core.stores.domain import MirrorDeliveryRow, MirrorRow
from daimon.core.stores.tenants import set_provision_status
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_NOW = datetime(2026, 10, 1, 12, tzinfo=UTC)
_LEASE = timedelta(minutes=2)
_MAX_AGE = timedelta(minutes=30)


async def _mirror(
    session: AsyncSession, tenant_id: uuid.UUID, agent_id: uuid.UUID, *, dest_id: str = "300"
) -> MirrorRow:
    return await store.upsert_mirror(
        session,
        tenant_id=tenant_id,
        name="qa",
        source_agent_id=agent_id,
        source_agent_name="helper",
        source_agent_names=("helper",),
        dest_platform="discord",
        dest_kind="channel",
        dest_id=dest_id,
        consent_note="askers agreed in the kickoff call",
        max_answer_chars=None,
        account_id=None,
        now=_NOW,
    )


def _event(
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    *,
    session_key: str = "sesn_1",
    account_id: uuid.UUID | None = None,
) -> ConversationEventV1:
    return ConversationEventV1(
        id=uuid.uuid4(),
        surface="hub",
        occurred_at=_NOW,
        tenant_id=tenant_id,
        agent_id=agent_id,
        source=SourceRef(channel_id=None, session_key=session_key),
        actor=ActorRef(account_id=account_id, display_name="Ada"),
        question="q",
        answer="a",
    )


async def _claim(
    session: AsyncSession, *, now: datetime = _NOW, owner: str = "w1"
) -> list[MirrorDeliveryRow]:
    return await store.claim_mirror_deliveries(
        session, platform="discord", owner=owner, now=now, lease=_LEASE, max_pending_age=_MAX_AGE
    )


async def test_an_event_is_queued_once_and_only_for_its_tenants_mirrors(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    agent = uuid.uuid4()
    tenant = await make_tenant(db_session)
    other = await make_tenant(db_session, workspace_id="other-guild")
    mirror = await _mirror(db_session, tenant.id, agent)
    await _mirror(db_session, other.id, agent)
    await db_session.commit()
    event = _event(tenant.id, agent)

    first = await record_conversation_event(db_session_factory, event, now=_NOW)
    await record_conversation_event(db_session_factory, event, now=_NOW)

    assert [m.id for m in first] == [mirror.id], "another tenant's mirror never matches"
    claimed = await _claim(db_session)
    assert [row.event_id for row in claimed] == [event.id], "a repeated event is queued once"


async def test_one_claim_per_session_at_a_time_and_settling_drops_the_payload(
    db_session: AsyncSession,
) -> None:
    agent = uuid.uuid4()
    tenant = await make_tenant(db_session)
    mirror = await _mirror(db_session, tenant.id, agent)
    first, second = _event(tenant.id, agent), _event(tenant.id, agent)
    for offset, event in enumerate((first, second)):
        await store.insert_deliveries(
            db_session, event=event, mirror_ids=[mirror.id], now=_NOW + timedelta(seconds=offset)
        )

    claimed = await _claim(db_session, now=_NOW + timedelta(seconds=5))
    assert [row.event_id for row in claimed] == [first.id], "a session's oldest post goes first"
    assert await _claim(db_session, now=_NOW + timedelta(seconds=6)) == [], (
        "nothing more is claimed for a session while one post is in flight"
    )
    assert await store.settle_mirror_delivery(
        db_session,
        claimed[0].id,
        owner="w1",
        status="delivered",
        dest_thread_id="900",
        now=_NOW,
    )
    payload = select(MirrorDelivery.payload).where(MirrorDelivery.id == claimed[0].id)
    assert await db_session.scalar(payload) is None, "a settled post keeps no question or answer"
    later = await _claim(db_session, now=_NOW + timedelta(seconds=7))
    assert [row.event_id for row in later] == [second.id], "the next post follows the settle"
    thread = await store.get_session_thread(db_session, mirror_id=mirror.id, session_key="sesn_1")
    assert thread == "900", "the session's thread is reused"


async def test_expired_leases_stale_posts_and_paused_mirrors_never_post(
    db_session: AsyncSession,
) -> None:
    agent = uuid.uuid4()
    tenant = await make_tenant(db_session)
    mirror = await _mirror(db_session, tenant.id, agent)
    await store.insert_deliveries(
        db_session, event=_event(tenant.id, agent), mirror_ids=[mirror.id], now=_NOW
    )
    await _claim(db_session)

    later = _NOW + _LEASE + timedelta(seconds=1)
    assert await _claim(db_session, now=later, owner="w2") == [], (
        "an expired lease is settled, not retried: the first owner may have posted"
    )
    await store.insert_deliveries(
        db_session,
        event=_event(tenant.id, agent, session_key="s2"),
        mirror_ids=[mirror.id],
        now=_NOW,
    )
    assert await _claim(db_session, now=_NOW + _MAX_AGE + timedelta(seconds=1)) == [], (
        "a post older than the pending limit is dropped"
    )
    await store.insert_deliveries(
        db_session,
        event=_event(tenant.id, agent, session_key="s3"),
        mirror_ids=[mirror.id],
        now=_NOW,
    )
    await store.mark_paused(db_session, mirror.id, reason="destination_unavailable", now=_NOW)
    assert await _claim(db_session) == [], "a paused mirror's posts are skipped"
    notes = await db_session.scalars(
        select(MirrorDelivery.note).where(MirrorDelivery.status == "skipped")
    )
    assert sorted(map(str, notes)) == ["interrupted", "paused", "stale"], (
        "each settles with its reason"
    )


async def test_a_poster_pause_pauses_the_mirror(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    agent = uuid.uuid4()
    tenant = await make_tenant(db_session)
    mirror = await _mirror(db_session, tenant.id, agent)
    await db_session.commit()
    await record_conversation_event(db_session_factory, _event(tenant.id, agent), now=_NOW)
    jobs: list[MirrorJob] = []

    async def post(job: MirrorJob) -> MirrorOutcome:
        jobs.append(job)
        return MirrorOutcome(status="skipped", note="refused", pause_reason="writers_none")

    count = await poll_mirror_deliveries_once(
        db_session_factory, platform="discord", post=post, settings=MirrorsSettings(), now=_NOW
    )

    assert count == 1 and jobs[0].event.question == "q", "the poster gets the stored event"
    paused = await store.get_mirror(db_session, mirror.id)
    assert paused is not None and paused.status == "paused", "the mirror is paused"
    assert paused.paused_reason == "writers_none", paused


async def test_replacing_a_mirrors_agent_or_destination_drops_what_it_queued(
    db_session: AsyncSession,
) -> None:
    agent = uuid.uuid4()
    tenant = await make_tenant(db_session)
    mirror = await _mirror(db_session, tenant.id, agent)
    for session_key in ("s1", "s2"):
        await store.insert_deliveries(
            db_session,
            event=_event(tenant.id, agent, session_key=session_key),
            mirror_ids=[mirror.id],
            now=_NOW,
        )
    await _mirror(db_session, tenant.id, agent)
    assert len(await _claim(db_session, owner="w0")) == 2, "an unchanged mirror keeps its queue"
    await store.insert_deliveries(
        db_session, event=_event(tenant.id, agent), mirror_ids=[mirror.id], now=_NOW
    )

    await _mirror(db_session, tenant.id, uuid.uuid4(), dest_id="301")

    assert await _claim(db_session, now=_NOW + timedelta(seconds=1)) == [], (
        "posts queued for the old agent and destination never go to the new ones"
    )
    rows = (await db_session.scalars(select(MirrorDelivery))).all()
    assert {(row.status, row.note) for row in rows} == {("skipped", "replaced")}, rows
    assert all(row.payload is None for row in rows), "a dropped post keeps no question or answer"


async def test_a_post_of_another_agent_is_never_handed_to_the_poster(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    mirror = await _mirror(db_session, tenant.id, uuid.uuid4())
    await store.insert_deliveries(
        db_session, event=_event(tenant.id, uuid.uuid4()), mirror_ids=[mirror.id], now=_NOW
    )
    await db_session.commit()
    jobs: list[MirrorJob] = []

    async def post(job: MirrorJob) -> MirrorOutcome:
        jobs.append(job)
        return MirrorOutcome(status="delivered")

    await poll_mirror_deliveries_once(
        db_session_factory, platform="discord", post=post, settings=MirrorsSettings(), now=_NOW
    )

    assert jobs == [], "a post queued for one agent is never posted under another"
    note = await db_session.scalar(select(MirrorDelivery.note))
    assert note == "replaced", note


async def test_the_sweep_drops_stale_posts_of_archived_tenants_without_a_poller(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The scheduler's sweep runs with mirrors off and no poller, for every tenant."""
    agent = uuid.uuid4()
    tenant = await make_tenant(db_session)
    mirror = await _mirror(db_session, tenant.id, agent)
    for session_key, age in (("old", _MAX_AGE + timedelta(seconds=1)), ("new", timedelta(0))):
        await store.insert_deliveries(
            db_session,
            event=_event(tenant.id, agent, session_key=session_key),
            mirror_ids=[mirror.id],
            now=_NOW - age,
        )
    await db_session.commit()
    await set_provision_status(db_session_factory, tenant_id=tenant.id, archive=True)

    settled = await sweep_mirror_deliveries(
        db_session_factory, settings=MirrorsSettings(enabled=False), now=_NOW
    )

    assert settled == 1, "only the post past the pending limit is dropped"
    rows = (
        await db_session.scalars(select(MirrorDelivery).order_by(MirrorDelivery.created_at))
    ).all()
    assert [(row.status, row.note, row.payload is None) for row in rows] == [
        ("skipped", "stale", True),
        ("pending", None, False),
    ], rows


async def test_a_poller_whose_lease_ran_out_never_posts_what_another_took_over(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Two pollers overlap: the second interrupts the first's expired claim and
    posts the session's next question; the first must not post the old one late."""
    agent = uuid.uuid4()
    tenant = await make_tenant(db_session)
    mirror = await _mirror(db_session, tenant.id, agent)
    events = {name: _event(tenant.id, agent, session_key=name[0]) for name in ("a1", "b1", "b2")}
    for offset, event in enumerate(events.values()):
        await store.insert_deliveries(
            db_session, event=event, mirror_ids=[mirror.id], now=_NOW + timedelta(seconds=offset)
        )
    await db_session.commit()
    names = {event.id: name for name, event in events.items()}
    posted: list[tuple[str, str]] = []
    settings = MirrorsSettings(lease_s=int(_LEASE.total_seconds()))

    def poster(owner: str) -> MirrorPoster:
        async def post(job: MirrorJob) -> MirrorOutcome:
            posted.append((owner, names[job.event.id]))
            if owner == "w1":
                await poll_mirror_deliveries_once(
                    db_session_factory,
                    platform="discord",
                    post=poster("w2"),
                    settings=settings,
                    now=_NOW + _LEASE + timedelta(minutes=1),
                    owner="w2",
                )
            return MirrorOutcome(status="delivered")

        return post

    await poll_mirror_deliveries_once(
        db_session_factory,
        platform="discord",
        post=poster("w1"),
        settings=settings,
        now=_NOW + timedelta(seconds=5),
        owner="w1",
    )

    assert posted == [("w1", "a1"), ("w2", "b2")], (
        "the first poller's interrupted claim is never posted after the second took over"
    )


async def test_purge_deletes_the_persons_queued_questions(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    agent = uuid.uuid4()
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    mirror = await _mirror(db_session, tenant.id, agent)
    await store.insert_deliveries(
        db_session,
        event=_event(tenant.id, agent, account_id=account.id),
        mirror_ids=[mirror.id],
        now=_NOW,
    )
    await db_session.commit()

    preview = await collect_purge_preview(sm=db_session_factory, account_id=account.id)
    report = await purge_account(sm=db_session_factory, account_id=account.id)

    assert preview.mirror_deliveries.count == 1, "the preview counts the queued question"
    assert report.db.mirror_deliveries == 1, "purge deletes it"
    assert await store.count_for_account(db_session, account_id=account.id) == 0
