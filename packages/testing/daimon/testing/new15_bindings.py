"""Frozen NEW15: genuine preparation/billing, then observed budget refusal.

All provider/runtime and gateway collaborators are explicit offline captures.
The first host turn is never manufactured. Outcomes are recomputed from facts;
this scoped proof does not certify the catalog's uncaptured global assertions.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import yaml
from daimon.adapters.discord.lifecycle import DiscordTurnLifecycle
from daimon.core.stores import accounts, identity
from daimon.core.stores.domain import PlatformPrincipalRow, Role
from daimon.core.turn.admission import AdmissionObserver, admit
from daimon.core.turn.binding import prepared_persistence
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.prepare import PreparedTurn, bind_session
from daimon.core.turn_hold import RequesterOrg
from daimon.testing.admin_bindings import AdminBindings, AdminCommand, AdminReceipt
from daimon.testing.admission_evidence import AdmissionEffects, AdmissionReceipt, admission_edge
from daimon.testing.channel_accounting import (
    ChannelAccounting,
    ChannelAccountingSnapshot,
    ControlledTurnPricing,
    MeteredChannelTurnReceipt,
)
from daimon.testing.discord_surface import DiscordSurfaceCapture, DiscordSurfaceHarness
from daimon.testing.outcome_oracle import CheckResult, OutcomeReport
from mux.contracts.events import Event, TurnEndedPayload
from mux.contracts.ids import Scope
from mux.errors import ScopeViolation

SCENARIO_ID = "QA-NEW15-CHANNEL-BUDGET-EXHAUSTED"
SOURCE_SHA256 = "8451c0e49a47642f87149ef65e126412ef38d48ec0d56f1b59b9ca415e9a5e25"


@dataclass(frozen=True)
class New15Run:
    scope: Scope
    admin: tuple[AdminReceipt, ...]
    metered: MeteredChannelTurnReceipt
    first_surface: DiscordSurfaceCapture
    first_terminal: tuple[Event, ...]
    admission: AdmissionReceipt | None
    readback: ChannelAccountingSnapshot
    cleanup: ChannelAccountingSnapshot
    source_sha256: str = SOURCE_SHA256

    @property
    def outcome(self) -> OutcomeReport:
        return new15_outcome(self)


@dataclass
class New15Bindings:
    admin: AdminBindings
    deps: TurnDeps
    scope: Scope
    channel_id: str
    thread_id: str
    external_user_id: str
    pricing: ControlledTurnPricing
    surface: Callable[[PreparedTurn], DiscordSurfaceHarness]
    invoke_denial: Callable[[str, AdmissionObserver], Awaitable[object]]
    effects: Callable[[], AdmissionEffects]
    native_send_count: Callable[[], int | None]
    clock: Callable[[], float] = time.monotonic
    """Capture-window clock shared with ``admission_edge``."""
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    """How the authored late-notice observation window elapses.

    The defaults observe the window in wall time. An injected pair may elapse it
    on a virtual timeline only if ``clock`` is the timeline every effect is
    stamped with and ``sleep`` runs the timers due before it returns; otherwise
    deferred work would be skipped while the receipt still claims the window."""

    async def authorize(self) -> None:
        await self.admin.authorize_command(
            AdminCommand(
                "new15_admission",
                self.admin.platform,
                self.admin.workspace_id,
                self.channel_id,
            )
        )
        if (
            self.deps.sessionmaker is not self.admin.sessions
            or self.deps.anthropic is not self.admin.anthropic
            or self.deps.deployment_default != self.admin.default
            or self.deps.public_url is not None
            or not self.deps.channel_backends
            or self.deps.turn_path != "mux"
            or self.scope.is_platform
            or self.scope.is_legacy_host_authorized
            or self.scope.tenant_id != self.admin.scope.tenant_id
            or not self.scope.authorization_id.strip()
            or not self.thread_id.strip()
        ):
            raise ScopeViolation("new15", "explicit owned offline collaborators required")
        async with self.admin.sessions() as db:
            principal = await identity.get_principal_by_id(
                db,
                principal_id=uuid.UUID(self.scope.principal_id),
                kind="platform",
            )
            account = await accounts.get_account(db, uuid.UUID(self.scope.account_id))
        if (
            not isinstance(principal, PlatformPrincipalRow)
            or account is None
            or str(principal.tenant_id) != self.scope.tenant_id
            or str(principal.account_id) != self.scope.account_id
            or str(account.tenant_id) != self.scope.tenant_id
            or account.role != Role.USER
            or principal.platform != self.admin.platform
            or principal.external_id != self.external_user_id
        ):
            raise ScopeViolation("new15", "actual owned platform member required")

    async def run(self, source: Path, *, evidence_id: str) -> New15Run:
        raw = source.read_bytes()
        if hashlib.sha256(raw).hexdigest() != SOURCE_SHA256 or not evidence_id.strip():
            raise ValueError("NEW15 requires original frozen bytes and evidence identity")
        authored = cast(dict[str, Any], yaml.safe_load(raw))
        await self.authorize()
        receipts = [await self.admin.configure_channel(self.channel_id)]
        values = {"channel_id": self.channel_id}
        # Preserve the original source's initial 0.01 budget. Repeat its literal
        # real mutation AFTER the metered turn, as requested by the lead.
        receipts.append(await self.admin.execute(authored["steps"][1], values))
        if receipts[-1].status != "executed":
            raise ValueError("NEW15 budget prerequisite did not execute")
        reader = ChannelAccounting(
            self.admin.sessions, self.scope, self.admin.platform, self.channel_id
        )
        metered: MeteredChannelTurnReceipt | None = None
        admission: AdmissionReceipt | None = None
        surface: DiscordSurfaceCapture | None = None
        terminal: tuple[Event, ...] = ()
        try:
            admitted = await admit(
                self.deps,
                tenant_id=uuid.UUID(self.scope.tenant_id),
                requester_org=RequesterOrg.HOST,
                platform=self.admin.platform,
                external_user_id=self.external_user_id,
                channel_id=self.channel_id,
                thread_id=self.thread_id,
                now=datetime.now(UTC),
            )
            prepared = await bind_session(
                self.deps,
                admitted,
                tenant_id=uuid.UUID(self.scope.tenant_id),
                platform=self.admin.platform,
                external_user_id=self.external_user_id,
                thread_id=self.thread_id,
                session_account_id=admitted.account_id,
                reuse_existing=True,
            )
            binding = None
            if self.admin.backend != "anthropic":
                persistence = await prepared_persistence(
                    self.deps,
                    prepared.admission,
                    tenant_id=uuid.UUID(self.scope.tenant_id),
                    platform=self.admin.platform,
                    thread_id=self.thread_id,
                    session_id=prepared.ma_session_id,
                    mapping_id=prepared.mapping_id,
                    session_ref=prepared.session_ref,
                )
                binding = persistence.binding
            harness = self.surface(prepared)
            try:

                async def first(lifecycle: DiscordTurnLifecycle) -> None:
                    nonlocal metered
                    await lifecycle.post_initial()
                    metered = await reader.run_turn(
                        deps=self.deps,
                        prepared=prepared,
                        pricing=self.pricing,
                        binding=binding,
                        thread_id=self.thread_id,
                        external_user_id=self.external_user_id,
                        user_message=authored["steps"][2]["text"],
                        lifecycle=lifecycle,
                        cancel=asyncio.Event(),
                        operation_key=f"{evidence_id}:first",
                    )

                surface = await harness.run(first)
            finally:
                await harness.client._connection.close()  # pyright: ignore[reportPrivateUsage]
            if self.deps.state_store is not None:
                terminal = tuple(
                    e
                    for e in await self.deps.state_store.read_events(prepared.ma_session_id)
                    if e.type == "session.turn_ended"
                    and e.authority == "record"
                    and e.payload.get("outcome") == "completed"
                )
                if len(terminal) == 1:
                    # Enrich metadata only from the actual durable normalized SDK
                    # terminal. No caller marker is promoted to a native root.
                    surface = surface.model_copy(
                        update={
                            "root_turn_id": TurnEndedPayload.model_validate(
                                terminal[0].payload
                            ).root_turn_id
                        }
                    )
            if metered is None:
                raise RuntimeError("host turn returned no accounting receipt")
            if metered.metered:
                receipts.append(await self.admin.execute(authored["steps"][1], values))
                if receipts[-1].status == "executed":

                    async def second(observer: AdmissionObserver) -> object:
                        result = await self.invoke_denial(authored["steps"][4]["text"], observer)
                        await self.sleep(float(authored["steps"][5]["s"]))
                        return result

                    admission = await admission_edge(
                        deps=self.deps,
                        scope=self.scope,
                        backend=self.admin.backend,
                        platform=self.admin.platform,
                        channel_id=self.channel_id,
                        thread_id=self.thread_id,
                        external_user_id=self.external_user_id,
                        evidence_id=f"{evidence_id}:second",
                        invoke=second,
                        effects=self.effects,
                        native_send_count=self.native_send_count,
                        clock=self.clock,
                    )
            readback = await reader.snapshot()
        finally:
            receipts.append(await self.admin.execute(authored["teardown"][0], values))
        if metered is None or surface is None:
            raise RuntimeError("NEW15 has no genuine first-turn capture")
        return New15Run(
            self.scope,
            tuple(receipts),
            metered,
            surface,
            terminal,
            admission,
            readback,
            await reader.snapshot(),
        )


def new15_outcome(run: New15Run) -> OutcomeReport:
    """Pinned predicates, recalculated from independent captures and readback."""
    DiscordSurfaceCapture.model_validate(run.first_surface.model_dump())
    if run.source_sha256 != SOURCE_SHA256:
        raise ValueError("NEW15 source identity is not the independently pinned original")
    if not run.admin:
        raise ValueError("NEW15 requires retained admin operations")
    before = run.metered.before
    identity = (before.tenant_id, before.platform, before.channel_id)
    if (
        str(before.tenant_id) != run.scope.tenant_id
        or any(
            (snapshot.tenant_id, snapshot.platform, snapshot.channel_id) != identity
            for snapshot in (run.metered.after, run.readback, run.cleanup)
        )
        or run.first_surface.session_id != run.metered.outcome.ma_session_id
        or (
            run.admission is not None
            and (
                run.admission.scope != run.scope
                or (run.admission.scope.tenant_id, run.admission.platform, run.admission.channel_id)
                != (str(before.tenant_id), before.platform, before.channel_id)
            )
        )
    ):
        raise ValueError("NEW15 capture is outside the owned channel/session")
    backend = run.admin[0].backend
    if any(r.backend != backend for r in run.admin) or (
        run.admission is not None and run.admission.backend != backend
    ):
        raise ValueError("NEW15 evidence differs from the selected backend")
    checks: list[CheckResult] = []

    def check(
        kind: str, turn: int | None, passed: bool, code: str, pending: str | None = None
    ) -> None:
        checks.append(
            CheckResult(
                kind=kind,
                turn=turn,
                status="PENDING" if pending else "PASS" if passed else "FAIL",
                code=pending or code,
            )
        )

    metered_gap = (
        None
        if run.metered.metered
        else "NEW15_FIRST_TURN_NOT_METERED:" + ",".join(run.metered.issues)
    )
    roots = tuple(
        TurnEndedPayload.model_validate(e.payload).root_turn_id
        for e in run.first_terminal
        if e.type == "session.turn_ended"
        and e.authority == "record"
        and e.session_id == run.metered.outcome.ma_session_id
        and e.payload.get("outcome") == "completed"
    )
    if len(roots) == 1 and run.first_surface.root_turn_id != roots[0]:
        raise ValueError("NEW15 surface root differs from the captured native terminal")
    if len(roots) != 1:
        metered_gap = metered_gap or "NEW15_FIRST_NATIVE_TERMINAL_UNBOUND"
    first_text = "\n".join(
        m.content + "\n" + "\n".join(m.embed_texts) for m in run.first_surface.messages
    )
    check(
        "text_present",
        1,
        "TINY-OK" in first_text,
        "ACTUAL_FIRST_DISCORD_TEXT",
        metered_gap
        or (None if run.first_surface.text_capture_complete else "NEW15_FIRST_SURFACE_INCOMPLETE"),
    )
    admission = run.admission
    capture_gaps: list[str] = []
    valid_notices = ()
    if admission is None:
        capture_gaps.append("NEW15_SECOND_ADMISSION_UNBOUND")
    else:
        if admission.effects.complete is not True:
            capture_gaps.append("NEW15_SECOND_EFFECT_COVERAGE_MISSING")
        if admission.native_sends is None:
            capture_gaps.append("NEW15_SECOND_NATIVE_SEND_COVERAGE_MISSING")
        elif type(admission.native_sends) is not int or admission.native_sends < 0:
            raise ValueError("NEW15 native send count is invalid")
        if (
            not math.isfinite(admission.started_s)
            or not math.isfinite(admission.observed_s)
            or admission.started_s < 0
            or admission.observed_s < admission.started_s
        ):
            raise ValueError("NEW15 admission capture window is invalid")
        # Authored wait is required independently of the stored capture gaps.
        if admission.observed_s - admission.started_s < 20:
            capture_gaps.append("NEW15_AUTHORED_TWENTY_SECOND_OBSERVATION_MISSING")
        for effect in (
            *admission.effects.notices,
            *admission.effects.logs,
            *admission.effects.reactions,
        ):
            if (
                not math.isfinite(effect.observed_s)
                or not admission.started_s <= effect.observed_s <= admission.observed_s
            ):
                raise ValueError("NEW15 adapter effect is outside the current capture window")
        decision = admission.decision
        if decision is None:
            capture_gaps.append("NEW15_SECOND_HOST_ADMISSION_UNBOUND")
        else:
            if (
                str(decision.tenant_id) != run.scope.tenant_id
                or decision.platform != admission.platform
                or decision.channel_id != admission.channel_id
                or decision.thread_id != admission.thread_id
                or decision.external_user_id != admission.external_user_id
                or (
                    decision.account_id is not None
                    and str(decision.account_id) != run.scope.account_id
                )
            ):
                raise ValueError("NEW15 admission decision differs from the owned attempt")
            if (
                admission.decision_s is None
                or not math.isfinite(admission.decision_s)
                or not admission.started_s <= admission.decision_s <= admission.observed_s
            ):
                raise ValueError("NEW15 decision is outside the current capture window")
        # Join second attempt to the independently captured first Discord thread
        # and persisted first-turn usage caller; neither receipt copy is authority.
        first_threads = {m.channel_id for m in run.first_surface.messages} | {
            e.message.channel_id for e in run.first_surface.events
        }
        first_callers = {
            row.platform_user_id
            for row in run.metered.after.usage
            if row.managed_session_id == run.metered.outcome.ma_session_id
            and row.tenant_id == before.tenant_id
            and row.channel_id == before.channel_id
        }
        readback_callers = {
            row.platform_user_id
            for row in run.readback.usage
            if row.managed_session_id == run.metered.outcome.ma_session_id
            and row.tenant_id == before.tenant_id
            and row.channel_id == before.channel_id
        }
        if not first_threads or not first_callers or not readback_callers:
            capture_gaps.append("NEW15_FIRST_ATTEMPT_IDENTITY_CAPTURE_MISSING")
        elif (
            first_threads != {admission.thread_id}
            or first_callers != {admission.external_user_id}
            or readback_callers != first_callers
        ):
            raise ValueError("NEW15 admission thread/caller differs from the first owned attempt")
        valid_notices = tuple(
            n
            for n in admission.effects.notices
            if admission.started_s <= n.observed_s <= admission.observed_s
            and n.observed_s <= admission.started_s + 15
        )
    # Stored gaps are conservative hints only. Empty stored gaps cannot certify
    # current facts whose coverage, duration or identity has been lost.
    gap = (
        metered_gap
        or (capture_gaps[0] if capture_gaps else None)
        or (
            "NEW15_SECOND_CAPTURE_INCOMPLETE"
            if admission is not None and admission.pending
            else None
        )
    )
    denied = (
        admission is not None
        and admission.decision is not None
        and admission.decision.reason == "channel_budget_exceeded"
        and admission.native_sends == 0
    )
    notices = admission.effects.notices if admission is not None else ()
    visible = any(n.text.strip() for n in valid_notices)
    check("no_silent_drop", 2, denied and visible, "REAL_DENIAL_NOTICE_AND_ZERO_NATIVE_SENDS", gap)
    check(
        "text_present",
        2,
        denied and any(re.search("spending budget|budget", n.text, re.I) for n in notices),
        "ACTUAL_BUDGET_NOTICE",
        gap,
    )
    check(
        "text_absent",
        2,
        denied and not any("TINY-AGAIN" in n.text for n in notices),
        "NO_SECOND_ANSWER",
        gap,
    )
    budget = run.readback.budget
    stable = (
        len(run.readback.debits) == 1
        and run.readback.debits == run.metered.after.debits
        and budget is not None
        and budget.is_exceeded
        and run.readback.net_debit_usd > 0
    )
    check(
        "ledger_debits", None, stable, "INDEPENDENT_SINGLE_DEBIT_AND_EXHAUSTED_BUDGET", metered_gap
    )
    check(
        "cleanup",
        None,
        run.admin[-1].status == "executed"
        and run.cleanup.budget is None
        and run.cleanup.debits == run.readback.debits,
        "REAL_BUDGET_CLEAR_AND_UNCHANGED_DEBITS",
    )
    return OutcomeReport(scenario_id=SCENARIO_ID, backend=backend, checks=tuple(checks))
