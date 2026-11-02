"""Explicit source-loss consent stays with its original immutable backend revision."""

from __future__ import annotations

import asyncio
import importlib.util
import uuid
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.core._models import AgentBackendRevisionRecord, Base, ChannelConfigRevision
from daimon.core.stores import UnflushedReadState, agent_backends, mux_state
from daimon.core.stores.domain import CompatibilityPreview
from daimon.testing.db import DatabasePreflight, require_owned_sessionmaker
from daimon.testing.factories import make_tenant
from mux.contracts.config import ConfigRevision
from mux.contracts.ids import ChannelRef
from mux.errors import ScopeViolation
from mux.profiles import backend_selection
from mux.state.store import ConfigRevisionConflict
from pydantic import ValidationError
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker


@pytest.fixture
async def owned(db_preflight: DatabasePreflight, db_engine: AsyncEngine, db_clean: None):
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    require_owned_sessionmaker(sessions)
    async with sessions.begin() as session:
        tenant = await make_tenant(session)
    channel = ChannelRef(tenant_id=str(tenant.id), platform="discord", channel_id="consent-channel")
    return sessions, channel, uuid.uuid4()


def consent(*, source: str = "a", target: str = "b"):
    return {
        "declared_drops": ["memory", "sandbox"],
        "source_pin": source * 64,
        "target_digest": target * 64,
    }


async def preference(session, channel, agent_id, *, local=1, value=None):
    return await agent_backends.put_revision(
        session,
        tenant_id=uuid.UUID(channel.tenant_id),
        agent_id=agent_id,
        agent_name="source",
        local=local,
        backend="openai",
        previous_local=local - 1 or None,
        preview_digest="preview",
        digest=f"preference-{local}",
        drop_consent=consent() if value is None else value,
    )


def config(channel, local=1):
    return ConfigRevision.create(channel, local, backend_selection("openai"))


async def test_consent_round_trips_through_the_public_revision_reader(owned):
    sessions, channel, agent_id = owned
    async with sessions.begin() as session:
        written = await preference(session, channel, agent_id)
    async with sessions() as session:
        read = await agent_backends.current(session, uuid.UUID(channel.tenant_id), agent_id)
    assert read == written
    assert read.declared_drops == ("memory", "sandbox")
    assert read.drop_consent.source_pin == "a" * 64
    assert read.drop_consent.target_digest == "b" * 64


@pytest.mark.parametrize("changed", [None, {"source_pin": "invalid"}])
async def test_cached_original_cannot_hide_a_persisted_consent_refusal(owned, changed):
    sessions, channel, agent_id = owned
    async with sessions.begin() as db:
        original = await preference(db, channel, agent_id)
        effective = await mux_state.put_config_revision(
            db, config(channel), origin="agent", agent_revision=original
        )
    async with sessions.begin() as db:
        cached = await db.scalar(
            select(AgentBackendRevisionRecord).where(
                AgentBackendRevisionRecord.agent_id == agent_id,
                AgentBackendRevisionRecord.local == original.local,
            )
        )
        await db.execute(
            update(AgentBackendRevisionRecord)
            .where(
                AgentBackendRevisionRecord.agent_id == agent_id,
                AgentBackendRevisionRecord.local == original.local,
            )
            .values(drop_consent=changed)
            .execution_options(synchronize_session=False)
        )
        assert cached.drop_consent == consent()
        assert (
            await agent_backends.current(db, uuid.UUID(channel.tenant_id), agent_id)
        ) == original
        if changed is None:
            persisted = await agent_backends.current(
                db, uuid.UUID(channel.tenant_id), agent_id, local=original.local, persisted=True
            )
            assert persisted.drop_consent is None
            refused = ScopeViolation
        else:
            with pytest.raises(ValidationError):
                await agent_backends.current(
                    db, uuid.UUID(channel.tenant_id), agent_id, local=original.local, persisted=True
                )
            refused = ValidationError
        with pytest.raises(refused):
            await mux_state.get_config_agent_revision(db, effective, agent_id=agent_id)
        assert cached.drop_consent == consent() and not db.dirty


async def test_dirty_original_cannot_replace_its_persisted_pin_or_autoflush(owned):
    sessions, channel, agent_id = owned
    async with sessions.begin() as db:
        original = await preference(db, channel, agent_id)
        effective = await mux_state.put_config_revision(
            db, config(channel), origin="agent", agent_revision=original
        )
    async with sessions() as db:
        cached = await db.scalar(
            select(AgentBackendRevisionRecord).where(
                AgentBackendRevisionRecord.agent_id == agent_id
            )
        )
        cached.drop_consent = consent(source="c")
        with pytest.raises(UnflushedReadState):
            await mux_state.get_config_agent_revision(db, effective, agent_id=agent_id)
        with pytest.raises(UnflushedReadState):
            await agent_backends.current(
                db, uuid.UUID(channel.tenant_id), agent_id, local=original.local, persisted=True
            )
        assert cached in db.dirty and cached.drop_consent == consent(source="c")
        with db.no_autoflush:
            assert (
                await db.scalar(
                    select(AgentBackendRevisionRecord.drop_consent).where(
                        AgentBackendRevisionRecord.agent_id == agent_id
                    )
                )
                == consent()
            )
        await db.rollback()


@pytest.mark.parametrize("deleted", [False, True])
async def test_effective_dirty_or_deleted_proof_source_is_not_flushed_or_overwritten(
    owned, deleted
):
    sessions, channel, agent_id = owned
    async with sessions.begin() as db:
        original = await preference(db, channel, agent_id)
        effective = await mux_state.put_config_revision(
            db, config(channel), origin="agent", agent_revision=original
        )
    async with sessions() as db:
        cached = await db.scalar(
            select(ChannelConfigRevision).where(
                ChannelConfigRevision.tenant_id == uuid.UUID(channel.tenant_id)
            )
        )
        if deleted:
            await db.delete(cached)
        else:
            cached.agent_backend_ref = [str(uuid.uuid4()), original.local]
        with pytest.raises(UnflushedReadState):
            await mux_state.get_config_agent_revision(db, effective, agent_id=agent_id)
        assert cached in (db.deleted if deleted else db.dirty)
        with db.no_autoflush:
            assert await db.scalar(
                select(ChannelConfigRevision.agent_backend_ref).where(
                    ChannelConfigRevision.tenant_id == uuid.UUID(channel.tenant_id)
                )
            ) == [str(agent_id), original.local]
        await db.rollback()


async def test_persisted_revision_read_keeps_pending_objects_unflushed(owned):
    sessions, channel, agent_id = owned
    async with sessions() as db:
        pending = AgentBackendRevisionRecord(
            tenant_id=uuid.UUID(channel.tenant_id),
            agent_id=agent_id,
            local=1,
            backend="openai",
            agent_name="source",
            preview_digest="preview",
            digest="pending",
            previous_local=None,
            drop_consent=consent(),
        )
        db.add(pending)
        assert (
            await agent_backends.current(db, uuid.UUID(channel.tenant_id), agent_id, persisted=True)
            is None
        )
        assert pending in db.new
        assert (
            await agent_backends.current(db, uuid.UUID(channel.tenant_id), agent_id)
        ).digest == "pending"
        assert pending not in db.new
        await db.rollback()


async def test_old_rows_have_no_consent_and_unchanged_serialized_fields(owned):
    sessions, channel, agent_id = owned
    async with sessions.begin() as session:
        value = await agent_backends.put_revision(
            session,
            tenant_id=uuid.UUID(channel.tenant_id),
            agent_id=agent_id,
            agent_name="legacy",
            local=1,
            backend="openai",
            previous_local=None,
            preview_digest="old",
            digest="old",
        )
        revision = await mux_state.put_config_revision(session, config(channel), origin="agent")
    assert value.declared_drops == ()
    assert "drop_consent" not in value.model_dump(mode="json")
    async with sessions() as session:
        assert (
            await mux_state.get_config_agent_revision(session, revision, agent_id=agent_id) is None
        )
        assert (
            await mux_state.effective_revision(session, channel, revision.digest, "agent")
            == revision
        )


async def test_identical_selection_never_aliases_different_consent_revisions(owned):
    sessions, channel, agent_id = owned
    async with sessions.begin() as session:
        original = await preference(session, channel, agent_id)
        later = await preference(session, channel, agent_id, local=2, value=consent(source="c"))
        first = await mux_state.put_config_revision(
            session, config(channel), origin="agent", agent_revision=original
        )
        assert (
            await mux_state.effective_revision(
                session, channel, first.digest, "agent", agent_revision=later
            )
            is None
        )
        assert await mux_state.effective_revision(session, channel, first.digest, "agent") is None
        second = await mux_state.put_config_revision(
            session, config(channel, 2), origin="agent", agent_revision=later
        )
    assert first.digest == second.digest
    async with sessions() as session:
        assert (
            await mux_state.effective_revision(
                session, channel, first.digest, "agent", agent_revision=original
            )
            == first
        )
        assert (
            await mux_state.effective_revision(
                session, channel, first.digest, "agent", agent_revision=later
            )
            == second
        )
        assert (
            await mux_state.get_config_agent_revision(session, first, agent_id=agent_id) == original
        )
        assert (
            await mux_state.get_config_agent_revision(session, second, agent_id=agent_id) == later
        )


async def test_retained_association_never_reads_the_latest_preference(owned):
    sessions, channel, agent_id = owned
    async with sessions.begin() as session:
        original = await preference(session, channel, agent_id)
        retained = await mux_state.put_config_revision(
            session, config(channel), origin="agent", agent_revision=original
        )
        await agent_backends.put_revision(
            session,
            tenant_id=uuid.UUID(channel.tenant_id),
            agent_id=agent_id,
            agent_name="source",
            local=2,
            backend=None,
            previous_local=1,
            preview_digest="clear",
            digest="clear",
        )
    async with sessions() as session:
        latest = await agent_backends.current(session, uuid.UUID(channel.tenant_id), agent_id)
        assert latest.local == 2 and latest.drop_consent is None
        assert (
            await mux_state.get_config_agent_revision(session, retained, agent_id=agent_id)
            == original
        )


async def test_a_config_collision_cannot_replace_its_original_consent(owned):
    sessions, channel, agent_id = owned
    async with sessions.begin() as session:
        original = await preference(session, channel, agent_id)
        later = await preference(session, channel, agent_id, local=2)
        revision = await mux_state.put_config_revision(
            session, config(channel), origin="agent", agent_revision=original
        )
    for candidate in (later, None):
        async with sessions.begin() as session:
            with pytest.raises(ConfigRevisionConflict):
                await mux_state.put_config_revision(
                    session, revision, origin="agent", agent_revision=candidate
                )
    async with sessions() as session:
        assert (
            await mux_state.get_config_agent_revision(session, revision, agent_id=agent_id)
            == original
        )


@pytest.mark.parametrize("change", ["tenant", "origin"])
async def test_both_public_association_writers_refuse_foreign_scope_before_a_write(owned, change):
    sessions, channel, agent_id = owned
    async with sessions.begin() as session:
        original = await preference(session, channel, agent_id)
    addressed = (
        channel.model_copy(update={"tenant_id": str(uuid.uuid4())})
        if change == "tenant"
        else channel
    )
    origin = "channel" if change == "origin" else "agent"
    async with sessions.begin() as session:
        with pytest.raises(ScopeViolation):
            await mux_state.put_config_revision(
                session, config(addressed), origin=origin, agent_revision=original
            )
        with pytest.raises(ScopeViolation):
            await mux_state.effective_revision(
                session, addressed, config(addressed).digest, origin, agent_revision=original
            )
        assert not (await session.scalars(select(ChannelConfigRevision))).all()


@pytest.mark.parametrize(
    "corruption",
    ["foreign_agent", "missing_original", "missing_consent", "bool_local", "extra", "invalid_uuid"],
)
async def test_original_association_corruption_is_typed_refused(owned, corruption):
    sessions, channel, agent_id = owned
    async with sessions.begin() as session:
        original = await preference(session, channel, agent_id)
        revision = await mux_state.put_config_revision(
            session, config(channel), origin="agent", agent_revision=original
        )
        raw = [str(agent_id), 1]
        if corruption == "missing_consent":
            await session.execute(
                update(AgentBackendRevisionRecord)
                .where(AgentBackendRevisionRecord.agent_id == agent_id)
                .values(drop_consent=None)
            )
        else:
            if corruption == "foreign_agent":
                raw[0] = str(uuid.uuid4())
            elif corruption == "missing_original":
                raw[1] = 100
            elif corruption == "bool_local":
                raw[1] = True
            elif corruption == "extra":
                raw.append("unknown")
            else:
                raw[0] = "not-an-id"
            await session.execute(
                update(ChannelConfigRevision)
                .where(ChannelConfigRevision.tenant_id == uuid.UUID(channel.tenant_id))
                .values(agent_backend_ref=raw)
            )
    async with sessions() as session:
        with pytest.raises(ScopeViolation):
            await mux_state.get_config_agent_revision(session, revision, agent_id=agent_id)


async def test_interleaved_identical_config_claims_keep_one_immutable_association(owned):
    sessions, channel, agent_id = owned
    async with sessions.begin() as session:
        one = await preference(session, channel, agent_id)
        two = await preference(session, channel, agent_id, local=2)
    gate = asyncio.Event()

    async def put(original):
        await gate.wait()
        async with sessions.begin() as session:
            try:
                return await mux_state.put_config_revision(
                    session, config(channel), origin="agent", agent_revision=original
                )
            except ConfigRevisionConflict:
                return None

    tasks = [asyncio.create_task(put(original)) for original in (one, two)]
    gate.set()
    results = await asyncio.gather(*tasks)
    assert sum(result is not None for result in results) == 1
    async with sessions() as session:
        winner = await mux_state.get_config_agent_revision(
            session, config(channel), agent_id=agent_id
        )
        assert winner in (one, two)


@pytest.mark.parametrize("invalid", ["instructions", "source_definition", "unknown", "duplicate"])
def test_the_persisted_consent_uses_the_existing_closed_agent_spec_domain(invalid):
    body = consent()
    body["declared_drops"] = [invalid] if invalid != "duplicate" else ["memory", "memory"]
    with pytest.raises(ValidationError):
        CompatibilityPreview(
            tenant_id=uuid.uuid4(),
            agent_id=uuid.uuid4(),
            target_backend="openai",
            setting_revision=0,
            policy_revision="policy",
            unsupported_skills=(),
            unsupported_mcps=(),
            unsupported_capabilities=(),
            shared_thread_limits=(),
            cost_line="offline",
            allowed=True,
            digest="preview",
            drop_consent=body,
        )


def test_a_saved_consent_may_declare_the_saved_keys_lost():
    """`credentials` is a declarable loss: the target runs without the saved keys."""
    body = consent()
    body["declared_drops"] = ["credentials", "memory"]
    preview = CompatibilityPreview(
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        target_backend="openai",
        setting_revision=0,
        policy_revision="policy",
        unsupported_skills=(),
        unsupported_mcps=(),
        unsupported_capabilities=(),
        shared_thread_limits=(),
        cost_line="offline",
        allowed=True,
        digest="preview",
        drop_consent=body,
    )
    assert preview.declared_drops == ("credentials", "memory")


@pytest.mark.fresh_schema
async def test_expand_downgrade_keeps_legacy_columns_and_rows_readable(
    db_preflight, db_session_factory
):
    sessions = db_session_factory
    require_owned_sessionmaker(sessions)
    async with sessions.begin() as session:
        tenant = await make_tenant(session)
    channel = ChannelRef(
        tenant_id=str(tenant.id), platform="discord", channel_id="migration-channel"
    )
    agent_id = uuid.uuid4()
    async with sessions.begin() as session:
        original = await preference(session, channel, agent_id)
        await mux_state.put_config_revision(
            session, config(channel), origin="agent", agent_revision=original
        )
    path = Path(__file__).parents[2] / "alembic/versions/0102_agent_backend_drop_consent.py"
    spec = importlib.util.spec_from_file_location("backend_consent_migration", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    async with sessions.begin() as session:
        connection = await session.connection()

        def downgrade(sync):
            with Operations.context(MigrationContext.configure(sync)):
                migration.downgrade()

        await connection.run_sync(downgrade)
    try:
        async with sessions() as session:
            # Exactly the previous image's columns: no neutral consent reads.
            old = (
                await session.execute(
                    text(
                        "SELECT agent_name, local, backend, previous_local, digest, preview_digest "
                        "FROM agent_backend_revision WHERE agent_id = :agent"
                    ),
                    {"agent": agent_id},
                )
            ).one()
            assert old.agent_name == "source" and old.backend == "openai" and old.local == 1
            retained = (
                await session.execute(text("SELECT revision, origin FROM channel_config_revision"))
            ).one()
            assert retained.origin == "agent"
            assert ConfigRevision.model_validate(retained.revision) == config(channel)
    finally:
        async with sessions.begin() as session:
            connection = await session.connection()

            def upgrade(sync):
                with Operations.context(MigrationContext.configure(sync)):
                    migration.upgrade()
                changes = compare_metadata(
                    MigrationContext.configure(
                        sync,
                        opts={
                            "include_object": lambda obj, name, kind, reflected, comparison: (
                                name in {"agent_backend_revision", "channel_config_revision"}
                                if kind == "table"
                                else obj.table.name
                                in {"agent_backend_revision", "channel_config_revision"}
                            )
                        },
                    ),
                    Base.metadata,
                )
                assert changes == []

            await connection.run_sync(upgrade)
    async with sessions() as session:
        old = await agent_backends.current(session, uuid.UUID(channel.tenant_id), agent_id)
        assert old is not None and old.drop_consent is None
        assert (
            await mux_state.get_config_agent_revision(session, config(channel), agent_id=agent_id)
            is None
        )


async def test_lost_preference_cas_rolls_back_both_consent_and_effective_association(owned):
    sessions, channel, agent_id = owned
    async with sessions.begin() as session:
        old = await agent_backends.put_revision(
            session,
            tenant_id=uuid.UUID(channel.tenant_id),
            agent_id=agent_id,
            agent_name="legacy",
            local=1,
            backend=None,
            previous_local=None,
            preview_digest="old",
            digest="old",
        )
    with pytest.raises(IntegrityError):
        async with sessions.begin() as session:
            candidate = await preference(session, channel, agent_id, local=2)
            await mux_state.put_config_revision(
                session, config(channel), origin="agent", agent_revision=candidate
            )
            await preference(session, channel, agent_id, local=1)
    async with sessions() as session:
        assert await agent_backends.current(session, uuid.UUID(channel.tenant_id), agent_id) == old
        assert not (await session.scalars(select(ChannelConfigRevision))).all()
