"""Starting runs: estimate, approve with a cap, cancel, list. Workers run the items
(`daimon.core.evals.runner`); spend is kept on the run (`spend.add_spend`)."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from typing import Final

from daimon.core.config import Settings
from daimon.core.evals.access import Subject
from daimon.core.evals.blind import new_run_id
from daimon.core.evals.cost import estimate_run, priced
from daimon.core.evals.format import (
    Approval,
    Arm,
    Case,
    Estimate,
    ItemCaseRef,
    ItemError,
    JsonObject,
    JudgeConfig,
    PrincipalRef,
    Run,
    RunItem,
    RunOrigin,
    RunStatus,
    Suite,
    SuitePin,
    to_document,
)
from daimon.core.evals.service import (
    BlindRun,
    EvalForbidden,
    EvalNotFound,
    EvalRefused,
    EvalService,
)
from daimon.core.stores import eval_runs
from daimon.core.stores import evals as store
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

MAX_ITEMS: Final = 5_000
REQUESTED_CAP: Final = "daimon.requested_cap_usd"
"""The cap a run waiting for an admin was asked for (`ext`)."""
_ENDED: Final[frozenset[RunStatus]] = frozenset({"done", "partial", "cancelled", "failed"})


class RunDraft(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    suite: SuitePin
    arm_ids: list[str] = Field(min_length=1)
    case_ids: list[str] | None = None
    repeats: int = Field(default=1, ge=1, le=20)
    judge_ids: list[str] = []
    name: str | None = Field(default=None, max_length=200)


class RunEstimate(Estimate):
    admin_approval_usd: float
    """Caps above this need an admin."""


@dataclass(frozen=True)
class _Plan:
    suite: store.SuiteRow
    version: int
    scrub: str
    cases: list[tuple[store.CaseRevisionRow, Case]]
    arms: list[Arm]
    judges: list[JudgeConfig]
    estimate: Estimate


def item_key(case_id: str, arm_id: str, repeat: int) -> str:
    key = f"{case_id}:{arm_id}:{repeat}"
    return key if len(key) <= 128 else f"item-{hashlib.sha256(key.encode()).hexdigest()[:32]}"


def _daimon_version() -> str | None:
    try:
        return version("daimon-core")
    except PackageNotFoundError:
        return None


def _newest(run: Run) -> datetime:
    at = run.started_at or (run.estimate.at if run.estimate else None)
    return at or datetime.min.replace(tzinfo=UTC)


class RunService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        settings: Settings,
        evals: EvalService,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions = sessions
        self._settings = settings
        self._evals = evals
        self._clock = clock

    async def _plan(self, session: AsyncSession, actor: Subject, draft: RunDraft) -> _Plan:
        suite, _, number = await self._evals.version_access(
            session, actor, draft.suite.id, draft.suite.version, "edit"
        )
        document = await store.suite_document(session, suite_id=suite.id, version=number)
        assert document is not None
        pinned = Suite.model_validate(document)
        rows = await store.version_cases(session, suite_id=suite.id, version=number)
        if draft.case_ids is not None:
            unknown = set(draft.case_ids) - {r.key for r in rows}
            if unknown:
                raise EvalNotFound(f"This suite version has no case {sorted(unknown)[0]}.")
            rows = [r for r in rows if r.key in draft.case_ids]
        if not rows:
            raise EvalRefused("Pick at least one case to run.")
        arms: list[Arm] = []
        for arm_id in dict.fromkeys(draft.arm_ids):
            row = await store.arm_by_key(session, tenant_id=actor.tenant_id, key=arm_id)
            if row is None:
                raise EvalNotFound(f"No arm {arm_id} here.")
            arms.append(Arm.model_validate(row.document))
        if not any(a.kind == "live" and a.live is not None for a in arms):
            raise EvalRefused("Pick at least one arm to run.")
        if any(a.live is not None and not priced(a.live) for a in arms):
            raise EvalRefused("An arm's model has no known price, so a cap can't hold it. "
                              "Pick another model.")  # fmt: skip
        offered = {j.id: j for j in (pinned.defaults and pinned.defaults.judges) or []}
        if missing := [j for j in draft.judge_ids if j not in offered]:
            raise EvalNotFound(f"This suite has no judge {missing[0]}.")
        cases = [(r, Case.model_validate(r.document)) for r in rows]
        if len(cases) * len(arms) * draft.repeats > MAX_ITEMS:
            raise EvalRefused(f"Runs hold at most {MAX_ITEMS} items. Pick fewer cases.")
        prior: dict[str, list[float]] = {}
        for arm in arms:
            if arm.config_hash is not None:
                prior[arm.id] = await eval_runs.prior_item_costs(
                    session, tenant_id=actor.tenant_id, arm_id=arm.id,
                    config_hash=arm.config_hash, case_keys=[r.key for r in rows],
                )  # fmt: skip
        settings = self._settings.evals
        estimate = estimate_run(
            [c for _, c in cases], arms, draft.repeats, [offered[j] for j in draft.judge_ids],
            prior=prior, simulator_model=settings.simulator_model,
            sandbox_rate_usd_per_hour=float(settings.sandbox_rate_usd_per_hour), at=self._clock(),
        )  # fmt: skip
        scrub = await store.version_scrub(session, suite_id=suite.id, version=number)
        return _Plan(
            suite=suite, version=number, scrub=scrub or "unscrubbed", cases=cases, arms=arms,
            judges=[offered[j] for j in draft.judge_ids], estimate=estimate,
        )  # fmt: skip

    async def estimate(self, actor: Subject, draft: RunDraft) -> RunEstimate:
        """What the draft should cost; editors and owners of its suite only."""
        await self._evals.check_on(actor)
        async with self._sessions() as session:
            plan = await self._plan(session, actor, draft)
        return RunEstimate(
            **plan.estimate.model_dump(), admin_approval_usd=self._settings.evals.admin_approval_usd
        )

    async def _past_limits(self, session: AsyncSession, actor: Subject, cap_usd: float) -> bool:
        """Whether the cap is past what the operator lets a workspace spend unasked: one run's
        limit, or the workspace's open runs' together. Locks the workspace's run starts."""
        limits = self._settings.evals
        if cap_usd > limits.admin_approval_usd:
            return True
        open_caps = await eval_runs.open_caps(session, tenant_id=actor.tenant_id)
        return open_caps + cap_usd > limits.workspace_cap_usd

    async def start(self, actor: Subject, draft: RunDraft, *, cap_usd: float) -> Run:
        """Queue a run with a spending cap. The operator pays, so a cap past their limits
        (`_past_limits`) from anyone else leaves the run `estimated`, waiting (`approve`)."""
        await self._evals.check_on(actor)
        if not cap_usd > 0:
            raise EvalRefused("Set a spend cap first.")
        now = self._clock()
        async with self._sessions() as session, session.begin():
            waiting = not actor.operator and await self._past_limits(session, actor, cap_usd)
            plan = await self._plan(session, actor, draft)
            run_id = new_run_id()
            run = Run(
                id=run_id, name=draft.name,
                suite=SuitePin(id=plan.suite.key, version=plan.version),
                arms=plan.arms, case_ids=draft.case_ids, repeats=draft.repeats,
                judges=plan.judges or None, status="estimated" if waiting else "approved",
                origin=RunOrigin(kind="local", daimon_version=_daimon_version()),
                estimate=plan.estimate,
                approval=None if waiting else self._approval(actor, now, cap_usd),
                ext={REQUESTED_CAP: cap_usd} if waiting else None,
            )  # fmt: skip
            items = [
                RunItem(
                    id=item_key(row.key, arm.id, repeat), run_id=run_id,
                    case=ItemCaseRef(id=row.key, revision=row.revision, hash=row.sha256),
                    arm_id=arm.id, repeat=repeat, status="pending",
                )
                for row, _ in plan.cases
                for arm in plan.arms
                if arm.kind == "live"
                for repeat in range(draft.repeats)
            ]  # fmt: skip
            stored = await store.add_run(
                session, tenant_id=actor.tenant_id, suite_id=plan.suite.id,
                document=to_document(run), items=[(to_document(i), ()) for i in items],
                scores=[], reviews=[], scrub_status=plan.scrub, created_by=actor.key,
            )  # fmt: skip
            await eval_runs.set_cap(session, run_id=stored, cap_usd=None if waiting else cap_usd)
        return run

    def _approval(self, actor: Subject, at: datetime, cap_usd: float) -> Approval:
        return Approval(by=PrincipalRef(kind="person", id=actor.key), at=at, cap_usd=cap_usd)

    async def approve(self, actor: Subject, run_id: str, *, cap_usd: float) -> Run:
        """An admin releases a run waiting for approval with a cap inside the operator's
        limits; past them, only the operator can."""
        await self._evals.check_on(actor)
        if not actor.is_admin:
            raise EvalForbidden("Ask an admin or an owner.")
        if not cap_usd > 0:
            raise EvalRefused("Set a spend cap first.")
        now = self._clock()
        async with self._sessions() as session, session.begin():
            row, _ = await self._evals.run_access(session, actor, run_id, "edit")
            if row.status != "estimated" or row.origin != "local":
                raise EvalRefused("This run isn't waiting for approval.")
            if not actor.operator and await self._past_limits(session, actor, cap_usd):
                raise EvalForbidden("Ask the person who runs Daimon to approve this cap.")

            def approved(document: JsonObject) -> JsonObject:
                ext = {k: v for k, v in (document.get("ext") or {}).items() if k != REQUESTED_CAP}  # pyright: ignore[reportAttributeAccessIssue,reportUnknownVariableType,reportUnknownMemberType]
                return {
                    **document, "status": "approved", "ext": ext or None,
                    "approval": to_document(self._approval(actor, now, cap_usd)),
                }  # fmt: skip

            updated = await store.update_run(session, run_id=row.id, change=approved)
            await eval_runs.set_cap(session, run_id=row.id, cap_usd=cap_usd)
        return Run.model_validate(updated.document)

    async def cancel(self, actor: Subject, run_id: str) -> Run:
        """Stop a run: no more items start, and workers interrupt the running ones."""
        await self._evals.check_on(actor)
        now = self._clock()
        async with self._sessions() as session, session.begin():
            row, _ = await self._evals.run_access(session, actor, run_id, "edit")
            if row.status in _ENDED:
                return Run.model_validate(row.document)
            updated = await store.update_run(
                session, run_id=row.id,
                change=lambda d: {**d, "status": "cancelled", "finished_at": now.isoformat()},
            )  # fmt: skip
            await eval_runs.close_items(
                session, run_id=row.id, status="cancelled",
                error=to_document(ItemError(kind="cancelled", message="the run was cancelled")),
            )  # fmt: skip
        return Run.model_validate(updated.document)

    async def runs(self, actor: Subject, suite_id: str | None = None) -> list[Run]:
        """Runs of the suites the actor may see outside review, newest first."""
        summaries = [s for s in await self._evals.suites(actor) if s.role != "reviewer"]
        found: list[Run] = []
        async with self._sessions() as session:
            for summary in summaries:
                if suite_id is not None and summary.id != suite_id:
                    continue
                suite = await store.suite_by_key(session, tenant_id=actor.tenant_id, key=summary.id)
                assert suite is not None
                for row in await store.runs_of_suite(session, suite_id=suite.id):
                    if summary.role == "owner" or row.scrub_status == "scrubbed":
                        found.append(Run.model_validate(row.document))
        return sorted(found, key=_newest, reverse=True)

    async def status(self, actor: Subject, run_id: str) -> Run | BlindRun:
        return await self._evals.run(actor, run_id)
