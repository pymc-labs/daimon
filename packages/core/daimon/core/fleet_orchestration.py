"""Orchestrator operations over the fleet owners' admission and journal ports.

No tool handle is authority. Hosts supply freshly loaded worker permissions;
the resolver authorizes writes and the store atomically retains operations,
reservations and audit. Missing owner ports leave the service unavailable.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager, nullcontext, suppress
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Literal, Protocol

import structlog
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.security_audit import record_authz_denial
from mux.contracts.config import BackendConfig
from mux.contracts.events import AgentMessagePayload, Event, TextPart
from mux.contracts.ids import ResourceRef, Revision, Scope
from mux.contracts.resources import Agent, AgentPatch, AgentSpec, SessionSpec
from pydantic import JsonValue, TypeAdapter

if TYPE_CHECKING:
    from daimon.core.fleet import FleetRun, FleetWorker
    from daimon.core.neutral_resources import ResolvedWorkerSpec, WorkerLimits, WorkerPermissions
    from daimon.core.pricing import ModelRates, ProviderPrice
    from daimon.core.stores.domain import BackendAgent
    from daimon.core.stores.fleet import WorkerPool
    from daimon.core.turn.deps import TurnDeps
    from daimon.core.turn.prepare import ProviderPreparationRequest
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


type Control = Literal["kill", "resume"]

log = structlog.get_logger(__name__)

WORKER_PREPARATION_TIMEOUT_S = 30.0


def _unique_object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate review field")
        result[key] = value
    return result


class FleetRefused(ValueError):
    """Closed refusal code, with no request, result or credential material."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"fleet: {reason}")


def _review_required(contract: Mapping[str, JsonValue] | None) -> bool:
    required = (contract or {}).get("review_required", False)
    if type(required) is not bool:
        raise FleetRefused("review_policy_invalid")
    return required


def _review_target(
    workers: tuple[FleetWorker, ...], reviewer_of: uuid.UUID, reviewer_backend: str
) -> FleetWorker:
    target = next((worker for worker in workers if worker.id == reviewer_of), None)
    if target is None or target.reviewer_of is not None:
        raise FleetRefused("review_target_required")
    if target.backend == reviewer_backend:
        raise FleetRefused("review_backend_must_differ")
    return target


def _result_format(contract: Mapping[str, JsonValue] | None) -> Literal["text", "json"]:
    contract = contract or {}
    format_ = contract.get("format", "text")
    if set(contract) - {"format", "review_required"} or format_ not in ("text", "json"):
        raise FleetRefused("result_contract_unsupported")
    _review_required(contract)
    return "json" if format_ == "json" else "text"


class FleetPorts(Protocol):
    """Store/budget/driver integration supplied by the owning host.

    Admission and control must commit operation identity, original ceilings,
    audit and reservation together. They repeat fresh authorization under the
    transaction before any native effect. Wait freezes the exact worker set and
    generation with the wake in that transaction. Result reads validate the
    worker's real binding slot before reading its journal. A missing integration
    never falls back to a provider client or a second state machine here.
    """

    async def read_run(
        self, scope: Scope, run_id: uuid.UUID
    ) -> tuple[FleetRun, tuple[FleetWorker, ...]]: ...

    async def controller_account(self, parent: WorkerPermissions) -> uuid.UUID | None: ...

    async def capture_source(self, parent: WorkerPermissions) -> BackendAgent | None: ...

    async def read_clone(
        self, parent: WorkerPermissions, ref: ResourceRef, revision: Revision
    ) -> Agent: ...

    async def admit(
        self,
        parent: WorkerPermissions,
        resolved: ResolvedWorkerSpec,
        *,
        run_id: uuid.UUID,
        operation_key: str,
        role: str,
        session: SessionSpec | None,
        result_contract: Mapping[str, JsonValue] | None,
        reviewer_of: uuid.UUID | None,
        reviewed_result: Event | None,
    ) -> tuple[FleetRun, FleetWorker]: ...

    async def control(
        self,
        parent: WorkerPermissions,
        run: FleetRun,
        worker: FleetWorker | None,
        *,
        action: Control,
        operation_key: str,
        expected_version: int,
        resolved: tuple[ResolvedWorkerSpec, ...],
    ) -> Mapping[str, JsonValue]:
        """Return a scoped durable acknowledgement, not native termination.

        Explicit accepted=True binds run_id, worker_id, action, operation_key,
        expected_version and the committed next version. A pending/refused
        operation uses accepted=False. Arbitrary JSON is not acknowledgement.
        """
        ...

    async def retained_control(
        self,
        parent: WorkerPermissions,
        run: FleetRun,
        worker: FleetWorker | None,
        *,
        action: Control,
        operation_key: str,
        expected_version: int,
    ) -> Mapping[str, JsonValue] | None:
        """Read an existing scoped operation only when its original request matches."""
        ...

    async def wait(
        self, parent: WorkerPermissions, run: FleetRun, workers: tuple[FleetWorker, ...]
    ) -> None: ...

    async def read_result(
        self,
        parent: WorkerPermissions,
        policy: TenantAccessPolicy,
        run: FleetRun,
        worker: FleetWorker,
    ) -> Event: ...

    def validate_contract(
        self, contract: Mapping[str, JsonValue] | None, result: Event
    ) -> JsonValue:
        """Validate the stored contract, refusing unsupported or invalid formats.

        Implementations must not fetch remote schemas or trust the excerpt.
        Return the validated output, rather than the model's summary of it.
        """
        ...


def _authorize(parent: WorkerPermissions, policy: TenantAccessPolicy) -> None:
    from daimon.core.authz import Action, Surface, authorize

    facts = parent.fleet
    decision = authorize(
        policy,
        subject=parent.subject,
        action=Action.SPAWN_WORKER,
        surface=Surface.FLEET,
        agent=parent.agent,
        place=facts.place,
        fleet=facts,
    )
    if not decision:
        raise FleetRefused(decision.reason or "permission_refused")
    if parent.agent_id is None:
        raise FleetRefused("root_identity_unbound")


def _scope(parent: WorkerPermissions, run: FleetRun) -> None:
    facts = parent.fleet
    if (
        facts.scope.is_platform
        or facts.scope.is_legacy_host_authorized
        or facts.scope.account_id != str(run.orchestrator_account_id)
        or run.id != facts.run_id
        or str(run.tenant_id) != facts.scope.tenant_id
        or run.orchestrator_account_id != facts.account_id
        or run.platform != facts.root.channel.platform
        or run.channel_id != facts.root.channel.channel_id
        or run.thread_id != facts.root.thread_id
    ):
        raise FleetRefused("foreign_run")


def _session_authorize(
    parent: WorkerPermissions, policy: TenantAccessPolicy, *, continuing: bool = False
) -> None:
    from daimon.core.authz import Action, SessionFacts, Surface, authorize

    root = parent.fleet.root
    decision = authorize(
        policy,
        subject=parent.subject,
        action=Action.CONTINUE_SESSION if continuing else Action.READ_SESSION,
        surface=Surface.FLEET,
        agent=parent.agent,
        origin=parent.fleet.place,
        origin_channel_ids=frozenset({root.channel.channel_id, root.thread_id}),
        session=SessionFacts(
            channel=root.channel.channel_id,
            thread=root.thread_id,
            seal_ids=parent.seal_ids,
            owned=parent.fleet.scope.account_id == str(parent.fleet.account_id),
        ),
    )
    if not decision:
        raise FleetRefused(decision.reason or "permission_refused")


class FleetOrchestrator:
    """Shared operations for authenticated MCP and platform control adapters."""

    def __init__(self, ports: FleetPorts | None = None) -> None:
        self._ports = ports

    def _owner(self) -> FleetPorts:
        if self._ports is None:
            raise FleetRefused("prerequisites_unavailable")
        return self._ports

    async def _controller_authorize(self, parent: WorkerPermissions) -> None:
        if parent.subject.via_agent_key or (
            not parent.subject.is_admin
            and await self._owner().controller_account(parent) != parent.fleet.account_id
        ):
            record_authz_denial("fleet_control", "controller_required")
            raise FleetRefused("controller_required")

    async def read_snapshot(
        self, parent: WorkerPermissions, policy: TenantAccessPolicy, run_id: uuid.UUID
    ) -> tuple[FleetRun, tuple[FleetWorker, ...]]:
        """Read canonical rows through the shared current-authority/membership fence."""
        _authorize(parent, policy)
        _session_authorize(parent, policy)
        if run_id != parent.fleet.run_id:
            raise FleetRefused("foreign_run")
        run, workers = await self._owner().read_run(parent.fleet.scope, run_id)
        _scope(parent, run)
        if any(worker.run_id != run.id or worker.tenant_id != run.tenant_id for worker in workers):
            raise FleetRefused("foreign_worker")
        if len({worker.id for worker in workers}) != len(workers):
            raise FleetRefused("ambiguous_workers")
        return run, workers

    async def spawn(
        self,
        parent: WorkerPermissions,
        policy: TenantAccessPolicy,
        *,
        now: datetime,
        operation_key: str,
        role: str,
        backend: BackendConfig,
        limits: WorkerLimits,
        run_id: uuid.UUID | None = None,
        agent: AgentSpec | None = None,
        clone: ResourceRef | None = None,
        clone_revision: Revision | None = None,
        overrides: AgentPatch | None = None,
        session: SessionSpec | None = None,
        result_contract: Mapping[str, JsonValue] | None = None,
        reviewer_of: uuid.UUID | None = None,
        captured_source: BackendAgent | None = None,
    ) -> Mapping[str, JsonValue]:
        """Resolve once, then hand admission to the atomic owner operation.

        Cross-review follows exactly this path with the target's durable result
        attached as input by the host. The model cannot supply review evidence.
        """
        _authorize(parent, policy)
        owner = self._owner()
        from daimon.core.neutral_resources import resolve_worker_spec

        selected_run = parent.fleet.run_id if run_id is None else run_id
        if selected_run != parent.fleet.run_id:
            raise FleetRefused("foreign_run")
        if (agent is None) == (clone is None):
            raise FleetRefused("spec_or_clone_required")
        if clone is None and (clone_revision is not None or overrides is not None):
            raise FleetRefused("clone_required")
        if not operation_key.strip() or not role.strip():
            raise FleetRefused("operation_required")
        if session is not None:
            # The host must construct worker-slot/session resources from the
            # resolved grant; arbitrary native refs are never request authority.
            raise FleetRefused("session_resources_unavailable")
        if reviewer_of is None:
            _result_format(result_contract)
        source: AgentSpec | Agent
        if clone is not None:
            if clone.kind != "agent" or clone_revision is None:
                raise FleetRefused("pinned_clone_required")
            if clone.tenant_id != parent.fleet.scope.tenant_id or (
                clone.account_id != parent.fleet.scope.account_id
                and (clone, clone_revision) not in parent.source_revisions
            ):
                raise FleetRefused("foreign_clone")
            source = await owner.read_clone(parent, clone, clone_revision)
            if source.ref != clone or source.revision != clone_revision:
                raise FleetRefused("stale_clone")
        else:
            assert agent is not None
            source = agent
        if captured_source is None:
            captured_source = await self._owner().capture_source(parent)
        resolved = resolve_worker_spec(
            parent,
            source,
            policy=policy,
            now=now,
            backend=backend,
            overrides=overrides,
            # Source memory is a host-owned binding already bounded by the
            # retained root. The resolver validates its scope, native payload,
            # read-only policy and explicit drops before materialization.
            resources=tuple(
                binding
                for binding in parent.resources
                if captured_source is not None
                and binding.resource is not None
                and binding.resource.kind == "memory_store"
                and binding.resource.id == captured_source.memory_store_id
            ),
            limits=limits,
            captured_source=captured_source,
        )
        reviewed: Event | None = None
        if reviewer_of is not None:
            run, workers = await self.read_snapshot(parent, policy, selected_run)
            target = _review_target(workers, reviewer_of, resolved.backend.backend)
            reviewed = await self._result(parent, policy, run, target)
            result_contract = {
                "format": "review",
                "reviewed_digest": target.result.digest if target.result is not None else "",
            }
        run, worker = await owner.admit(
            parent,
            resolved,
            run_id=selected_run,
            operation_key=operation_key,
            role=role,
            session=None,
            result_contract=result_contract,
            reviewer_of=reviewer_of,
            reviewed_result=reviewed,
        )
        _scope(parent, run)
        if worker.run_id != run.id or worker.tenant_id != run.tenant_id:
            raise FleetRefused("foreign_worker")
        if (
            worker.spec_digest != resolved.digest
            or worker.spec != resolved.spec.model_dump(mode="json")
            or worker.backend != resolved.backend.backend
            or worker.profile != resolved.backend.profile
            or worker.model != resolved.spec.model.id
            or worker.budget_usd != resolved.limits.budget_usd
            or worker.deadline_at != resolved.limits.deadline
            or worker.max_turns != resolved.limits.max_turns
            or worker.reviewer_of != reviewer_of
            or worker.result_contract != result_contract
            or worker.id == reviewer_of
        ):
            raise FleetRefused("admitted_worker_mismatch")
        return {"run_id": str(run.id), "worker_id": str(worker.id), "status": worker.status}

    async def status(
        self, parent: WorkerPermissions, policy: TenantAccessPolicy, *, run_id: uuid.UUID
    ) -> Mapping[str, JsonValue]:
        run, workers = await self.read_snapshot(parent, policy, run_id)
        return {
            "run_id": str(run.id),
            "status": run.status,
            "version": run.version,
            "reserved_usd": str(run.reserved_usd),
            "observed_usd": str(run.observed_usd),
            "workers": [
                {
                    "worker_id": str(worker.id),
                    "role": worker.role,
                    "status": worker.status,
                    "version": worker.version,
                    "reviewer_of": str(worker.reviewer_of)
                    if worker.reviewer_of is not None
                    else None,
                }
                for worker in workers
            ],
        }

    async def control(
        self,
        parent: WorkerPermissions,
        policy: TenantAccessPolicy,
        *,
        run_id: uuid.UUID,
        worker_id: uuid.UUID | None,
        action: Control,
        expected_version: int,
        operation_key: str,
        now: datetime,
    ) -> Mapping[str, JsonValue]:
        if (
            action not in ("kill", "resume")
            or type(expected_version) is not int
            or expected_version < 1
        ):
            raise FleetRefused("invalid_control")
        if now.utcoffset() is None:
            raise FleetRefused("deadline_timezone_required")
        run, workers = await self.read_snapshot(parent, policy, run_id)
        _session_authorize(parent, policy, continuing=True)
        await self._controller_authorize(parent)
        worker = next((row for row in workers if row.id == worker_id), None)
        if worker_id is not None and worker is None:
            raise FleetRefused("foreign_worker")
        if not operation_key.strip():
            raise FleetRefused("operation_required")
        retained = await self._owner().retained_control(
            parent,
            run,
            worker,
            action=action,
            operation_key=operation_key,
            expected_version=expected_version,
        )
        if retained is not None:
            return self._control_reply(
                retained, run, worker, action, operation_key, expected_version
            )
        target_version = worker.version if worker is not None else run.version
        if expected_version != target_version:
            raise FleetRefused("stale_version")
        if run.status in ("completed", "failed", "cancelled"):
            raise FleetRefused("terminal_run")
        if worker is not None and worker.status in (
            "succeeded",
            "failed",
            "cancelled",
            "timed_out",
            "budget_exceeded",
        ):
            raise FleetRefused("terminal_worker")
        if action == "resume" and (
            now >= run.deadline_at
            or (
                worker is not None
                and (now >= worker.deadline_at or worker.turns_used >= worker.max_turns)
            )
        ):
            raise FleetRefused("original_limit_exhausted")
        prepared: list[ResolvedWorkerSpec] = []
        if action == "resume":
            from daimon.core.neutral_resources import WorkerLimits, resolve_worker_spec

            candidates = (
                (worker,)
                if worker is not None
                else tuple(
                    row for row in workers if row.status in ("pending", "starting", "running")
                )
            )
            if not candidates:
                raise FleetRefused("terminal_run")
            for candidate in candidates:
                if now >= candidate.deadline_at or candidate.turns_used >= candidate.max_turns:
                    raise FleetRefused("original_limit_exhausted")
                choices = tuple(
                    grant
                    for grant in parent.backend_grants
                    if grant.backend == candidate.backend
                    and grant.profile == candidate.profile
                    and grant.model in (None, candidate.model)
                )
                if len(choices) != 1:
                    raise FleetRefused("backend_grant_unavailable")
                prepared.append(
                    resolve_worker_spec(
                        parent,
                        AgentSpec.model_validate(candidate.spec),
                        policy=policy,
                        now=now,
                        backend=BackendConfig.model_validate(choices[0].model_dump(mode="json")),
                        limits=WorkerLimits(
                            budget_usd=candidate.budget_usd,
                            deadline=candidate.deadline_at,
                            max_turns=candidate.max_turns,
                        ),
                    )
                )
        reply = await self._owner().control(
            parent,
            run,
            worker,
            action=action,
            expected_version=expected_version,
            operation_key=operation_key,
            resolved=tuple(prepared),
        )
        return self._control_reply(reply, run, worker, action, operation_key, expected_version)

    @staticmethod
    def _control_reply(
        reply: Mapping[str, JsonValue],
        run: FleetRun,
        worker: FleetWorker | None,
        action: Control,
        operation_key: str,
        expected_version: int,
    ) -> Mapping[str, JsonValue]:
        identifiers: dict[str, JsonValue] = {
            "run_id": str(run.id),
            "worker_id": str(worker.id) if worker is not None else None,
            "action": action,
            "operation_key": operation_key,
            "expected_version": expected_version,
        }
        if reply.get("accepted") is False:
            return identifiers | {"accepted": False, "status": "waiting"}
        version = reply.get("version")
        if (
            reply.get("accepted") is not True
            or any(reply.get(key) != value for key, value in identifiers.items())
            or type(version) is not int
            or version <= expected_version
        ):
            raise FleetRefused("control_evidence_invalid")
        return identifiers | {"accepted": True, "status": "accepted", "version": version}

    async def _result(
        self,
        parent: WorkerPermissions,
        policy: TenantAccessPolicy,
        run: FleetRun,
        worker: FleetWorker,
    ) -> Event:
        from daimon.core.fleet import result_digest

        pointer = worker.result
        if worker.status != "succeeded" or pointer is None or worker.binding_id is None:
            raise FleetRefused("result_unavailable")
        result = await self._owner().read_result(parent, policy, run, worker)
        if (
            result.session_id != pointer.session_id
            or result.native.provider != worker.backend
            or result.sequence != pointer.sequence
            or result_digest(dict(result.payload)) != pointer.digest
            or result.authority not in ("record", "reconciled")
            or result.type != "agent.message"
        ):
            raise FleetRefused("result_evidence_invalid")
        message = AgentMessagePayload.model_validate(result.payload)
        if not message.complete:
            raise FleetRefused("result_incomplete")
        return result

    async def collect(
        self, parent: WorkerPermissions, policy: TenantAccessPolicy, *, run_id: uuid.UUID
    ) -> Mapping[str, JsonValue]:
        """Return verified inputs for one summary, never a synthesized success.

        Quorum and wake readiness are scheduling facts. They cannot bypass an
        unfinished/failed worker, a refused review or an invalid result contract.
        """
        run, workers = await self.read_snapshot(parent, policy, run_id)
        await self._controller_authorize(parent)
        if run.status in ("cancelled", "failed"):
            return {"run_id": str(run.id), "status": "blocked", "reason": "run_stopped"}
        if not workers:
            raise FleetRefused("empty_run")
        active = {
            worker.reviewer_of
            for worker in workers
            if worker.status in ("pending", "starting", "running")
        }
        if active:
            # The initial phase precedes separate review cohorts for each target.
            for target in {None} if None in active else active:
                await self._owner().wait(
                    parent, run, tuple(worker for worker in workers if worker.reviewer_of == target)
                )
            return {"run_id": str(run.id), "status": "waiting"}
        if any(worker.status != "succeeded" for worker in workers):
            return {"run_id": str(run.id), "status": "blocked", "reason": "worker_failed"}
        if any(
            worker.reviewer_of is None
            and _review_required(worker.result_contract)
            and not any(reviewer.reviewer_of == worker.id for reviewer in workers)
            for worker in workers
        ):
            return {"run_id": str(run.id), "status": "blocked", "reason": "review_required"}
        results: dict[uuid.UUID, Event] = {}
        for worker in workers:
            results[worker.id] = await self._result(parent, policy, run, worker)
        for worker in workers:
            if worker.reviewer_of is None:
                continue
            target = _review_target(workers, worker.reviewer_of, worker.backend)
            if target.result is None:
                raise FleetRefused("review_target_required")
            contract = worker.result_contract
            if contract != {"format": "review", "reviewed_digest": target.result.digest}:
                raise FleetRefused("review_digest_mismatch")
            message = AgentMessagePayload.model_validate(results[worker.id].payload)
            if not message.content or any(
                not isinstance(part, TextPart) for part in message.content
            ):
                raise FleetRefused("review_evidence_invalid")
            text = "".join(part.text for part in message.content if isinstance(part, TextPart))
            try:
                verdict = json.loads(text, object_pairs_hook=_unique_object)
            except ValueError:
                raise FleetRefused("review_evidence_invalid") from None
            if not isinstance(verdict, dict) or verdict != {
                "verdict": "approved",
                "reviewed_digest": target.result.digest,
            }:
                return {"run_id": str(run.id), "status": "blocked", "reason": "review_refused"}
        outputs: list[JsonValue] = [
            {
                "worker_id": str(worker.id),
                "result": self._owner().validate_contract(
                    worker.result_contract, results[worker.id]
                ),
            }
            for worker in workers
            if worker.reviewer_of is None
        ]
        return {"run_id": str(run.id), "status": "ready", "results": outputs}


class HostFleetPorts:
    """Compose durable fleet, admission and full-result owners for the host.

    Native dispatch and completion/control hooks are installed by their existing
    host owners. An absent hook refuses before funding or native effects.
    """

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        prices: Mapping[tuple[str, str], ModelRates | ProviderPrice] | None = None,
        markup: Decimal,
        dispatch: Callable[
            [WorkerPermissions, FleetWorker, ResolvedWorkerSpec, Event | None], Awaitable[None]
        ]
        | None = None,
        clone_reader: Callable[[WorkerPermissions, ResourceRef, Revision], Awaitable[Agent]]
        | None = None,
        deps: TurnDeps | None = None,
        wait: Callable[[WorkerPermissions, FleetRun, tuple[FleetWorker, ...]], Awaitable[None]]
        | None = None,
        preparation: Callable[[WorkerPermissions], Awaitable[ProviderPreparationRequest]]
        | None = None,
        pool: WorkerPool | None = None,
    ) -> None:
        if prices is None:
            from types import MappingProxyType

            from daimon.core.pricing import MODEL_PRICING
            from daimon.core.turn.runtimes import channel_runtime_prices
            from mux.profiles import MANAGED_AGENTS

            prices = MappingProxyType(
                {(MANAGED_AGENTS.provider, model): rate for model, rate in MODEL_PRICING.items()}
                | dict(channel_runtime_prices())
            )
        self.sessions, self.prices, self.markup = sessions, prices, markup
        self._deps = deps
        if preparation is None and deps is not None and deps.fleet_enabled and pool is not None:
            preparation = self._prepare_original
        self._preparation, self._pool = preparation, pool
        self._dispatch = dispatch
        if dispatch is None and preparation is not None and pool is not None:
            self._dispatch = self._dispatch_worker
        self._clone_reader, self._wait = clone_reader, wait
        self._tasks: dict[uuid.UUID, asyncio.Task[None]] = {}
        self._watches: dict[
            uuid.UUID, tuple[dict[uuid.UUID, asyncio.Event], asyncio.Event, asyncio.Task[None]]
        ] = {}

    async def _prepare_original(self, parent: WorkerPermissions) -> ProviderPreparationRequest:
        from daimon.core.turn.runtime_manifest import read_frozen_preparation

        if self._deps is None or not self._deps.fleet_enabled:
            raise FleetRefused("prerequisites_unavailable")
        return await read_frozen_preparation(
            self._deps,
            parent,
            deadline=parent.limits.deadline,
            now=lambda: datetime.now(UTC),
        )

    async def _dispatch_worker(
        self,
        parent: WorkerPermissions,
        worker: FleetWorker,
        resolved: ResolvedWorkerSpec,
        reviewed_result: Event | None,
    ) -> None:
        from daimon.core.channel_budget import watch_fleet_budget
        from daimon.core.fleet import FleetConflict, result_digest
        from daimon.core.headless_runner import commit_worker_transition, run_worker
        from daimon.core.stores import fleet
        from daimon.core.turn.ceiling import remaining_s

        async with self._load_preparation(parent, deadline=worker.deadline_at) as request:
            pool = self._pool
            if pool is None or request.deps.running_turns.shutting_down:
                raise FleetRefused("prerequisites_unavailable")
            active = self._tasks.get(worker.id)
            if active is not None and not active.done():
                return
            watch = self._watches.get(worker.run_id)
            if watch is None or watch[2].done():
                cancellations: dict[uuid.UUID, asyncio.Event] = {}
                finished = asyncio.Event()

                async def monitor() -> None:
                    try:
                        with request.deps.running_turns.track():
                            async with asyncio.timeout(
                                remaining_s(
                                    parent.limits.deadline, now=request.now(), refuse_expired=True
                                )
                            ):
                                await watch_fleet_budget(
                                    sessionmaker=self.sessions,
                                    parent=parent,
                                    prices=self.prices,
                                    markup=self.markup,
                                    cancelled=cancellations,
                                    finished=finished,
                                )
                    finally:
                        for event in cancellations.values():
                            event.set()

                watch = cancellations, finished, asyncio.create_task(monitor())
                self._watches[worker.run_id] = watch

                def ended(done: asyncio.Task[None]) -> None:
                    retained = self._watches.get(worker.run_id)
                    if retained is not None and retained[2] is done:
                        self._watches.pop(worker.run_id)
                    if not done.cancelled():
                        done.exception()

                watch[2].add_done_callback(ended)
            cancel = watch[0].setdefault(worker.id, asyncio.Event())
            trigger = "Complete the task in your instructions."
            if reviewed_result is not None:
                trigger = json.dumps(
                    {
                        "instruction": (
                            "Return JSON verdict approved or refused and reviewed_digest."
                        ),
                        "reviewed_digest": result_digest(dict(reviewed_result.payload)),
                        "result": dict(reviewed_result.payload),
                    }
                )

            async def execute() -> None:
                with request.deps.running_turns.track():
                    if request.deps.running_turns.shutting_down:
                        cancel.set()
                    bound = min(request.deadline, worker.deadline_at)
                    async with asyncio.timeout(
                        remaining_s(bound, now=request.now(), refuse_expired=True)
                    ):
                        while not cancel.is_set():
                            claimed = await pool.claim(
                                self.sessions,
                                tenant_id=worker.tenant_id,
                                run_id=worker.run_id,
                                worker_id=worker.id,
                            )
                            if claimed is not None:
                                try:
                                    failure_code = "preparation_refused"
                                    try:
                                        async with asyncio.timeout(WORKER_PREPARATION_TIMEOUT_S):
                                            prepared = await request.for_worker(parent, resolved)
                                        failure_code = "dispatch_refused"
                                        await run_worker(
                                            prepared,
                                            parent=parent,
                                            worker=claimed,
                                            trigger_message=trigger,
                                            prices=self.prices,
                                            cancel=cancel,
                                        )
                                    except BaseException:
                                        with suppress(FleetConflict):
                                            await asyncio.shield(
                                                commit_worker_transition(
                                                    request,
                                                    parent,
                                                    claimed,
                                                    failure_code=failure_code,
                                                )
                                            )
                                        raise
                                finally:
                                    pool.release()
                                return
                            _, rows = await fleet.run_status(
                                self.sessions, tenant_id=worker.tenant_id, run_id=worker.run_id
                            )
                            actual = next((row for row in rows if row.id == worker.id), None)
                            if actual is None or actual.status != "pending":
                                return
                            await asyncio.sleep(0.25)

            task = asyncio.create_task(execute())
            self._tasks[worker.id] = task

            def completed(done: asyncio.Task[None]) -> None:
                if self._tasks.get(worker.id) is done:
                    self._tasks.pop(worker.id)
                error = asyncio.CancelledError() if done.cancelled() else done.exception()
                if error is not None:
                    from daimon.core.channel_backend import backend_refusal_causes

                    causes = backend_refusal_causes(error)
                    log.warning(
                        "fleet.worker_task_failed",
                        run_id=str(worker.run_id),
                        worker_id=str(worker.id),
                        error_class=causes[0]["type"],
                        causes=causes,
                    )

            task.add_done_callback(completed)

    async def close(self) -> None:
        for events, finished, _ in self._watches.values():
            for event in events.values():
                event.set()
            finished.set()
        tasks = (*self._tasks.values(), *(watch[2] for watch in self._watches.values()))
        if tasks:
            try:
                async with asyncio.timeout(5):
                    await asyncio.gather(*tasks, return_exceptions=True)
            except TimeoutError:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    @asynccontextmanager
    async def _load_preparation(
        self, parent: WorkerPermissions, *, deadline: datetime | None = None
    ) -> AsyncIterator[ProviderPreparationRequest]:
        from daimon.core.errors import TurnError
        from daimon.core.turn.ceiling import remaining_s

        if self._preparation is None:
            raise FleetRefused("prerequisites_unavailable")
        bound = min(
            parent.limits.deadline, deadline if deadline is not None else parent.limits.deadline
        )
        try:
            with self._deps.running_turns.track() if self._deps is not None else nullcontext():
                async with asyncio.timeout(
                    remaining_s(bound, now=datetime.now(UTC), refuse_expired=True)
                ):
                    request = await self._preparation(parent)
                    bound = min(bound, request.deadline)
                    remaining_s(bound, now=request.now(), refuse_expired=True)
                    yield replace(request, deadline=bound)
                    remaining_s(bound, now=datetime.now(UTC), refuse_expired=True)
        except TimeoutError:
            raise FleetRefused("preparation_deadline_exceeded") from None
        except TurnError as error:
            if error.kind != "ceiling":
                raise
            raise FleetRefused("preparation_deadline_exceeded") from None

    async def capture_source(self, parent: WorkerPermissions) -> BackendAgent | None:
        if self._preparation is None:
            return None
        async with self._load_preparation(parent) as request:
            return (await request.capture_root(parent)).source

    async def controller_account(self, parent: WorkerPermissions) -> uuid.UUID | None:
        from daimon.core.stores.identity import find_platform_principal

        if parent.subject.platform_user_id is None:
            return None
        async with self.sessions() as db:
            principal = await find_platform_principal(
                db,
                tenant_id=uuid.UUID(parent.fleet.scope.tenant_id),
                platform=parent.fleet.root.channel.platform,
                external_id=parent.subject.platform_user_id,
            )
            return principal.account_id if principal is not None else None

    async def read_run(
        self, scope: Scope, run_id: uuid.UUID
    ) -> tuple[FleetRun, tuple[FleetWorker, ...]]:
        from daimon.core.stores import fleet

        if scope.is_platform or scope.is_legacy_host_authorized:
            raise FleetRefused("root_scope_required")
        run, workers = await fleet.run_status(
            self.sessions, tenant_id=uuid.UUID(scope.tenant_id), run_id=run_id
        )
        if str(run.orchestrator_account_id) != scope.account_id:
            raise FleetRefused("foreign_run")
        return run, workers

    async def read_clone(
        self, parent: WorkerPermissions, ref: ResourceRef, revision: Revision
    ) -> Agent:
        if self._clone_reader is not None:
            return await self._clone_reader(parent, ref, revision)
        captured = await self.capture_source(parent)
        if (
            captured is None
            or captured.source is None
            or (captured.source.ref, captured.source.revision) != (ref, revision)
        ):
            raise FleetRefused("clone_evidence_unavailable")
        return captured.source

    async def admit(
        self,
        parent: WorkerPermissions,
        resolved: ResolvedWorkerSpec,
        *,
        run_id: uuid.UUID,
        operation_key: str,
        role: str,
        session: SessionSpec | None,
        result_contract: Mapping[str, JsonValue] | None,
        reviewer_of: uuid.UUID | None,
        reviewed_result: Event | None,
    ) -> tuple[FleetRun, FleetWorker]:
        from types import MappingProxyType

        from daimon.core.channel_budget import admit_fleet_worker
        from daimon.core.fleet import FleetConflict
        from daimon.core.pricing import ProviderPrice, resolved_worker_prices
        from daimon.core.stores import access_policy, fleet, mux_state
        from mux.errors import OperationConflict
        from mux.state.operations import request_digest

        if self._dispatch is None or session is not None or run_id != parent.fleet.run_id:
            raise FleetRefused("prerequisites_unavailable")
        if (reviewer_of is None) != (reviewed_result is None):
            raise FleetRefused("review_evidence_invalid")
        self.prices = MappingProxyType(
            dict(self.prices)
            | dict(
                resolved_worker_prices(
                    ((resolved.backend, resolved.spec.model),),
                    native_prices={
                        key: rate
                        for key, rate in self.prices.items()
                        if isinstance(rate, ProviderPrice)
                    },
                )
            )
        )
        scope = parent.fleet.scope
        tenant_id = uuid.UUID(scope.tenant_id)
        key = f"fleet:{run_id}:admit:{operation_key}"
        digest = request_digest(
            {
                "resolved": resolved.digest,
                "role": role,
                "result_contract": dict(result_contract) if result_contract is not None else None,
                "reviewer_of": str(reviewer_of) if reviewer_of is not None else None,
                "reviewed_result": reviewed_result.model_dump(mode="json")
                if reviewed_result is not None
                else None,
            }
        )
        try:
            async with self.sessions.begin() as db:
                policy = await access_policy.load_access_policy(db, tenant_id=tenant_id)
                _authorize(parent, policy)
                # Lock before the ordinal read; admission reuses this same transaction lock.
                await fleet.lock_budget(
                    db,
                    tenant_id=tenant_id,
                    actor_id=parent.fleet.account_id,
                    run_id=run_id,
                )
                run, workers = await fleet.run_status(db, tenant_id=tenant_id, run_id=run_id)
                _scope(parent, run)
                begun = await mux_state.begin_operation(
                    db,
                    scope,
                    key=key,
                    request_digest=digest,
                    operation_id=key,
                    now=await mux_state.database_clock(db),
                )
                if not begun.fresh:
                    worker = next(
                        (
                            row
                            for row in workers
                            if str(row.id) == begun.record.result.get("worker_id")
                        ),
                        None,
                    )
                    if begun.record.operation.status != "processed" or worker is None:
                        raise FleetRefused("admission_evidence_unavailable")
                else:
                    usage = await admit_fleet_worker(
                        db,
                        parent=parent,
                        policy=policy,
                        resolved=resolved,
                        ordinal=len(workers),
                        role=role,
                        prices=self.prices,
                        markup=self.markup,
                        reviewer_of=reviewer_of,
                        result_contract=result_contract,
                    )
                    await mux_state.claim_send(
                        db,
                        scope,
                        key,
                        now=await mux_state.database_clock(db),
                        clock=mux_state.database_clock,
                        fence=None,
                    )
                    await mux_state.advance_operation(
                        db,
                        scope,
                        key,
                        "processed",
                        now=await mux_state.database_clock(db),
                        clock=mux_state.database_clock,
                        fence=None,
                        result={"worker_id": str(usage.worker.id)},
                    )
                    run, worker = usage.run, usage.worker
            if worker.status == "cancelled" and worker.error_code is not None:
                raise FleetRefused("financial_admission_refused")
            if worker.status == "pending":
                await self._dispatch(parent, worker, resolved, reviewed_result)
            return run, worker
        except OperationConflict:
            raise FleetRefused("operation_conflict") from None
        except FleetConflict:
            raise FleetRefused("admission_evidence_invalid") from None

    async def read_result(
        self,
        parent: WorkerPermissions,
        policy: TenantAccessPolicy,
        run: FleetRun,
        worker: FleetWorker,
    ) -> Event:
        from daimon.core.fleet import FleetConflict, check_result, worker_slot
        from daimon.core.stores import access_policy, fleet, mux_state

        _scope(parent, run)
        async with self.sessions.begin() as db:
            fresh = await access_policy.load_access_policy(db, tenant_id=run.tenant_id)
            _authorize(parent, fresh)
            _session_authorize(parent, fresh)
            actual_run, workers = await fleet.run_status(db, tenant_id=run.tenant_id, run_id=run.id)
            _scope(parent, actual_run)
            actual = next((row for row in workers if row.id == worker.id), None)
            if (
                actual is None
                or actual != worker
                or actual.result is None
                or actual.status != "succeeded"
            ):
                raise FleetRefused("result_unavailable")
            binding = await mux_state.get_binding(db, worker_slot(actual_run, actual))
            records = await mux_state.read_events(
                db, actual.result.session_id, after=actual.result.sequence - 1, limit=1
            )
            result = records[0] if records else None
            try:
                check_result(actual_run, actual, binding, result, actual.result)
            except FleetConflict:
                raise FleetRefused("result_evidence_invalid") from None
            assert result is not None
            return result

    def validate_contract(
        self, contract: Mapping[str, JsonValue] | None, result: Event
    ) -> JsonValue:
        format_ = _result_format(contract)
        message = AgentMessagePayload.model_validate(result.payload)
        if (
            not message.complete
            or not message.content
            or any(not isinstance(part, TextPart) for part in message.content)
        ):
            raise FleetRefused("result_contract_invalid")
        text = "".join(part.text for part in message.content if isinstance(part, TextPart))
        if format_ == "text":
            return text
        try:
            value = json.loads(text, object_pairs_hook=_unique_object)
            json.dumps(value, allow_nan=False)
            return value
        except ValueError:
            raise FleetRefused("result_contract_invalid") from None

    async def wait(
        self, parent: WorkerPermissions, run: FleetRun, workers: tuple[FleetWorker, ...]
    ) -> None:
        if self._wait is not None:
            return await self._wait(parent, run, workers)
        if self._deps is None:
            raise FleetRefused("prerequisites_unavailable")
        from daimon.core.continuity.continuation import ContinuationRequest
        from daimon.core.continuity.wakes import arm_worker_finished, worker_finished_key
        from daimon.core.fleet import FleetConflict
        from daimon.core.neutral_resources import WorkerSpecRefused
        from daimon.core.stores import access_policy, fleet, mux_state, turn_origins
        from daimon.core.stores.domain import ChatPlatform
        from daimon.core.turn.runtime_manifest import read_frozen_root
        from mux.errors import ScopeViolation

        try:
            frozen, _ = await read_frozen_root(
                self._deps,
                parent.fleet.scope,
                origin_id=parent.fleet.origin_id,
                run_id=run.id,
                now=datetime.now(UTC),
                drain=True,
            )
        except (WorkerSpecRefused, ScopeViolation, FleetConflict):
            raise FleetRefused("wait_unavailable") from None
        ids = tuple(worker.id for worker in workers)
        async with self.sessions.begin() as db:
            policy = await access_policy.load_access_policy(db, tenant_id=run.tenant_id)
            _authorize(parent, policy)
            _session_authorize(parent, policy, continuing=True)
            await fleet.lock_budget(
                db, tenant_id=run.tenant_id, actor_id=parent.fleet.account_id, run_id=run.id
            )
            actual, _ = await fleet.run_status(db, tenant_id=run.tenant_id, run_id=run.id)
            _scope(parent, actual)
            now = await mux_state.database_clock(db)
            origin = await turn_origins.get_active_origin(
                db,
                origin_id=parent.fleet.origin_id,
                tenant_id=actual.tenant_id,
                account_id=actual.orchestrator_account_id,
                platform=actual.platform,
                now=now,
            )
            _scope(frozen, actual)
            if (
                origin is None
                or not frozen.subject.platform_user_id
                or origin.responder_ma_agent_id != frozen.source_agents[0].id
            ):
                raise FleetRefused("wait_unavailable")
            try:
                request = ContinuationRequest(
                    tenant_id=actual.tenant_id,
                    platform=TypeAdapter[ChatPlatform](ChatPlatform).validate_python(
                        actual.platform
                    ),
                    parent_channel_id=actual.channel_id,
                    thread_id=actual.thread_id,
                    requester_account_id=actual.orchestrator_account_id,
                    requester_external_user_id=frozen.subject.platform_user_id,
                    target_ma_agent_id=origin.responder_ma_agent_id,
                    target_name=origin.responder_name,
                    requested_work=None,
                    reason="worker_finished",
                    idempotency_key=worker_finished_key(actual.id, ids),
                )
                row = await arm_worker_finished(
                    db, request, run_id=actual.id, worker_ids=ids, now=now
                )
            except ValueError:
                raise FleetRefused("wait_unavailable") from None
            if row is None:
                raise FleetRefused("wait_unavailable")

    async def _control_record(
        self,
        parent: WorkerPermissions,
        run: FleetRun,
        worker: FleetWorker | None,
        *,
        action: Control,
        operation_key: str,
        expected_version: int,
        write: bool,
    ) -> Mapping[str, JsonValue] | None:
        from daimon.core.fleet import FleetConflict
        from daimon.core.stores import access_policy, fleet, mux_state
        from mux.errors import OperationConflict, ScopeViolation
        from mux.state.operations import request_digest

        if action != "kill":
            raise FleetRefused("resume_evidence_unavailable")
        scope = parent.fleet.scope
        key = f"fleet:{run.id}:control:{operation_key}"
        terms: dict[str, JsonValue] = {
            "run_id": str(run.id),
            "worker_id": str(worker.id) if worker else None,
            "action": action,
            "operation_key": operation_key,
            "expected_version": expected_version,
        }
        digest = request_digest(terms)
        try:
            async with self.sessions.begin() as db:
                policy = await access_policy.load_access_policy(db, tenant_id=run.tenant_id)
                _authorize(parent, policy)
                _session_authorize(parent, policy, continuing=True)
                await fleet.lock_budget(
                    db, tenant_id=run.tenant_id, actor_id=parent.fleet.account_id, run_id=run.id
                )
                actual, _ = await fleet.run_status(db, tenant_id=run.tenant_id, run_id=run.id)
                _scope(parent, actual)
                now = await mux_state.database_clock(db)
                if write:
                    begun = await mux_state.begin_operation(
                        db, scope, key=key, request_digest=digest, operation_id=key, now=now
                    )
                    record = None if begun.fresh else begun.record
                else:
                    record = await mux_state.get_operation(db, scope, key)
                    if record is None:
                        return None
                if record is not None:
                    if record.operation.request_digest != digest:
                        raise FleetRefused("operation_conflict")
                    if record.operation.status != "processed":
                        raise FleetRefused("control_evidence_unavailable")
                    return record.result
                if actual.status != "running":
                    raise FleetRefused("terminal_run")
                if worker is None:
                    updated = await fleet.set_run_status(
                        db,
                        tenant_id=run.tenant_id,
                        run_id=run.id,
                        expected_version=expected_version,
                        status="cancelled",
                    )
                else:
                    updated = await fleet.request_worker_stop(
                        db,
                        tenant_id=run.tenant_id,
                        worker_id=worker.id,
                        expected_version=expected_version,
                    )
                reply = terms | {"accepted": True, "version": updated.version}
                await mux_state.claim_send(
                    db, scope, key, now=now, clock=mux_state.database_clock, fence=None
                )
                await mux_state.advance_operation(
                    db,
                    scope,
                    key,
                    "processed",
                    now=now,
                    clock=mux_state.database_clock,
                    fence=None,
                    result=reply,
                )
                return reply
        except OperationConflict:
            raise FleetRefused("operation_conflict") from None
        except (FleetConflict, ScopeViolation):
            raise FleetRefused("control_evidence_invalid") from None

    async def retained_control(
        self,
        parent: WorkerPermissions,
        run: FleetRun,
        worker: FleetWorker | None,
        *,
        action: Control,
        operation_key: str,
        expected_version: int,
    ) -> Mapping[str, JsonValue] | None:
        return await self._control_record(
            parent,
            run,
            worker,
            action=action,
            operation_key=operation_key,
            expected_version=expected_version,
            write=False,
        )

    async def control(
        self,
        parent: WorkerPermissions,
        run: FleetRun,
        worker: FleetWorker | None,
        *,
        action: Control,
        operation_key: str,
        expected_version: int,
        resolved: tuple[ResolvedWorkerSpec, ...],
    ) -> Mapping[str, JsonValue]:
        reply = await self._control_record(
            parent,
            run,
            worker,
            action=action,
            operation_key=operation_key,
            expected_version=expected_version,
            write=True,
        )
        if reply is None:
            raise FleetRefused("control_evidence_unavailable")
        return reply
