"""Tenant-bound encrypted intake persistence and connection-owned recovery claims."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from cryptography.fernet import MultiFernet
from daimon.core._models import (
    LeadIntakeConfig,
    LeadIntakeEffect,
    LeadIntakeExecution,
    LeadIntakeRecord,
    LeadMailboxMessageRecord,
    LeadMailboxScanRecord,
    LeadMailboxState,
    LeadOpportunityRecord,
    LeadResolutionAudit,
)
from daimon.core.github_credentials import decrypt_token, encrypt_token
from daimon.core.lead_intake import (
    IntakeBusy,
    IntakeConfig,
    IntakeRecord,
    LeadDestination,
    LeadEffectState,
    LeadSubmission,
    captured_here,
    normalize_project,
)
from daimon.core.lead_intake_access import LeadReadDenied, LeadReadTarget, read_target
from daimon.core.lead_intake_execution import current_intake_execution
from pydantic import BaseModel, Field
from sqlalchemy import case, delete, func, literal, select, text, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class MailboxState(BaseModel):
    checkpoint: datetime | None = None
    discovered: list[str] = Field(default_factory=list)
    error_code: str | None = None


class IntakeStore:
    """One tenant per instance; all queries retain the tenant predicate."""

    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], fernet: MultiFernet, tenant_id: UUID
    ) -> None:
        self.sessions, self.fernet, self.tenant_id = sessions, fernet, tenant_id

    def seal(self, value: str) -> bytes:
        return encrypt_token(self.fernet, value)

    def open(self, value: bytes) -> str:
        return decrypt_token(self.fernet, value)

    async def configure(self, config: IntakeConfig, credentials: dict[str, str]) -> None:
        """Operator service entrypoint; credentials never enter tool arguments."""
        if not config.gmail_mailbox.strip():
            # Discoveries are keyed by the mailbox, not the connection's name.
            raise ValueError("gmail_mailbox_required")
        values = dict(
            tenant_id=self.tenant_id,
            config=config.model_dump(mode="json"),
            encrypted_credentials=self.seal(json.dumps(credentials)),
        )
        async with self.sessions() as session, session.begin():
            existing = await session.get(LeadIntakeConfig, self.tenant_id, with_for_update=True)
            if existing is not None:
                previous = IntakeConfig.model_validate(existing.config)
                excluded = {"mode", "revision"}
                changed = previous.model_dump(exclude=excluded) != config.model_dump(
                    exclude=excluded
                )
                if config.revision < previous.revision or (
                    changed and config.revision <= previous.revision
                ):
                    raise ValueError("configuration_revision_must_increase")
                if changed:
                    outstanding = await session.scalar(
                        select(LeadIntakeEffect.intake_id)
                        .join(
                            LeadIntakeRecord,
                            (LeadIntakeRecord.tenant_id == LeadIntakeEffect.tenant_id)
                            & (LeadIntakeRecord.id == LeadIntakeEffect.intake_id),
                        )
                        .where(
                            LeadIntakeRecord.tenant_id == self.tenant_id,
                            LeadIntakeRecord.status.in_(("pending", "review")),
                        )
                        .limit(1)
                    )
                    if outstanding is not None:
                        raise ValueError("finish_outstanding_effects_before_reconfiguration")
            await session.execute(
                insert(LeadIntakeConfig)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=["tenant_id"],
                    set_={k: v for k, v in values.items() if k != "tenant_id"},
                )
            )

    async def configuration(self) -> tuple[IntakeConfig, dict[str, str]]:
        async with self.sessions() as session:
            row = await session.get(LeadIntakeConfig, self.tenant_id)
            if row is None:
                raise ValueError("intake_not_configured")
            return IntakeConfig.model_validate(row.config), json.loads(
                self.open(row.encrypted_credentials)
            )

    @asynccontextmanager
    async def claim(self, key: str) -> AsyncIterator[None]:
        # A dedicated transaction owns the lock while each intent/result commits
        # independently. Connection death releases it; successors reconcile intents.
        async with self.sessions() as session, session.begin():
            acquired = await session.scalar(
                text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"lead:{self.tenant_id}:{key}"},
            )
            if not acquired:
                raise IntakeBusy("intake_busy")
            yield

    async def accept(
        self,
        config: IntakeConfig,
        submission: LeadSubmission,
        actor_id: str | None,
        provenance: tuple[UUID, UUID] | None = None,
    ) -> IntakeRecord:
        connection = "gmail" if submission.source.kind == "gmail" else "discord"
        digest = hashlib.sha256(
            f"{self.tenant_id}:{connection}:{submission.source.key}".encode()
        ).hexdigest()
        now = datetime.now(UTC)
        raw = submission.evidence.raw_source
        evidence = submission.evidence.model_copy(update={"raw_source": None})
        submission = submission.model_copy(update={"evidence": evidence})
        record = IntakeRecord(
            id=uuid4(),
            tenant_id=self.tenant_id,
            source_digest=digest,
            status="rejected" if submission.classification == "other" else "pending",
            submission=submission,
            actor_id=actor_id,
            agent_id=config.agent_id,
            routine_id=provenance[0] if provenance else None,
            execution_id=provenance[1] if provenance else None,
            config_revision=config.revision,
            created_at=now,
            destination=LeadDestination(guild_id=config.guild_id, channel_id=config.channel_id),
            expires_at=now + timedelta(days=config.retention_days),
            raw_expires_at=now + timedelta(days=config.raw_retention_days),
        )
        async with self.sessions() as session, session.begin():
            await session.execute(
                insert(LeadIntakeRecord)
                .values(
                    tenant_id=self.tenant_id,
                    id=record.id,
                    source_digest=digest,
                    actor_digest=actor_digest(self.tenant_id, actor_id) if actor_id else None,
                    status=record.status,
                    encrypted_payload=self.seal(record.model_dump_json()),
                    encrypted_raw=self.seal(raw) if raw else None,
                    created_at=now,
                    expires_at=record.expires_at,
                    raw_expires_at=record.raw_expires_at,
                    destination_guild_id=config.guild_id,
                    destination_channel_id=config.channel_id,
                )
                .on_conflict_do_nothing(index_elements=["tenant_id", "source_digest"])
            )
            row = (
                await session.execute(
                    select(LeadIntakeRecord).where(
                        LeadIntakeRecord.tenant_id == self.tenant_id,
                        LeadIntakeRecord.source_digest == digest,
                    )
                )
            ).scalar_one()
            await session.execute(
                update(LeadMailboxMessageRecord)
                .where(
                    LeadMailboxMessageRecord.tenant_id == self.tenant_id,
                    LeadMailboxMessageRecord.source_digest == digest,
                )
                .values(intake_id=row.id, disposition=row.status, encrypted_message_id=None)
            )
            return self._record(row)

    def _record(self, row: LeadIntakeRecord) -> IntakeRecord:
        # Readers come from the capture-time columns, which survive erasure.
        destination = LeadDestination(
            guild_id=row.destination_guild_id, channel_id=row.destination_channel_id
        )
        if row.encrypted_payload is not None and row.expires_at > datetime.now(UTC):
            record = IntakeRecord.model_validate_json(self.open(row.encrypted_payload))
            return record.model_copy(update={"destination": destination})
        return IntakeRecord(
            id=row.id,
            tenant_id=self.tenant_id,
            source_digest=row.source_digest,
            status="expired",
            submission=None,
            destination=destination,
            config_revision=0,
            created_at=row.created_at,
            expires_at=row.expires_at,
            raw_expires_at=row.raw_expires_at,
        )

    def mailbox_key(self, config: IntakeConfig) -> str:
        """Discovery state follows the mailbox, so renaming its connection strands nothing."""
        mailbox = config.gmail_mailbox.strip().lower()
        return hashlib.sha256(f"{self.tenant_id}:gmail:{mailbox}".encode()).hexdigest()

    def source_digest(self, config: IntakeConfig, message_id: str) -> str:
        key = f"gmail:{config.gmail_mailbox.strip().lower()}:{message_id}"
        return hashlib.sha256(f"{self.tenant_id}:gmail:{key}".encode()).hexdigest()

    async def seen_source(self, config: IntakeConfig, message_id: str) -> bool:
        digest = self.source_digest(config, message_id)
        async with self.sessions() as session:
            accepted = await session.scalar(
                select(LeadIntakeRecord.id).where(
                    LeadIntakeRecord.tenant_id == self.tenant_id,
                    LeadIntakeRecord.source_digest == digest,
                )
            )
            expired_discovery = await session.scalar(
                select(LeadMailboxMessageRecord.source_digest).where(
                    LeadMailboxMessageRecord.tenant_id == self.tenant_id,
                    LeadMailboxMessageRecord.source_digest == digest,
                    LeadMailboxMessageRecord.expires_at <= datetime.now(UTC),
                )
            )
            return accepted is not None or expired_discovery is not None

    async def is_discovered(self, config: IntakeConfig, message_id: str) -> bool:
        async with self.sessions() as session:
            identity = await session.scalar(
                select(LeadMailboxMessageRecord.source_digest).where(
                    LeadMailboxMessageRecord.tenant_id == self.tenant_id,
                    LeadMailboxMessageRecord.source_digest
                    == self.source_digest(config, message_id),
                    LeadMailboxMessageRecord.disposition == "discovered",
                    LeadMailboxMessageRecord.encrypted_message_id.is_not(None),
                    LeadMailboxMessageRecord.expires_at > datetime.now(UTC),
                )
            )
            return identity is not None

    async def get(self, intake_id: UUID) -> IntakeRecord:
        async with self.sessions() as session:
            row = await session.get(LeadIntakeRecord, (self.tenant_id, intake_id))
            if row is None:
                raise ValueError("intake_not_found")
            record = self._record(row)
            effects = (
                await session.execute(
                    select(LeadIntakeEffect)
                    .where(
                        LeadIntakeEffect.tenant_id == self.tenant_id,
                        LeadIntakeEffect.intake_id == intake_id,
                    )
                    .order_by(LeadIntakeEffect.created_at)
                )
            ).scalars()
            record.effects = [_effect_state(e) for e in effects] if record.submission else []
            return record

    async def list(
        self,
        limit: int = 100,
        *,
        pending_only: bool = False,
        after_id: UUID | None = None,
        destination_channel_id: str | None = None,
        authorize_read: Callable[[LeadReadTarget], Awaitable[None]] | None = None,
        config_revision: int | None = None,
    ) -> list[IntakeRecord]:
        async with self.sessions() as session:
            query = select(LeadIntakeRecord).where(LeadIntakeRecord.tenant_id == self.tenant_id)
            if destination_channel_id is not None:
                # A prefilter only: every returned record is still authorized.
                query = query.where(
                    LeadIntakeRecord.destination_channel_id == destination_channel_id
                )
            if pending_only:
                query = query.where(
                    LeadIntakeRecord.status.in_(("pending", "review")),
                    LeadIntakeRecord.expires_at > datetime.now(UTC),
                )
            if after_id is not None:
                after = await self.get(after_id)
                query = query.where(
                    tuple_(LeadIntakeRecord.created_at, LeadIntakeRecord.id)
                    > tuple_(literal(after.created_at), literal(after.id))
                )
            # Stream the query and apply the live permission predicate before
            # counting the visible page. Hidden IDs never become client cursors.
            rows = await session.stream_scalars(
                query.order_by(LeadIntakeRecord.created_at, LeadIntakeRecord.id).execution_options(
                    yield_per=100
                )
            )
            records: list[IntakeRecord] = []
            try:
                async for row in rows:
                    record = await self.get(row.id)
                    if config_revision is not None and record.config_revision != config_revision:
                        continue
                    if authorize_read is not None:
                        try:
                            await authorize_read(read_target(record))
                        except LeadReadDenied:
                            continue
                    records.append(record)
                    if len(records) >= min(limit, 100):
                        break
            finally:
                await rows.close()
            return records

    async def health(self) -> dict[str, object]:
        async with self.sessions() as session:
            status = case(
                (LeadIntakeRecord.expires_at <= datetime.now(UTC), "expired"),
                else_=LeadIntakeRecord.status,
            )
            counts = await session.execute(
                select(status, func.count())
                .where(LeadIntakeRecord.tenant_id == self.tenant_id)
                .group_by(status)
            )
            oldest = await session.scalar(
                select(func.min(LeadIntakeRecord.created_at)).where(
                    LeadIntakeRecord.tenant_id == self.tenant_id,
                    LeadIntakeRecord.status.in_(("pending", "review")),
                    LeadIntakeRecord.expires_at > datetime.now(UTC),
                )
            )
            last_scan = await session.scalar(
                select(func.max(LeadMailboxScanRecord.completed_at)).where(
                    LeadMailboxScanRecord.tenant_id == self.tenant_id,
                    LeadMailboxScanRecord.state == "completed",
                )
            )
            unclassified = await session.scalar(
                select(func.count())
                .select_from(LeadMailboxMessageRecord)
                .where(
                    LeadMailboxMessageRecord.tenant_id == self.tenant_id,
                    LeadMailboxMessageRecord.disposition == "discovered",
                    LeadMailboxMessageRecord.expires_at > datetime.now(UTC),
                )
            )
            return {
                "status_counts": dict(counts.tuples().all()),
                "oldest_pending_at": oldest,
                "last_successful_scan_at": last_scan,
                "unclassified_count": unclassified,
            }

    async def save(self, record: IntakeRecord) -> None:
        if record.tenant_id != self.tenant_id:
            raise ValueError("tenant_mismatch")
        async with self.sessions() as session, session.begin():
            await session.execute(
                update(LeadIntakeRecord)
                .where(
                    LeadIntakeRecord.tenant_id == self.tenant_id,
                    LeadIntakeRecord.id == record.id,
                    LeadIntakeRecord.encrypted_payload.is_not(None),
                )
                .values(status=record.status, encrypted_payload=self.seal(record.model_dump_json()))
            )

    async def adopt(self, intake_id: UUID, revision: int) -> None:
        """Rebind unfinished work to an operator-reviewed revision, audited.

        Connections, CRM and mappings may change; the sales channel may not:
        a lead keeps the readers of the channel it was captured for.
        """
        async with self.claim(f"source:{intake_id}"):
            config, _ = await self.configuration()
            record = await self.get(intake_id)
            if revision != config.revision or record.status not in {"pending", "review"}:
                raise ValueError("configuration_or_intake_not_eligible")
            if not captured_here(record, config):
                raise ValueError("destination_change_not_adoptable")
            for step in ("starter", "company", "contact", "deal"):
                if await self.effect(intake_id, step) is not None:
                    raise ValueError("cannot_rebind_started_effects")
            before = str(record.config_revision)
            record.config_revision, record.error_code = revision, None
            await self.audit(record, "deployment-operator", before, config.model_dump_json())

    async def starter_recovery_facts(self, record: IntakeRecord) -> tuple[list[str], bool]:
        async with self.sessions() as session:
            claimed = list(
                await session.scalars(
                    select(LeadIntakeEffect.remote_id).where(
                        LeadIntakeEffect.tenant_id == self.tenant_id,
                        LeadIntakeEffect.intake_id != record.id,
                        LeadIntakeEffect.step == "starter",
                        LeadIntakeEffect.remote_id.is_not(None),
                    )
                )
            )
            peers = (
                await session.execute(
                    select(LeadIntakeRecord)
                    .join(
                        LeadIntakeEffect,
                        (LeadIntakeRecord.tenant_id == LeadIntakeEffect.tenant_id)
                        & (LeadIntakeRecord.id == LeadIntakeEffect.intake_id),
                    )
                    .where(
                        LeadIntakeRecord.tenant_id == self.tenant_id,
                        LeadIntakeRecord.id != record.id,
                        LeadIntakeEffect.step == "starter",
                        LeadIntakeEffect.state == "uncertain",
                    )
                )
            ).scalars()
            title = record.submission.thread_title.strip() if record.submission else ""
            uncertain_peer = False
            for row in peers:
                other = self._record(row)
                if other.submission and other.submission.thread_title.strip() == title:
                    uncertain_peer = True
                    break
            return [value for value in claimed if value is not None], uncertain_peer

    async def effect(self, intake_id: UUID, step: str) -> LeadEffectState | None:
        async with self.sessions() as session:
            row = await session.get(LeadIntakeEffect, (self.tenant_id, intake_id, step))
            return _effect_state(row) if row else None

    async def _lock_live(self, session: AsyncSession, intake_id: UUID) -> None:
        # Erasure takes the same parent-row lock before deleting child rows.
        # A stale resolver/effect snapshot cannot resurrect sensitive children.
        identity = await session.scalar(
            select(LeadIntakeRecord.id)
            .where(
                LeadIntakeRecord.tenant_id == self.tenant_id,
                LeadIntakeRecord.id == intake_id,
                LeadIntakeRecord.encrypted_payload.is_not(None),
                LeadIntakeRecord.expires_at > datetime.now(UTC),
            )
            .with_for_update()
        )
        if identity is None:
            raise ValueError("intake_erased_or_expired")

    async def intent(self, intake_id: UUID, step: str, stable_key: str) -> None:
        async with self.sessions() as session, session.begin():
            await self._lock_live(session, intake_id)
            await session.execute(
                insert(LeadIntakeEffect)
                .values(
                    tenant_id=self.tenant_id,
                    intake_id=intake_id,
                    step=step,
                    stable_key=stable_key,
                    state="uncertain",
                    created_at=datetime.now(UTC),
                )
                .on_conflict_do_nothing()
            )

    async def finish(self, intake_id: UUID, step: str, remote_id: str) -> None:
        async with self.sessions() as session, session.begin():
            await session.execute(
                update(LeadIntakeEffect)
                .where(
                    LeadIntakeEffect.tenant_id == self.tenant_id,
                    LeadIntakeEffect.intake_id == intake_id,
                    LeadIntakeEffect.step == step,
                )
                .values(state="completed", remote_id=remote_id)
            )

    async def audit(
        self,
        record: IntakeRecord,
        actor_id: str,
        before: str,
        after: str,
        *,
        effect_resolution: tuple[str, str | None] | None = None,
    ) -> None:
        if record.tenant_id != self.tenant_id:
            raise ValueError("tenant_mismatch")
        async with self.sessions() as session, session.begin():
            await self._lock_live(session, record.id)
            updated = await session.scalar(
                update(LeadIntakeRecord)
                .where(
                    LeadIntakeRecord.tenant_id == self.tenant_id,
                    LeadIntakeRecord.id == record.id,
                    LeadIntakeRecord.encrypted_payload.is_not(None),
                    LeadIntakeRecord.expires_at > datetime.now(UTC),
                )
                .values(status=record.status, encrypted_payload=self.seal(record.model_dump_json()))
                .returning(LeadIntakeRecord.id)
            )
            if updated is None:
                raise ValueError("intake_erased_or_expired")
            if effect_resolution is not None:
                step, remote_id = effect_resolution
                conditions = (
                    LeadIntakeEffect.tenant_id == self.tenant_id,
                    LeadIntakeEffect.intake_id == record.id,
                    LeadIntakeEffect.step == step,
                    LeadIntakeEffect.state == "uncertain",
                )
                if remote_id is None:
                    await session.execute(delete(LeadIntakeEffect).where(*conditions))
                else:
                    await session.execute(
                        update(LeadIntakeEffect)
                        .where(*conditions)
                        .values(state="completed", remote_id=remote_id)
                    )
            session.add(
                LeadResolutionAudit(
                    id=uuid4(),
                    tenant_id=self.tenant_id,
                    intake_id=record.id,
                    encrypted_resolution=self.seal(
                        json.dumps(dict(actor=actor_id, before=before, after=after))
                    ),
                    created_at=datetime.now(UTC),
                )
            )

    async def mailbox(self, config: IntakeConfig) -> MailboxState:
        key = self.mailbox_key(config)
        async with self.sessions() as session:
            row = await session.get(LeadMailboxState, (self.tenant_id, key))
            state = (
                MailboxState.model_validate_json(self.open(row.encrypted_state))
                if row
                else MailboxState()
            )
            # The per-message ledger owns pending IDs and their expiry. Coverage
            # state contains only the checkpoint/error, never a second ID copy.
            encrypted_ids = await session.scalars(
                select(LeadMailboxMessageRecord.encrypted_message_id)
                .where(
                    LeadMailboxMessageRecord.tenant_id == self.tenant_id,
                    LeadMailboxMessageRecord.mailbox_key == key,
                    LeadMailboxMessageRecord.disposition == "discovered",
                    LeadMailboxMessageRecord.expires_at > datetime.now(UTC),
                    LeadMailboxMessageRecord.encrypted_message_id.is_not(None),
                )
                .order_by(
                    LeadMailboxMessageRecord.expires_at, LeadMailboxMessageRecord.source_digest
                )
            )
            state.discovered = [self.open(value) for value in encrypted_ids if value is not None]
            return state

    async def save_mailbox(self, config: IntakeConfig, state: MailboxState) -> None:
        key = self.mailbox_key(config)
        async with self.sessions() as session, session.begin():
            encrypted = self.seal(state.model_copy(update={"discovered": []}).model_dump_json())
            await session.execute(
                insert(LeadMailboxState)
                .values(tenant_id=self.tenant_id, mailbox_key=key, encrypted_state=encrypted)
                .on_conflict_do_update(
                    index_elements=["tenant_id", "mailbox_key"],
                    set_={"encrypted_state": encrypted},
                )
            )

    async def opportunity(
        self, config: IntakeConfig, record: IntakeRecord, company_id: str, description: str
    ) -> str:
        normalized = normalize_project(description)
        choice = (
            f"selected:{record.progress.selected_deal_id}"
            if record.progress.selected_deal_id
            else str(record.id)
            if record.progress.separate_opportunity_confirmed
            else f"{company_id}:{normalized}"
        )
        key = hashlib.sha256(
            f"{self.tenant_id}:{config.crm_connection_id}:{config.portal_id}:{choice}".encode()
        ).hexdigest()
        async with self.sessions() as session, session.begin():
            await self._lock_live(session, record.id)
            await session.execute(
                insert(LeadOpportunityRecord)
                .values(
                    tenant_id=self.tenant_id,
                    identity_digest=key,
                    intake_id=record.id,
                    encrypted_mapping=self.seal(
                        json.dumps(
                            dict(company_id=company_id, description=description, deal_id=None)
                        )
                    ),
                    expires_at=record.expires_at,
                )
                .on_conflict_do_nothing()
            )
        return key

    async def finish_opportunity(
        self, key: str, company_id: str, description: str, deal_id: str
    ) -> None:
        async with self.sessions() as session, session.begin():
            await session.execute(
                update(LeadOpportunityRecord)
                .where(
                    LeadOpportunityRecord.tenant_id == self.tenant_id,
                    LeadOpportunityRecord.identity_digest == key,
                )
                .values(
                    encrypted_mapping=self.seal(
                        json.dumps(
                            dict(company_id=company_id, description=description, deal_id=deal_id)
                        )
                    )
                )
            )

    async def start_scan(self, config: IntakeConfig, start: datetime, end: datetime) -> UUID:
        identity = uuid4()
        async with self.sessions() as session, session.begin():
            session.add(
                LeadMailboxScanRecord(
                    tenant_id=self.tenant_id,
                    id=identity,
                    mailbox_key=self.mailbox_key(config),
                    window_start=start,
                    window_end=end,
                    state="running",
                    discovered_count=0,
                    started_at=datetime.now(UTC),
                )
            )
        return identity

    async def record_discovered(
        self, config: IntakeConfig, message_ids: list[str], scan_id: UUID
    ) -> None:
        async with self.sessions() as session, session.begin():
            for message in message_ids:
                await session.execute(
                    insert(LeadMailboxMessageRecord)
                    .values(
                        tenant_id=self.tenant_id,
                        source_digest=self.source_digest(config, message),
                        mailbox_key=self.mailbox_key(config),
                        encrypted_message_id=self.seal(message),
                        disposition="discovered",
                        expires_at=datetime.now(UTC) + timedelta(days=config.retention_days),
                    )
                    .on_conflict_do_nothing()
                )

            await session.execute(
                update(LeadMailboxScanRecord)
                .where(
                    LeadMailboxScanRecord.tenant_id == self.tenant_id,
                    LeadMailboxScanRecord.id == scan_id,
                )
                .values(discovered_count=LeadMailboxScanRecord.discovered_count + len(message_ids))
            )

    async def finish_scan(self, scan_id: UUID, count: int, error: str | None) -> None:
        async with self.sessions() as session, session.begin():
            await session.execute(
                update(LeadMailboxScanRecord)
                .where(
                    LeadMailboxScanRecord.tenant_id == self.tenant_id,
                    LeadMailboxScanRecord.id == scan_id,
                )
                .values(
                    state="failed" if error else "completed",
                    discovered_count=count,
                    completed_at=datetime.now(UTC),
                    error_code=error,
                )
            )

    async def purge(self, now: datetime) -> None:
        """Erase all lead content/effects/audit, keeping only the source tombstone."""
        async with self.sessions() as session, session.begin():
            await _purge(session, now, self.tenant_id)


def _effect_state(row: LeadIntakeEffect) -> LeadEffectState:
    return LeadEffectState(
        step=row.step,
        stable_key=row.stable_key,
        state=row.state,
        remote_id=row.remote_id,
        created_at=row.created_at,
    )


@asynccontextmanager
async def routine_execution(
    sessions: async_sessionmaker[AsyncSession],
    tenant_id: UUID,
    routine_id: UUID,
    *,
    register: bool = True,
) -> AsyncIterator[None]:
    """A live, expiring grant ends with the scheduler fire, including cancellation."""
    identity = uuid4()
    if register:
        async with sessions() as session, session.begin():
            session.add(
                LeadIntakeExecution(
                    tenant_id=tenant_id,
                    id=identity,
                    routine_id=routine_id,
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            )
    token = current_intake_execution.set((routine_id, identity))
    try:
        yield
    finally:
        current_intake_execution.reset(token)
        if register:
            async with sessions() as session, session.begin():
                await session.execute(
                    delete(LeadIntakeExecution).where(
                        LeadIntakeExecution.tenant_id == tenant_id,
                        LeadIntakeExecution.id == identity,
                    )
                )


async def execution_is_active(
    session: AsyncSession, tenant_id: UUID, routine_id: UUID, execution_id: UUID
) -> bool:
    row = await session.get(LeadIntakeExecution, (tenant_id, execution_id))
    return row is not None and row.routine_id == routine_id and row.expires_at > datetime.now(UTC)


async def purge_expired(session: AsyncSession, now: datetime) -> None:
    """Scheduler maintenance uses only ciphertext expiry; no decryption key needed."""
    await _purge(session, now, None)


# An intake's child rows, deleted in this order by retention and erasure.
_INTAKE_CHILDREN = (
    LeadIntakeEffect,
    LeadResolutionAudit,
    LeadOpportunityRecord,
    LeadMailboxMessageRecord,
)


async def _delete_children(
    session: AsyncSession, tenant_id: UUID | None, ids: list[UUID] | list[tuple[UUID, UUID]]
) -> None:
    """Delete the child rows of `ids`: intake ids in `tenant_id`, or (tenant, id) pairs."""
    for model in _INTAKE_CHILDREN:
        if tenant_id is None:
            await session.execute(
                delete(model).where(tuple_(model.tenant_id, model.intake_id).in_(ids))
            )
        else:
            await session.execute(
                delete(model).where(model.tenant_id == tenant_id, model.intake_id.in_(ids))
            )


async def _purge(session: AsyncSession, now: datetime, tenant_id: UUID | None) -> None:
    """Expire lead content in one tenant, or in every tenant when `tenant_id` is None."""
    await purge_auxiliary(session, now, tenant_id)
    expired: list[UUID] | list[tuple[UUID, UUID]]
    if tenant_id is None:
        expired = list(
            (
                await session.execute(
                    select(LeadIntakeRecord.tenant_id, LeadIntakeRecord.id)
                    .where(LeadIntakeRecord.expires_at <= now)
                    .with_for_update()
                )
            ).tuples()
        )
    else:
        expired = list(
            await session.scalars(
                select(LeadIntakeRecord.id)
                .where(
                    LeadIntakeRecord.tenant_id == tenant_id,
                    LeadIntakeRecord.expires_at <= now,
                )
                .with_for_update()
            )
        )
    await _delete_children(session, tenant_id, expired)
    in_tenant = () if tenant_id is None else (LeadIntakeRecord.tenant_id == tenant_id,)
    await session.execute(
        update(LeadIntakeRecord)
        .where(*in_tenant, LeadIntakeRecord.raw_expires_at <= now)
        .values(encrypted_raw=None)
    )
    await session.execute(
        update(LeadIntakeRecord)
        .where(*in_tenant, LeadIntakeRecord.expires_at <= now)
        .values(encrypted_payload=None, encrypted_raw=None, status="expired", actor_digest=None)
    )


def actor_digest(tenant_id: UUID, actor_id: str) -> str:
    return hashlib.sha256(f"{tenant_id}:{actor_id}".encode()).hexdigest()


async def erase_actor(session: AsyncSession, tenant_id: UUID, actor_id: str) -> int:
    """Privacy erasure removes actor-submitted evidence and its effects/audit."""
    ids = select(LeadIntakeRecord.id).where(
        LeadIntakeRecord.tenant_id == tenant_id,
        LeadIntakeRecord.actor_digest == actor_digest(tenant_id, actor_id),
        LeadIntakeRecord.encrypted_payload.is_not(None),
    )
    identities = list(await session.scalars(ids.with_for_update()))
    await _delete_children(session, tenant_id, identities)
    await session.execute(
        update(LeadIntakeRecord)
        .where(LeadIntakeRecord.tenant_id == tenant_id, LeadIntakeRecord.id.in_(identities))
        .values(encrypted_payload=None, encrypted_raw=None, actor_digest=None, status="expired")
    )
    return len(identities)


async def count_actor(session: AsyncSession, tenant_id: UUID, actor_id: str) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(LeadIntakeRecord)
            .where(
                LeadIntakeRecord.tenant_id == tenant_id,
                LeadIntakeRecord.actor_digest == actor_digest(tenant_id, actor_id),
                LeadIntakeRecord.encrypted_payload.is_not(None),
            )
        )
        or 0
    )


async def configured_routines(
    session: AsyncSession, tenant_ids: set[UUID]
) -> set[tuple[UUID, UUID]]:
    rows = await session.execute(
        select(LeadIntakeConfig.tenant_id, LeadIntakeConfig.config).where(
            LeadIntakeConfig.tenant_id.in_(tenant_ids)
        )
    )
    return {
        (tenant, config.routine_id)
        for tenant, payload in rows
        if (config := IntakeConfig.model_validate(payload)).routine_id is not None
    }


async def purge_auxiliary(
    session: AsyncSession, now: datetime, tenant_id: UUID | None = None
) -> None:
    # Unclassified sources retain only a digest disposition after their TTL,
    # preventing overlapping scans from rediscovering the same stale source.
    expire_messages = (
        update(LeadMailboxMessageRecord)
        .where(LeadMailboxMessageRecord.expires_at <= now)
        .values(
            encrypted_message_id=None,
            disposition="expired",
            mailbox_key="",
        )
    )
    if tenant_id is not None:
        expire_messages = expire_messages.where(LeadMailboxMessageRecord.tenant_id == tenant_id)
    await session.execute(expire_messages)
    query = delete(LeadOpportunityRecord).where(LeadOpportunityRecord.expires_at <= now)
    if tenant_id is not None:
        query = query.where(LeadOpportunityRecord.tenant_id == tenant_id)
    await session.execute(query)
    query = delete(LeadMailboxScanRecord).where(
        LeadMailboxScanRecord.started_at <= now - timedelta(days=90)
    )
    if tenant_id is not None:
        query = query.where(LeadMailboxScanRecord.tenant_id == tenant_id)
    await session.execute(query)


async def confirmation_tools(
    sessions: async_sessionmaker[AsyncSession], tenant_id: UUID
) -> frozenset[str]:
    """Only opted-in tenants add the resolver to their mandatory approval gates."""
    async with sessions() as session:
        configured = await session.scalar(
            select(LeadIntakeConfig.tenant_id).where(LeadIntakeConfig.tenant_id == tenant_id)
        )
    return frozenset({"lead_intake_resolve"}) if configured else frozenset()
