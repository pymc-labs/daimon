"""Explicit D11 native-turn billing and actual-post/owned-ledger binding.

Controlled rates prove host arithmetic only. Native receipts and inventory must
come from execution, independently of the database being checked.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, cast

from daimon.core.native_footer_cost import NativeFooterCostContext
from daimon.core.pricing import ProviderPrice
from daimon.core.usage_billing import ObservationBilled
from daimon.core.usage_recording import record_provider_usage
from daimon.testing.discord_surface import (
    DiscordSurfaceCapture,
    _component_labels,  # pyright: ignore[reportPrivateUsage]
    _embed_texts,  # pyright: ignore[reportPrivateUsage]
)
from daimon.testing.footer_accounting import FooterAccountingHook, FooterAccountingReport
from daimon.testing.outcome_oracle import (
    CheckResult,
    OutcomeReport,
    RunEvidence,
    VisibleText,
    evaluate,
)
from daimon.testing.platform_surface import PlatformSurfaceCapture
from daimon.testing.product_profiles import OPT_IN_PASS, QAProductProfile
from daimon.testing.slack_surface import (
    _decode as decode_slack,  # pyright: ignore[reportPrivateUsage]
)
from daimon.testing.teams_surface import (
    _decode as decode_teams,  # pyright: ignore[reportPrivateUsage]
)
from daimon.testing.turn_accounting import RevisionRateAttribution, TurnUsageInventory
from mux.contracts.ids import ModelRef, ResourceRef, Scope
from mux.contracts.resources import ProviderBinding
from mux.contracts.usage import UsageObservation
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

SCENARIO = "QA-D11-FOOTER-COST-MATCHES-LEDGER"
type Capture = DiscordSurfaceCapture | PlatformSurfaceCapture


@dataclass(frozen=True)
class D11Result:
    evidence: RunEvidence
    outcome: OutcomeReport
    accounting: FooterAccountingReport
    profile: QAProductProfile

    @property
    def status(self) -> str:
        if self.outcome.status == "PASS" and self.profile.flags["core.turn.native_footer_cost"]:
            return OPT_IN_PASS
        return self.outcome.status


@dataclass(frozen=True)
class D11TurnBinding:
    """One authorized attempt; install billing before dispatch, evaluate after posts.

    The caller admits a real binding/root/model before execution. Missing rates
    stay unknown; a tariff never provides model identity or completion authority.
    Fresh instances are required for each profile and attempt.
    """

    sessions: async_sessionmaker[AsyncSession]
    scope: Scope
    binding: ProviderBinding
    native_session: ResourceRef
    root_turn_id: str
    capture_id: str
    profile: QAProductProfile
    admitted_model: ModelRef
    price: ProviderPrice | None
    markup: Decimal
    platform_user_id: str
    rate_evidence_id: str | None = None
    model_evidence_id: str | None = None
    infrastructure_usd: Decimal | None = None
    infrastructure_seconds: Decimal | None = None
    infrastructure_policy: str | None = None
    infrastructure_evidence_id: str | None = None
    _issued: bool = field(default=False, init=False)
    _cost: NativeFooterCostContext | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self._admit()
        if (
            self.native_session.provider not in ("openai", "gemini")
            or self.admitted_model.provider != self.native_session.provider
            or not self.capture_id.strip()
            or not self.platform_user_id.strip()
            or not self.markup.is_finite()
            or self.markup < 0
            or (
                self.price is not None
                and (
                    self.price.provider != self.admitted_model.provider
                    or self.price.model != self.admitted_model.id
                )
            )
        ):
            raise ValueError("D11 requires an admitted native model, profile and capture")
        # Reuse the rate policy's validation, including explicit zero vs unknown.
        self.attribution("admission-policy", 1)

    def _admit(self) -> FooterAccountingHook:
        return FooterAccountingHook(
            self.sessions, self.scope, self.binding, self.native_session, self.root_turn_id
        )

    def attribution(self, identity: str, revision: int) -> RevisionRateAttribution:
        if self.infrastructure_policy not in (None, "measured", "not_applicable"):
            raise ValueError("unknown infrastructure policy")
        return RevisionRateAttribution(
            observation_id=identity,
            revision=revision,
            price=self.price,
            rate_evidence_id=self.rate_evidence_id,
            admitted_model=self.admitted_model,
            model_evidence_id=self.model_evidence_id,
            infrastructure_policy=self.infrastructure_policy,
            infrastructure_usd=self.infrastructure_usd,
            infrastructure_seconds=self.infrastructure_seconds,
            infrastructure_evidence_id=self.infrastructure_evidence_id,
        )

    def billing(self) -> ObservationBilled:
        """Real recorder, with the opt-in context only on the opted native path."""
        self._admit()
        if self._issued:
            raise ValueError("D11 billing binding cannot be reused")
        object.__setattr__(self, "_issued", True)
        if self.profile.flags["core.turn.native_footer_cost"]:
            object.__setattr__(
                self,
                "_cost",
                NativeFooterCostContext(
                    self.sessions, self.scope, self.binding, self.native_session, self.markup
                ),
            )
        return ObservationBilled(self._record, native_footer_cost=self._cost)

    async def _record(self, *, observation: UsageObservation) -> bool:
        self._admit()  # Every bind, including changed routing, before any connection.
        if (
            observation.session != self.native_session
            or observation.turn_id != self.root_turn_id
            or observation.model not in (None, self.admitted_model)
        ):
            raise ValueError("D11 recorder refuses foreign native usage")
        normalized = observation.model_copy(update={"model": self.admitted_model})
        price = self.price if self.rate_evidence_id and self.model_evidence_id else None
        infra = self.infrastructure_usd if self.infrastructure_evidence_id else None
        if self.infrastructure_policy is None or (
            self.infrastructure_policy == "measured" and self.infrastructure_seconds is None
        ):
            infra = None
        settled = await record_provider_usage(
            sessionmaker=self.sessions,
            binding_id=self.binding.id,
            observation=normalized,
            tenant_id=uuid.UUID(self.scope.tenant_id),
            platform_user_id=self.platform_user_id,
            provider_price=price,
            infrastructure_usd=infra,
            billing_grain="turn",
            managed_session_id=self.native_session.id,
            model_id=self.admitted_model.id,
            markup=self.markup,
            channel_id=self.binding.thread.channel.channel_id,
        )
        if self._cost is not None:
            self._cost.note(
                normalized,
                price=price,
                infrastructure_usd=infra,
                infrastructure_seconds=self.infrastructure_seconds,
                settled=settled,
            )
        return settled

    async def evaluate(
        self,
        *,
        evidence: RunEvidence,
        capture: Capture,
        receipts: tuple[UsageObservation, ...],
        inventory: TurnUsageInventory | None,
        assertions: Sequence[Mapping[str, JsonValue]],
    ) -> D11Result:
        """Preserve all source assertions; replace only their accounting joins.

        This verdict cannot discharge independent catalog/ingress/source gaps in
        the runner. It supplies no invented inventory, receipts or provider root.
        """
        hook = self._admit()
        if not self._issued:
            raise ValueError("D11 billing must be installed before native dispatch")
        if evidence.scenario_id != SCENARIO or evidence.backend != self.native_session.provider:
            raise ValueError("D11 evidence belongs to another scenario/provider")
        if len(evidence.turns) != 1:
            raise ValueError("D11 binding requires one actual turn")
        turn = evidence.turns[0]
        if (
            turn.turn != 1
            or turn.session_id != self.native_session.id
            or turn.root_turn_id != self.root_turn_id
            or capture.evidence_id != self.capture_id
            or capture.session_id != turn.session_id
            or capture.root_turn_id != turn.root_turn_id
            or capture.started_s < turn.started_s
        ):
            raise ValueError("D11 actual capture differs from the authorized attempt")
        texts = _posted_text(capture, self.binding)
        # Preserve every non-text stream and its ordering. Texts follow those
        # streams only when their clocks allow it; otherwise refuse the join.
        start_order = (
            max((item.order for item in turn.host_effects() if item not in turn.texts), default=-1)
            + 1
        )
        visible = tuple(
            VisibleText(
                evidence_id=f"{capture.evidence_id}:visible:{i}",
                message_id=identity,
                order=start_order + i,
                observed_s=capture.observed_s,
                text=text,
            )
            for i, (identity, text) in enumerate(texts)
        )
        bound_turn = type(turn).model_validate_json(
            turn.model_copy(
                update={
                    "texts": visible,
                    "text_capture_complete": capture.text_capture_complete,
                }
            ).model_dump_json()
        )
        bound = RunEvidence(
            scenario_id=evidence.scenario_id, backend=evidence.backend, turns=(bound_turn,)
        )
        base = evaluate(bound, assertions)
        accounting_assertions = [
            a
            for a in assertions
            if a.get("kind") in ("footer_cost_matches_ledger", "ledger_matches_usage")
        ]
        if (
            len(accounting_assertions) != 2
            or {str(a.get("kind")) for a in accounting_assertions}
            != {"footer_cost_matches_ledger", "ledger_matches_usage"}
            or any(a.get("turn") != 1 for a in accounting_assertions)
        ):
            raise ValueError("D11 requires both unchanged turn-one accounting predicates")
        # The host arithmetic is exact; preserve the footer's source tolerance.
        footer = next(a for a in accounting_assertions if a["kind"] == "footer_cost_matches_ledger")
        tolerance = Decimal(str(footer.get("tol_usd", "0.001")))
        terminal = turn.terminals[-1] if turn.terminals else None
        report = await hook.evaluate_turn(
            capture=capture,
            terminal=terminal,
            receipts=receipts,
            inventory=inventory,
            attributions=tuple(self.attribution(r.id, r.revision) for r in receipts),
            markup=self.markup,
            tol_usd=tolerance,
        )
        replacements = {c.kind: c for c in report.checks}
        checks = tuple(replacements.get(check.kind, check) for check in base.checks)
        if self.profile.flags["core.turn.native_footer_cost"] and (
            self._cost is None
            or not self._cost.finished
            or self._cost.root_turn_id != self.root_turn_id
            or self._cost.result.status != "complete"
        ):
            checks += (
                CheckResult(
                    kind="native_footer_cost_binding",
                    turn=1,
                    status="PENDING",
                    code="OPT_IN_NATIVE_COST_CONTEXT_INCOMPLETE",
                    evidence_ids=(capture.evidence_id,),
                ),
            )

        outcome = OutcomeReport(scenario_id=base.scenario_id, backend=base.backend, checks=checks)
        return D11Result(bound, outcome, report, self.profile)


def _posted_text(capture: Capture, binding: ProviderBinding) -> tuple[tuple[str, str], ...]:
    """Validate accepted history and raw payload projection before the ledger read."""
    capture = type(capture).model_validate_json(capture.model_dump_json())
    platform = "discord" if isinstance(capture, DiscordSurfaceCapture) else capture.platform
    if platform != binding.thread.channel.platform:
        raise ValueError("D11 capture uses another platform")
    survivors: dict[str, dict[str, object]] = {}
    created: set[str] = set()
    accepted_times: dict[str, float] = {}
    last_s = capture.started_s
    for index, event in enumerate(capture.events):
        message = event.message
        if (
            event.evidence_id != f"{capture.evidence_id}:post:{index}"
            or message.observed_s < last_s
        ):
            raise ValueError("D11 post history is incomplete or unordered")
        last_s = message.observed_s
        accepted_times[message.message_id] = message.observed_s
        raw_payload: object = json.loads(message.payload_json)
        if not isinstance(raw_payload, dict):
            raise ValueError("D11 accepted payload must be an object")
        payload = cast(dict[str, Any], raw_payload)
        if isinstance(capture, DiscordSurfaceCapture):
            discord = DiscordSurfaceCapture.model_validate_json(capture.model_dump_json())
            item = discord.events[index].message
            if (
                item.channel_id != binding.thread.thread_id
                or item.content != (payload.get("content") or "")
                or item.embed_texts != _embed_texts(payload)
                or item.component_labels != _component_labels(payload.get("components", []))
            ):
                raise ValueError("D11 Discord text differs from accepted payload")
        else:
            item = capture.events[index].message
            decoded = (decode_slack if platform == "slack" else decode_teams)(payload)
            if (
                item.destination_id != binding.thread.channel.channel_id
                or (item.thread_id or item.destination_id) != binding.thread.thread_id
                or any(getattr(item, key) != value for key, value in decoded.items())
            ):
                raise ValueError("D11 platform text differs from accepted payload")
        dumped = cast(dict[str, object], message.model_dump(exclude={"observed_s"}))
        if event.operation == "send":
            if message.message_id in created or message.order != len(created):
                raise ValueError("D11 post history has duplicate creation")
            created.add(message.message_id)
        elif message.message_id not in survivors:
            raise ValueError("D11 post history lacks original creation")
        elif message.order != survivors[message.message_id]["order"]:
            raise ValueError("D11 edit changed message order")
        if event.operation == "delete":
            if dumped != survivors.pop(message.message_id):
                raise ValueError("D11 deletion changed payload")
        else:
            survivors[message.message_id] = dumped
    if tuple(survivors.values()) != tuple(
        m.model_dump(exclude={"observed_s"}) for m in capture.messages
    ):
        raise ValueError("D11 surviving posts differ from accepted history")
    if any(
        not accepted_times[m.message_id] <= m.observed_s <= capture.observed_s
        for m in capture.messages
    ):
        raise ValueError("D11 final posts are outside the captured window")
    if isinstance(capture, DiscordSurfaceCapture):
        return tuple(
            (m.message_id, "\n".join((m.content, *m.embed_texts, *m.component_labels)))
            for m in capture.messages
        )
    return tuple(
        (m.message_id, "\n".join((*m.body_texts, *m.card_texts, *m.control_labels)))
        for m in capture.messages
    )
