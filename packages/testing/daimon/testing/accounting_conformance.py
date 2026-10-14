"""C12 queried host facts from an isolated test database, never native meters.

Only an explicitly constructed hook runs this synthetic scenario. Production
accounting functions and a connection-local SQL fault provide all effects;
the unchanged mux C12 fixture decides whether the resulting facts conform.
"""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from daimon.core import accounting_outbox
from daimon.core._models import UsageEvent
from daimon.core.pricing import ModelRates, ProviderPrice
from daimon.core.stores import tenant_ledger, usage_events
from daimon.core.stores.mux_state import PostgresStateStore
from daimon.core.usage_recording import record_turn_usage
from daimon.testing.conformance.runner import PendingKind, PendingReason, require
from daimon.testing.db import WORKER_SCHEMA_PATTERN, isolated_test_schema
from mux.contracts.ids import ModelRef, ResourceRef, Scope
from mux.contracts.resources import ProviderBinding
from mux.contracts.usage import UsageObservation
from mux.state.store import binding_slot
from mux.state.usage_ledger import AppliedUsage, OutboxRow
from sqlalchemy import event, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.sql import Executable, Insert

type Snapshot = tuple[tuple[tuple[object, ...], ...], tuple[tuple[str, Decimal], ...]]


class LedgerCrash(Exception):
    """A controlled failure on this connection before the ledger INSERT."""


@dataclass(frozen=True)
class HostAccountingHook:
    """Attach as transport.host_accounting_evidence in a fresh C12 envelope.

    The caller provisions a disposable tenant and binding in its own schema.
    Event names are namespaced; native usage/price evidence is never replaced.
    Only OpenAI/Gemini are admitted; default/Anthropic paths never call this.
    A hook is single-use because it proves a first historical replay and crash.
    """

    engine: AsyncEngine
    scope: Scope
    binding: ProviderBinding
    session: ResourceRef
    model: ModelRef
    observed_at: datetime
    namespace: str

    @property
    def pending_reason(self) -> PendingReason:
        """Keep C12 deferred before dispatch; collected host facts are supplemental."""
        return PendingReason(
            PendingKind.ADAPTER_DEPENDENCY,
            "Neutral host aggregate coverage rejects the shared C12 sibling arrangement; "
            "full C12 certification is deferred.",
        )

    def __post_init__(self) -> None:
        url = self.engine.url
        schema = isolated_test_schema(self.engine)
        schema_match = WORKER_SCHEMA_PATTERN.fullmatch(schema) if schema is not None else None
        isolated_ci = (
            url.database == "daimon_test"
            and os.environ.get("GITHUB_ACTIONS") == "true"
            and os.environ.get("CI") == "true"
            and schema_match is not None
            and int(schema_match[1]) == os.getpid()
        )
        # Match the QA loopback/no-overrides policy, with the stronger sprint
        # lane namespace. The sole shared-name exception requires the approved
        # CI flags AND actual builder provenance for this process's schema.
        # All refusals happen before any session can acquire a connection.
        if (
            url.get_backend_name() != "postgresql"
            or url.host not in ("localhost", "127.0.0.1")
            or url.query
            or not (
                re.fullmatch(r"daimon_test_nc_[A-Za-z0-9][A-Za-z0-9_]{0,47}", url.database or "")
                or isolated_ci
            )
        ):
            raise ValueError("C12 hooks require an owned local daimon_test_nc_* lane database")
        if self.session.provider not in ("openai", "gemini"):
            raise ValueError("C12 neutral hook requires explicit OpenAI or Gemini")
        if (
            self.session.kind != "session"
            or self.scope.is_platform
            or self.scope.is_legacy_host_authorized
            or self.session.tenant_id != self.scope.tenant_id
            or self.session.account_id != self.scope.account_id
            or self.model.provider != self.session.provider
            or self.binding.provider != self.session.provider
            or self.binding.legacy_account_id not in (None, self.scope.account_id)
            or self.binding.thread.channel.tenant_id != self.scope.tenant_id
            or self.binding.native_refs.get("session") != self.session.id
            or self.observed_at.utcoffset() is None
            or not self.namespace
        ):
            raise ValueError("C12 evidence requires one authorized binding/session/model")
        uuid.UUID(self.scope.tenant_id)

    @property
    def sessions(self) -> async_sessionmaker[AsyncSession]:
        return async_sessionmaker(self.engine, expire_on_commit=False)

    @property
    def store(self) -> PostgresStateStore:
        return PostgresStateStore(self.sessions)

    @property
    def price(self) -> ProviderPrice:
        # Test policy, not a native price table or settlement of live receipts.
        return ProviderPrice(
            provider=self.model.provider,
            model=self.model.id,
            checked_on=self.observed_at.date(),
            input=Decimal(2),
            output=Decimal(10),
            cache_read=Decimal(1),
        )

    def observation(self, name: str, revision: int = 1, output: int = 100) -> UsageObservation:
        return UsageObservation(
            id=f"{self.namespace}:{name}",
            revision=revision,
            session=self.session,
            model=self.model,
            grain="model_request",
            basis="increment",
            input_tokens=0,
            output_tokens=output,
            input_cached_tokens=0,
            input_cache_write_tokens=0,
            completeness="measured",
            observed_at=self.observed_at,
            native_meter={"controlled_host_accounting": True},
        )

    async def record(self, observation: UsageObservation) -> bool:
        async with self.sessions() as session, session.begin():
            return await accounting_outbox.record_observation_usage(
                session,
                binding_id=self.binding.id,
                observation=observation,
                tenant_id=uuid.UUID(self.scope.tenant_id),
                platform_user_id=self.scope.principal_id,
                pricing=None,
                provider_price=self.price,
                infrastructure_usd=Decimal(0),
                channel_id=self.binding.thread.channel.channel_id,
            )

    async def apply(self, row: OutboxRow, session: AsyncSession) -> bool:
        return await accounting_outbox.apply_usage_outbox(
            session,
            row,
            tenant_id=uuid.UUID(self.scope.tenant_id),
            platform_user_id=self.scope.principal_id,
            pricing=None,
            provider_price=self.price,
            infrastructure_usd=Decimal(0),
            channel_id=self.binding.thread.channel.channel_id,
        )

    async def snapshot(self, names: tuple[str, ...]) -> Snapshot:
        identities = {self.observation(name).id for name in names}
        async with self.sessions() as session:
            usage = await usage_events.list_for_tenant(
                session, tenant_id=uuid.UUID(self.scope.tenant_id)
            )
            ledger = await tenant_ledger.list_for_tenant(
                session, tenant_id=uuid.UUID(self.scope.tenant_id)
            )
        return (
            tuple(
                (
                    row.managed_session_id,
                    row.event_id,
                    row.model,
                    row.input_tokens,
                    row.output_tokens,
                    row.cache_creation_input_tokens,
                    row.cache_read_input_tokens,
                    row.platform_user_id,
                    row.channel_id,
                    row.id,
                    row.occurred_at,
                    row.tenant_id,
                )
                for row in sorted(usage, key=lambda row: row.event_id)
                if row.managed_session_id == self.session.id and row.event_id in identities
            ),
            tuple(
                (row.idempotency_key, row.delta_usd)
                for row in sorted(ledger, key=lambda row: row.idempotency_key)
                if any(
                    row.idempotency_key == f"turn:{self.session.id}:{identity}"
                    or row.idempotency_key.startswith(f"adjust:{self.binding.id}:{identity}:")
                    for identity in identities
                )
            ),
        )

    async def _pending(self, store: PostgresStateStore) -> tuple[OutboxRow, ...]:
        return tuple(
            row for row in await store.pending_outbox() if row.binding_id == self.binding.id
        )

    async def __call__(self) -> Mapping[str, object]:
        require(
            await self.store.get_binding(binding_slot(self.binding)) == self.binding,
            "C12: owned binding must already be durable",
        )
        require(
            not (await self.snapshot(("history", "correction", "crash")))[0]
            and not await self._pending(self.store),
            "C12: envelope must be fresh",
        )
        history = self.observation("history")
        await record_turn_usage(
            sessionmaker=self.sessions,
            tenant_id=uuid.UUID(self.scope.tenant_id),
            platform_user_id=self.scope.principal_id,
            observation=history,
            pricing=ModelRates(input=2, output=10, cache_write=3, cache_read=1),
            channel_id=self.binding.thread.channel.channel_id,
        )
        history_before = await self.snapshot(("history",))
        require(await self.record(history), "C12: historical revision not ingested")
        require(not await self.record(history), "C12: historical replay reapplied")
        history_after = await self.snapshot(("history",))
        for revision, output in enumerate((100, 120, 110), 1):
            require(
                await self.record(self.observation("correction", revision, output)),
                "C12: correction not applied",
            )
        corrections = await self.snapshot(("correction",))
        async with self.sessions() as session:
            spend = await tenant_ledger.get_channel_spend(
                session,
                tenant_id=uuid.UUID(self.scope.tenant_id),
                channel_id=self.binding.thread.channel.channel_id,
                since=None,
                until=None,
            )
        overlap_before = await self.snapshot(("history", "correction"))
        aggregate_rejections: list[str] = []
        for grain in ("turn", "session"):
            aggregate = self.observation(f"total-{grain}", output=999).model_copy(
                update={
                    "grain": grain,
                    "basis": "cumulative",
                    "covers": (self.observation("correction").id,),
                }
            )
            try:
                require(await self.record(aggregate), "C12: covered aggregate not persisted")
            except ValueError as exc:
                # This deliberately records the merged host's refusal rather
                # than bypassing ingest or manufacturing accepted aggregates.
                if str(exc) not in (
                    "overlapping usage grains require explicit coverage or one billing grain",
                    "overlapping sibling usage coverage",
                ):
                    raise
                aggregate_rejections.append(grain)
        overlap_after = await self.snapshot(("history", "correction"))
        async with self.sessions() as session:
            bodies = (
                await session.scalars(
                    text(
                        "SELECT applied FROM usage_observation WHERE binding_id=:binding "
                        "AND observation_id IN (:turn, :session) "
                        "ORDER BY applied->'observation'->>'grain'"
                    ),
                    {
                        "binding": self.binding.id,
                        "turn": self.observation("total-turn").id,
                        "session": self.observation("total-session").id,
                    },
                )
            ).all()
        aggregates = tuple(AppliedUsage.model_validate(body).observation for body in bodies)
        require(
            await self.record(self.observation("crash")), "C12: initial crash usage not applied"
        )
        crash_before = await self.snapshot(("crash",))
        pending = await self.store.record_usage(self.binding.id, self.observation("crash", 2, 120))
        require(pending is not None, "C12: correction must be durably pending")
        if pending is None:
            raise TypeError("C12: missing pending correction")
        pending_before = len(await self._pending(self.store))
        projection_at_failure: int | None = None

        def crash_before_ledger(
            connection: Connection,
            statement: Executable,
            multiparams: Sequence[Mapping[str, object]],
            params: Mapping[str, object],
            execution_options: Mapping[str, object],
        ) -> None:
            nonlocal projection_at_failure
            if isinstance(statement, Insert) and statement.table.name == "tenant_ledger":
                projection_at_failure = connection.scalar(
                    select(UsageEvent.output_tokens).where(
                        UsageEvent.tenant_id == uuid.UUID(self.scope.tenant_id),
                        UsageEvent.managed_session_id == self.session.id,
                        UsageEvent.event_id == self.observation("crash").id,
                    )
                )
                raise LedgerCrash("controlled failure before ledger insert")

        async with self.engine.connect() as connection:
            event.listen(connection.sync_connection, "before_execute", crash_before_ledger)
            try:
                async with AsyncSession(bind=connection) as session, session.begin():
                    await self.apply(pending, session)
            except LedgerCrash:
                pass
            else:
                raise ValueError("C12: controlled crash boundary was not reached")
            finally:
                event.remove(connection.sync_connection, "before_execute", crash_before_ledger)
        crash_after_failure = await self.snapshot(("crash",))
        restarted = PostgresStateStore(self.sessions)
        pending_after_failure = await self._pending(restarted)
        require(len(pending_after_failure) == 1, "C12: rollback lost pending correction")
        async with self.sessions() as session, session.begin():
            require(
                await self.apply(pending_after_failure[0], session), "C12: restart failed to apply"
            )
        async with self.sessions() as session, session.begin():
            require(
                not await self.apply(pending_after_failure[0], session), "C12: replay charged twice"
            )
        return {
            "binding_id": self.binding.id,
            "history_before": history_before,
            "history_after": history_after,
            "corrections": corrections,
            "correction_channel_spend": spend
            - sum((-row[1] for row in history_after[1]), Decimal(0)),
            "overlap_before": overlap_before,
            "overlap_after": overlap_after,
            "aggregate_grains": tuple(observation.grain for observation in aggregates),
            "aggregate_covers": tuple(observation.covers for observation in aggregates),
            "aggregate_rejections": tuple(aggregate_rejections),
            "crash_before": crash_before,
            "crash_after_failure": crash_after_failure,
            "projection_at_failure": projection_at_failure,
            "pending_before_failure": pending_before,
            "pending_after_failure": len(pending_after_failure),
            "crash_after_restart": await self.snapshot(("crash",)),
            "pending_after_restart": len(await self._pending(restarted)),
            "source": "controlled_host_accounting",
            "pending_reason": self.pending_reason,
        }
