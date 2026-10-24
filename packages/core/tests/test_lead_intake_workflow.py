"""Durable coordinator parity against isolated Postgres and synthetic providers."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sized
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.github_credentials import build_multifernet
from daimon.core.lead_intake import (
    IntakeConfig,
    IntakeResolution,
    LeadClassification,
    LeadDraft,
    LeadSource,
    LeadSourceKind,
)
from daimon.core.lead_intake_access import (
    LeadReadDenied,
    LeadReader,
    LeadReadTarget,
    NativeChannel,
    authorize_lead_read,
)
from daimon.core.lead_intake_service import (
    EffectRequest,
    IntakeAccess,
    IntakeService,
    VerifiedSource,
)
from daimon.core.membership import Membership
from daimon.core.stores.lead_intake import IntakeStore
from daimon.testing.factories import make_tenant
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures/lead_intake/classification.json").read_text()
)


@pytest.fixture
async def lead_sessions(
    db_nullpool_engine: AsyncEngine, db_clean: None
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(db_nullpool_engine, expire_on_commit=False)


class FakePorts:
    def __init__(self, source_text: str) -> None:
        self.source_text = source_text
        self.remote: dict[str, str] = {}
        self.writes: list[str] = []
        self.fail_step: str | None = None
        self.fail_before = False
        self.recipients: tuple[str, ...] = ("intake@example.test",)
        self.scan_fail = False

    async def source(
        self, config: IntakeConfig, source: LeadSource, actor_id: str | None
    ) -> VerifiedSource:
        return VerifiedSource(
            source=source,
            text=self.source_text,
            raw="sanitized raw text",
            recipients=self.recipients,
        )

    async def recover(self, config: IntakeConfig, effect: EffectRequest) -> str | None:
        return effect.payload.get("selected") or self.remote.get(effect.key)

    def replay_safe(self, effect: EffectRequest) -> bool:
        return False

    async def write(self, config: IntakeConfig, effect: EffectRequest) -> str:
        if self.fail_before and self.fail_step == effect.step:
            raise TimeoutError("synthetic response loss")
        identity = str(len(self.remote) + 100)
        self.remote[effect.key] = identity
        self.writes.append(effect.step)
        if self.fail_step == effect.step:
            raise TimeoutError("synthetic response loss")
        return identity

    async def scan(
        self, config: IntakeConfig, start: datetime, end: datetime, cursor: str | None
    ) -> tuple[list[str], str | None]:
        if cursor and self.scan_fail:
            raise TimeoutError("synthetic page failure")
        return (["archived-a"], "next") if not cursor else (["archived-b"], None)


async def noop(access: IntakeAccess) -> None:
    pass


async def readable(target: LeadReadTarget) -> None:
    pass


def draft(classification: str = "project", message: str = "immutable-1") -> LeadDraft:
    return LeadDraft(
        source=LeadSource(
            kind=LeadSourceKind.GMAIL, mailbox="owner@example.test", message_id=message
        ),
        classification=LeadClassification(classification),
        opportunity_description="Forecast project",
        thread_title="Synthetic",
        thread_content="Research summary\nDRAFT reply: Thank you for your enquiry.",
        enquirer_email="ada@synthetic.example",
        company_name="Synthetic",
        company_domain="synthetic.example",
        enquirer_evidence="From: Ada <ada@synthetic.example>",
        company_evidence="Ada, Synthetic synthetic.example",
    )


async def setup(
    sessions: async_sessionmaker[AsyncSession], mode: str = "capture"
) -> tuple[IntakeStore, IntakeService, FakePorts]:
    async with sessions() as session, session.begin():
        tenant = await make_tenant(session)
    store = IntakeStore(sessions, build_multifernet((Fernet.generate_key().decode(),)), tenant.id)
    config = IntakeConfig(
        agent_id=uuid4(),
        guild_id="1",
        channel_id="2",
        source_connection_id="gmail-1",
        gmail_mailbox="owner@example.test",
        crm_connection_id="hubspot-1",
        portal_id="3",
        recipients=("intake@example.test",),
        mode=mode,
        reconciliation_start=datetime.now(UTC) - timedelta(days=1),
    )
    await store.configure(config, {"hubspot_access_token": "synthetic"})
    ports = FakePorts(FIXTURES[-2]["text"])
    return store, IntakeService(store, ports, noop, authorize_read=readable), ports


@pytest.mark.parametrize("row", FIXTURES, ids=[r["kind"] for r in FIXTURES])
async def test_fake_model_classification_replay(
    lead_sessions: async_sessionmaker[AsyncSession], row: dict[str, str]
) -> None:
    store, service, ports = await setup(lead_sessions, "active")
    ports.source_text = row["text"]
    discovered = await service.scan()
    assert isinstance(discovered["discovered"], list)
    assert "archived-a" in discovered["discovered"]
    fetched = await service.read_source("archived-a")
    assert fetched.text == row["text"] and fetched.raw is None
    # Replay the model decision from the text actually fetched through the
    # configured mailbox, without a separately provisioned model credential.
    classification = next(
        item["classification"] for item in FIXTURES if item["text"] == fetched.text
    )
    result = await service.submit(draft(classification, "archived-a"), "7")
    if row["classification"] == "other":
        assert result.status == "rejected"
        assert ports.writes == [], "Junk cannot post, create CRM or archive"
    else:
        assert result.status == "completed"
        assert ports.writes == [
            "starter",
            "thread",
            "dossier:0",
            "archive",
            "company",
            "contact",
            "deal",
            "association:companies",
            "association:contacts",
        ]
    assert (await store.get(result.id)).submission == result.submission


async def test_capture_and_replay_preserve_original(
    lead_sessions: async_sessionmaker[AsyncSession],
) -> None:
    store, service, ports = await setup(lead_sessions)
    first = await service.submit(draft(), "7")
    ports.source_text = "changed provider text"
    replay = await service.submit(draft("other"), "8")
    assert replay.id == first.id
    assert replay.submission == first.submission
    assert replay.actor_id == "7"
    assert ports.writes == []
    assert len(await store.list()) == 1


@pytest.mark.parametrize(
    "step",
    [
        "starter",
        "thread",
        "dossier:0",
        "archive",
        "company",
        "contact",
        "deal",
        "association:companies",
    ],
)
async def test_uncertain_response_recovers_without_duplicates(
    lead_sessions: async_sessionmaker[AsyncSession], step: str
) -> None:
    store, service, ports = await setup(lead_sessions, "active")
    ports.fail_step = step
    first = await service.submit(draft(), "7")
    assert first.status == "pending"
    ports.fail_step = None
    results = await asyncio.gather(service.resume(first.id), service.resume(first.id))
    assert all(r.status in {"pending", "completed"} for r in results)
    assert (await service.resume(first.id)).status == "completed"
    assert ports.writes.count(step) == 1
    assert len(ports.writes) == 9
    assert (await store.get(first.id)).status == "completed"


async def test_uncertain_absence_never_blindly_reposts(
    lead_sessions: async_sessionmaker[AsyncSession],
) -> None:
    _, service, ports = await setup(lead_sessions, "active")
    ports.fail_step, ports.fail_before = "starter", True
    first = await service.submit(draft(), "7")
    ports.fail_step = None
    retry = await service.resume(first.id)
    assert retry.status == "review"
    assert retry.error_code == "uncertain_write"
    assert ports.writes == []


async def test_recipient_rejection_precedes_acceptance(
    lead_sessions: async_sessionmaker[AsyncSession],
) -> None:
    store, service, ports = await setup(lead_sessions, "active")
    ports.recipients = ("unrelated@example.test",)
    with pytest.raises(ValueError, match="recipient_mismatch"):
        await service.submit(draft(), "7")
    assert await store.list() == []
    assert ports.writes == []


async def test_identity_review_still_delivers_draft(
    lead_sessions: async_sessionmaker[AsyncSession],
) -> None:
    _, service, ports = await setup(lead_sessions, "active")
    ports.source_text = "Uncertain original sender"
    result = await service.submit(draft(), "7")
    assert result.status == "review"
    assert result.error_code == "identity_review"
    assert ports.writes == ["starter", "thread", "dossier:0", "archive"]


async def test_crm_paused_delivers_and_archives(
    lead_sessions: async_sessionmaker[AsyncSession],
) -> None:
    _, service, ports = await setup(lead_sessions, "crm_paused")
    result = await service.submit(draft(), "7")
    assert result.status == "pending"
    assert ports.writes == ["starter", "thread", "dossier:0", "archive"]


async def test_retention_keeps_only_tombstone_and_tenant_isolation(
    lead_sessions: async_sessionmaker[AsyncSession],
) -> None:
    store, service, ports = await setup(lead_sessions, "active")
    first = await service.submit(draft(), "7")
    async with lead_sessions() as session:
        row = (
            await session.execute(
                text(
                    "SELECT encrypted_payload, encrypted_raw FROM lead_intakes WHERE tenant_id=:tenant"
                ),
                {"tenant": store.tenant_id},
            )
        ).one()
        assert b"synthetic.example" not in bytes(row.encrypted_payload)
        assert row.encrypted_raw is not None
    await store.purge(datetime.now(UTC) + timedelta(days=8))
    async with lead_sessions() as session:
        assert (
            await session.execute(
                text("SELECT encrypted_raw FROM lead_intakes WHERE tenant_id=:tenant"),
                {"tenant": store.tenant_id},
            )
        ).scalar() is None
    other, _, _ = await setup(lead_sessions)
    with pytest.raises(ValueError, match="intake_not_found"):
        await other.get(first.id)
    assert await other.effect(first.id, "starter") is None
    await store.purge(datetime.now(UTC) + timedelta(days=91))
    tombstone = await store.get(first.id)
    assert tombstone.submission is None
    assert tombstone.progress.discord_message_id is None
    assert tombstone.actor_id is None
    replay = await service.submit(draft(), "7")
    assert replay.id == first.id and replay.status == "expired"
    assert ports.writes.count("starter") == 1
    assert await store.effect(first.id, "starter") is None


async def test_scan_failure_preserves_discoveries_without_checkpoint_skip(
    lead_sessions: async_sessionmaker[AsyncSession],
) -> None:
    store, service, ports = await setup(lead_sessions)
    ports.scan_fail = True
    failed = await service.scan()
    assert failed["error_code"] == "mailbox_scan_failed"
    assert failed["checkpoint"] is None
    assert failed["discovered"] == ["archived-a"]
    ports.scan_fail = False
    result = await service.scan()
    assert result["caught_up"] is True
    state = await store.mailbox((await store.configuration())[0])
    assert state.discovered == ["archived-a", "archived-b"]


async def test_resolution_audits_without_changing_source(
    lead_sessions: async_sessionmaker[AsyncSession],
) -> None:
    store, service, ports = await setup(lead_sessions, "active")
    ports.source_text = "Uncertain original sender"
    first = await service.submit(draft(), "7")
    result = await service.resolve(
        first.id,
        IntakeResolution(
            company_id="200",
            contact_id="201",
            evidence="Sales resolver verified original identity against source email.",
        ),
        "8",
    )
    assert result.status == "completed"
    assert result.submission == first.submission
    async with lead_sessions() as session:
        audit = (
            await session.execute(
                text(
                    "SELECT encrypted_resolution FROM lead_resolution_audit WHERE tenant_id=:tenant"
                ),
                {"tenant": store.tenant_id},
            )
        ).scalar_one()
    assert '"actor": "8"' in store.open(audit)
    assert "verified original identity" in store.open(audit)


async def test_privacy_erasure_and_retention_maintenance(
    lead_sessions: async_sessionmaker[AsyncSession],
) -> None:
    from daimon.core.stores.lead_intake import count_actor, erase_actor, purge_expired

    store, service, _ = await setup(lead_sessions, "active")
    first = await service.submit(draft(), "7")
    async with lead_sessions() as session, session.begin():
        assert await count_actor(session, store.tenant_id, "7") == 1
        assert await count_actor(session, store.tenant_id, "8") == 0
        assert await erase_actor(session, store.tenant_id, "7") == 1
    assert (await store.get(first.id)).status == "expired"
    assert await store.effect(first.id, "starter") is None
    second = await service.submit(draft(message="immutable-2"), "8")
    async with lead_sessions() as session, session.begin():
        await purge_expired(session, datetime.now(UTC) + timedelta(days=91))
    assert (await store.get(second.id)).submission is None


async def test_mode_cutover_and_configuration_revision(
    lead_sessions: async_sessionmaker[AsyncSession],
) -> None:
    store, service, ports = await setup(lead_sessions)
    first = await service.submit(draft(), "7")
    config, credentials = await store.configuration()
    await store.configure(config.model_copy(update={"mode": "active"}), credentials)
    assert (await service.resume(first.id)).status == "completed"
    assert ports.writes.count("starter") == 1
    with pytest.raises(ValueError, match="configuration_revision_must_increase"):
        await store.configure(config.model_copy(update={"channel_id": "different"}), credentials)


async def test_queue_and_scan_ignore_completed_history(
    lead_sessions: async_sessionmaker[AsyncSession],
) -> None:
    store, service, _ = await setup(lead_sessions, "active")
    first = await service.submit(draft(), "7")
    assert await store.list(pending_only=True) == []
    assert (await store.health())["status_counts"] == {"completed": 1}
    assert await store.list(after_id=first.id) == []
    config, credentials = await store.configuration()
    await store.configure(config.model_copy(update={"mode": "capture"}), credentials)
    pending = await service.submit(draft(message="archived-a"), "7")
    assert [r.id for r in await store.list(pending_only=True)] == [pending.id]
    scanned = await service.scan()
    assert scanned["discovered"] == ["archived-b"]


async def test_live_revocation_stops_next_effect(
    lead_sessions: async_sessionmaker[AsyncSession],
) -> None:
    store, _, ports = await setup(lead_sessions, "active")

    async def reauthorize(access: IntakeAccess) -> None:
        if access.create_thread:
            raise PermissionError("revoked")

    service = IntakeService(store, ports, reauthorize, authorize_read=readable)
    result = await service.submit(draft(), "7")
    assert result.status == "pending"
    assert ports.writes == ["starter"]


async def test_execution_grant_is_live_tenant_bound_and_revoked(
    lead_sessions: async_sessionmaker[AsyncSession],
) -> None:
    from daimon.core.lead_intake_execution import current_intake_execution
    from daimon.core.stores.lead_intake import execution_is_active, routine_execution
    from daimon.testing.factories import make_routine

    async with lead_sessions() as session, session.begin():
        tenant = await make_tenant(session)
        routine = await make_routine(session, tenant=tenant, created_by_user_id="7")
    async with routine_execution(lead_sessions, tenant.id, routine.id):
        grant = current_intake_execution.get()
        assert grant is not None
        async with lead_sessions() as session:
            assert await execution_is_active(session, tenant.id, routine.id, grant[1])
            assert not await execution_is_active(session, uuid4(), routine.id, grant[1])
    async with lead_sessions() as session:
        assert not await execution_is_active(session, tenant.id, routine.id, grant[1])
    assert current_intake_execution.get() is None


async def test_partial_dossier_recovery_precedes_archive(
    lead_sessions: async_sessionmaker[AsyncSession],
) -> None:
    _, service, ports = await setup(lead_sessions, "active")
    prepared = draft().model_copy(update={"thread_content": "DRAFT reply. " * 400})
    ports.fail_step = "dossier:1"
    first = await service.submit(prepared, "7")
    assert first.status == "pending"
    assert "archive" not in ports.writes
    ports.fail_step = None
    assert (await service.resume(first.id)).status == "completed"
    assert ports.writes.count("dossier:0") == ports.writes.count("dossier:1") == 1
    assert ports.writes.index("dossier:2") < ports.writes.index("archive")


async def test_resolution_confirmation_is_tenant_opt_in(
    lead_sessions: async_sessionmaker[AsyncSession],
) -> None:
    from daimon.core.stores.lead_intake import confirmation_tools

    assert await confirmation_tools(lead_sessions, uuid4()) == frozenset()
    store, _, _ = await setup(lead_sessions)
    assert await confirmation_tools(lead_sessions, store.tenant_id) == frozenset(
        {"lead_intake_resolve"}
    )


async def test_resolution_cannot_approve_another_configuration_revision(
    lead_sessions: async_sessionmaker[AsyncSession],
) -> None:
    store, service, ports = await setup(lead_sessions)
    record = await service.submit(draft(), "7")
    config, credentials = await store.configuration()
    await store.configure(
        config.model_copy(update={"revision": 2, "portal_id": "different"}), credentials
    )
    with pytest.raises(ValueError, match="config_revision_review"):
        await service.resolve(
            record.id,
            IntakeResolution(
                company_id="200", evidence="Verified company selection for the original portal."
            ),
            "7",
        )
    assert ports.writes == []


async def test_manual_source_replay_is_independent_of_gmail_connection(
    lead_sessions: async_sessionmaker[AsyncSession],
) -> None:
    store, service, ports = await setup(lead_sessions)
    prepared = draft().model_copy(
        update={
            "source": LeadSource(kind=LeadSourceKind.MANUAL, channel_id="500", message_id="600")
        }
    )
    first = await service.submit(prepared, "7")
    config, credentials = await store.configuration()
    await store.configure(
        config.model_copy(update={"revision": 2, "source_connection_id": "gmail-new"}), credentials
    )
    replay = await service.submit(prepared, "7")
    assert replay.id == first.id
    assert ports.writes == []


async def test_gmail_tombstone_survives_connection_rename(
    lead_sessions: async_sessionmaker[AsyncSession],
) -> None:
    store, service, ports = await setup(lead_sessions, "active")
    first = await service.submit(draft(), "7")
    assert first.status == "completed"
    config, credentials = await store.configuration()
    renamed = config.model_copy(update={"revision": 2, "source_connection_id": "gmail-renamed"})
    await store.configure(renamed, credentials)
    assert await store.seen_source(renamed, draft().source.message_id)
    assert (await service.submit(draft(), "7")).id == first.id
    await store.purge(datetime.now(UTC) + timedelta(days=91))
    replay = await service.submit(draft(), "7")
    assert replay.id == first.id and replay.submission is None
    assert ports.writes.count("starter") == 1


async def test_resolution_cannot_recreate_data_after_actor_erasure(
    lead_sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    from daimon.core.lead_intake import IntakeRecord
    from daimon.core.stores.lead_intake import erase_actor

    store, service, _ = await setup(lead_sessions)
    record = await service.submit(draft(), "7")
    reached, release = asyncio.Event(), asyncio.Event()
    original = store.audit

    async def paused_audit(
        current: IntakeRecord,
        actor: str,
        before: str,
        after: str,
        *,
        effect_resolution: tuple[str, str | None] | None = None,
    ) -> None:
        reached.set()
        await release.wait()
        await original(current, actor, before, after, effect_resolution=effect_resolution)

    monkeypatch.setattr(store, "audit", paused_audit)
    resolution = asyncio.create_task(
        service.resolve(
            record.id,
            IntakeResolution(company_id="201", evidence="Human checked exact company record 201."),
            "8",
        )
    )
    await asyncio.wait_for(reached.wait(), 10)
    try:
        async with lead_sessions() as session, session.begin():
            assert await erase_actor(session, store.tenant_id, "7") == 1
    finally:
        release.set()
    with pytest.raises(ValueError, match="intake_erased_or_expired"):
        await resolution
    with pytest.raises(ValueError, match="intake_erased_or_expired"):
        await store.intent(record.id, "company", "stale")
    config, _ = await store.configuration()
    with pytest.raises(ValueError, match="intake_erased_or_expired"):
        await store.opportunity(config, record, "201", "stale personal description")
    async with lead_sessions() as session:
        for table in ("lead_resolution_audit", "lead_intake_effects", "lead_opportunities"):
            assert (
                await session.scalar(
                    text(f"SELECT count(*) FROM {table} WHERE tenant_id=:tenant"),
                    {"tenant": store.tenant_id},
                )
                == 0
            )
    assert (await store.get(record.id)).submission is None


async def test_unclassified_discovery_expires_from_health_and_resume(
    lead_sessions: async_sessionmaker[AsyncSession],
) -> None:
    store, service, _ = await setup(lead_sessions)
    scanned = await service.scan()
    assert isinstance(scanned["discovered"], Sized)
    assert len(scanned["discovered"]) == 2
    assert (await store.health())["unclassified_count"] == 2
    async with lead_sessions() as session, session.begin():
        state = await session.scalar(
            text("SELECT encrypted_state FROM lead_mailbox_states WHERE tenant_id=:tenant"),
            {"tenant": store.tenant_id},
        )
        assert state is not None and "archived-a" not in store.open(state)
        await session.execute(
            text(
                "UPDATE lead_mailbox_messages SET expires_at=now()-interval '1 second' WHERE tenant_id=:tenant"
            ),
            {"tenant": store.tenant_id},
        )
    assert (await store.health())["unclassified_count"] == 0
    assert (await store.mailbox((await store.configuration())[0])).discovered == []
    assert (await service.scan())["discovered"] == []
    await store.purge(datetime.now(UTC))
    config, _ = await store.configuration()
    assert await store.seen_source(config, "archived-a")
    async with lead_sessions() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT encrypted_message_id, disposition, mailbox_key FROM lead_mailbox_messages WHERE tenant_id=:tenant"
                ),
                {"tenant": store.tenant_id},
            )
        ).all()
        assert len(rows) == 2
        assert all(row == (None, "expired", "") for row in rows)


async def test_preclassification_read_requires_live_discovery(
    lead_sessions: async_sessionmaker[AsyncSession],
) -> None:
    store, service, ports = await setup(lead_sessions)
    with pytest.raises(ValueError, match="unexpired_discovery_required"):
        await service.read_source("undiscovered")
    await service.scan()
    assert (await service.read_source("archived-a")).raw is None
    await store.purge(datetime.now(UTC) + timedelta(days=91))
    with pytest.raises(ValueError, match="unexpired_discovery_required"):
        await service.read_source("archived-a")
    assert ports.writes == []


async def test_thread_write_revocation_between_dossier_chunks_stops_resume(
    lead_sessions: async_sessionmaker[AsyncSession],
) -> None:
    store, _, ports = await setup(lead_sessions, "active")
    checked: list[str] = []

    async def authorize(access: IntakeAccess) -> None:
        if access.thread_id:
            checked.append(access.thread_id)
        if access.write and access.thread_id and "dossier:0" in ports.writes:
            raise PermissionError("thread_writers_none")

    service = IntakeService(store, ports, authorize, authorize_read=readable)
    result = await service.submit(
        draft().model_copy(update={"thread_content": "DRAFT: " + "x" * 4000}), "7"
    )
    assert result.status == "pending"
    assert checked and set(checked) == {result.progress.discord_thread_id}
    assert ports.writes == ["starter", "thread", "dossier:0"]
    with pytest.raises(PermissionError, match="thread_writers_none"):
        await service.resume(result.id)
    assert ports.writes == ["starter", "thread", "dossier:0"]


def _member_of(*readable: str) -> FakeChannels:
    return FakeChannels(frozenset(readable))


class FakeChannels:
    """Native facts where only `readable` channels grant view and history."""

    def __init__(self, readable: frozenset[str]) -> None:
        self.readable = readable

    async def channel(self, channel_id: str) -> NativeChannel | None:
        return NativeChannel(
            id=channel_id,
            guild_id="1",
            parent_id=None,
            category_id=None,
            is_text=True,
            is_thread=False,
            private_thread=False,
            everyone_can_view=False,
            can_view=channel_id in self.readable,
            can_read_history=channel_id in self.readable,
            in_thread_or_manages=True,
        )


def _as_reader(
    store: IntakeStore, service: IntakeService, native: FakeChannels, *, user: str = "7"
) -> IntakeService:
    """The same service, reading as `user` through the real read rule."""

    async def authorize_read(target: LeadReadTarget) -> None:
        config, _ = await store.configuration()
        reader = LeadReader(
            tenant_id=store.tenant_id,
            platform_user_id=user,
            facts=Membership(is_member=True, role_ids=("42",)),
            policy=TenantAccessPolicy(invoker_role_ids=("42",)),
            config=config,
            agent_id=config.agent_id,
        )
        await authorize_lead_read(target, reader, native)

    return IntakeService(store, service.ports, noop, authorize_read=authorize_read)


async def test_reconfiguring_the_sales_channel_never_moves_old_readers(
    lead_sessions: async_sessionmaker[AsyncSession],
) -> None:
    store, service, _ = await setup(lead_sessions)
    record = await service.submit(draft(), "7")
    assert record.destination is not None and record.destination.channel_id == "2"
    config, credentials = await store.configuration()
    await store.configure(config.model_copy(update={"revision": 2, "channel_id": "B"}), credentials)
    assert (await store.get(record.id)).destination == record.destination
    a_only = _as_reader(store, service, _member_of("2"))
    b_only = _as_reader(store, service, _member_of("B"))
    assert (await a_only.get(record.id)).submission == record.submission
    assert (await a_only.resume(record.id)).id == record.id
    for read in (b_only.get, b_only.resume):
        with pytest.raises(LeadReadDenied, match="sales_channel_history_required"):
            await read(record.id)
    # New captures snapshot the channel configured when they are accepted.
    newer = await service.submit(draft(message="immutable-2"), "7")
    assert newer.destination is not None and newer.destination.channel_id == "B"
    assert [r.id for r in await store.list(destination_channel_id="2")] == [record.id]
    # Erased or expired, the stub keeps its readers.
    await store.purge(datetime.now(UTC) + timedelta(days=91))
    stub = await store.get(record.id)
    assert stub.submission is None and stub.destination == record.destination
    with pytest.raises(LeadReadDenied):
        await b_only.get(record.id)
    assert (await a_only.get(record.id)).status == "expired"


async def test_adoption_never_moves_a_lead_to_another_channel(
    lead_sessions: async_sessionmaker[AsyncSession],
) -> None:
    store, service, _ = await setup(lead_sessions)
    record = await service.submit(draft(), "7")
    config, credentials = await store.configuration()
    moved = config.model_copy(update={"revision": 2, "channel_id": "B"})
    await store.configure(moved, credentials)
    with pytest.raises(ValueError, match="destination_change_not_adoptable"):
        await store.adopt(record.id, 2)
    assert (await store.get(record.id)).config_revision == 1
    await store.configure(
        moved.model_copy(update={"revision": 3, "channel_id": "2", "portal_id": "9"}), credentials
    )
    await store.adopt(record.id, 3)
    assert (await store.get(record.id)).config_revision == 3


async def test_a_resolver_added_later_cannot_read_or_resolve_without_history(
    lead_sessions: async_sessionmaker[AsyncSession],
) -> None:
    store, service, ports = await setup(lead_sessions)
    record = await service.submit(draft(), "7")
    config, credentials = await store.configuration()
    await store.configure(
        config.model_copy(update={"revision": 2, "resolver_user_ids": ("9",)}), credentials
    )
    late = _as_reader(store, service, _member_of(), user="9")
    with pytest.raises(LeadReadDenied):
        await late.get(record.id)
    with pytest.raises(LeadReadDenied):
        await late.resolve(
            record.id,
            IntakeResolution(company_id="200", evidence="Verified company selection by sales."),
            "9",
        )
    assert ports.writes == []
    assert (await store.get(record.id)).progress.selected_company_id is None


class ManyMessages(FakePorts):
    async def scan(
        self, config: IntakeConfig, start: datetime, end: datetime, cursor: str | None
    ) -> tuple[list[str], str | None]:
        page = int(cursor or 0)
        ids = [f"m-{n:03}" for n in range(page * 60, min(page * 60 + 60, 150))]
        return ids, (str(page + 1) if page < 2 else None)


async def test_renaming_the_connection_strands_no_discovery(
    lead_sessions: async_sessionmaker[AsyncSession],
) -> None:
    store, _, _ = await setup(lead_sessions)
    ports = ManyMessages("Genuine project enquiry")
    service = IntakeService(store, ports, noop, authorize_read=readable)
    first = await service.scan()
    assert isinstance(first["discovered"], Sized)
    assert first["has_more_discovered"] is True and len(first["discovered"]) == 100
    accepted = await service.submit(
        draft().model_copy(
            update={
                "source": LeadSource(
                    kind=LeadSourceKind.GMAIL, mailbox="owner@example.test", message_id="m-000"
                )
            }
        ),
        "7",
    )
    expiry = "SELECT source_digest, expires_at FROM lead_mailbox_messages WHERE tenant_id=:t"
    async with lead_sessions() as session:
        before = dict((await session.execute(text(expiry), {"t": store.tenant_id})).tuples().all())
    config, credentials = await store.configuration()
    renamed = config.model_copy(update={"revision": 2, "source_connection_id": "gmail-renamed"})
    await store.configure(renamed, credentials)
    again = await service.scan()
    pending = (await store.mailbox(renamed)).discovered
    assert sorted(pending) == [f"m-{n:03}" for n in range(1, 150)]
    assert again["discovered"] == pending[:100] and again["has_more_discovered"] is True
    assert (await store.health())["unclassified_count"] == 149
    async with lead_sessions() as session:
        after = dict((await session.execute(text(expiry), {"t": store.tenant_id})).tuples().all())
    assert after == before, "a rename extends no discovery's lifetime"
    replay = await service.submit(
        draft().model_copy(update={"source": accepted.submission.source}),  # type: ignore[union-attr]
        "7",
    )
    assert replay.id == accepted.id


async def test_manually_selected_company_without_name_uses_lead_deal_name(
    lead_sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, service, ports = await setup(lead_sessions, "active")
    effects: list[EffectRequest] = []
    original = ports.write

    async def capture(config: IntakeConfig, effect: EffectRequest) -> str:
        effects.append(effect)
        return await original(config, effect)

    monkeypatch.setattr(ports, "write", capture)
    result = await service.submit(draft().model_copy(update={"company_name": None}), "7")
    assert result.status == "review"
    result = await service.resolve(
        result.id,
        IntakeResolution(
            company_id="201",
            contact_id="202",
            evidence="Verified company 201 and contact 202 by hand.",
        ),
        "7",
    )
    assert result.status == "completed", result.error_code
    deal = next(effect for effect in effects if effect.step == "deal")
    assert deal.payload["name"] == "Lead: Forecast project"
