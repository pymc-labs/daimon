"""Read-only D11 joins of actual adapter posts and durable host accounting.

Explicit offline hooks only. Controlled prices certify host arithmetic, never live
spend. A request-span subtotal is not a complete turn, and C12 remains deferred.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import cast

from daimon.core.pricing import ModelRates, ProviderPrice, cost_of, provider_cost_of
from daimon.core.stores.domain import TenantLedgerRow
from daimon.core.stores.turn_usage import read_accounting_revisions
from daimon.core.tenant_balance import debit_amount
from daimon.testing.db import require_owned_sessionmaker
from daimon.testing.discord_surface import DiscordSurfaceCapture
from daimon.testing.outcome_oracle import CheckResult, Status, TerminalEvidence
from daimon.testing.platform_surface import PlatformSurfaceCapture
from daimon.testing.turn_accounting import (
    RevisionRateAttribution,
    TurnAccountingReader,
    TurnUsageInventory,
)
from mux.contracts.ids import ResourceRef, Scope
from mux.contracts.resources import ProviderBinding
from mux.contracts.usage import UsageObservation
from mux.state.usage_ledger import AppliedUsage, OutboxRow
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@dataclass(frozen=True)
class UsagePriceReceipt:
    """Independent normalized native revision and explicit offline price policy.

    Infrastructure None means unknown; zero must be explicitly justified by the
    caller's policy. Native meters/version strings are retained, never invented.
    This receipt supplies no completion or aggregate-coverage authority.
    """

    observation: UsageObservation
    price: ProviderPrice | None
    infrastructure_usd: Decimal | None
    infrastructure_evidence_id: str | None


@dataclass(frozen=True)
class SettledRevision:
    row: OutboxRow
    settled: bool


@dataclass(frozen=True)
class AccountingSnapshot:
    binding: ProviderBinding | None
    usage: tuple[AppliedUsage, ...]
    outbox: tuple[SettledRevision, ...]
    ledger: tuple[TenantLedgerRow, ...]


@dataclass(frozen=True)
class FooterAccountingReport:
    """Supplemental controlled arithmetic; never a live-spend certificate."""

    checks: tuple[CheckResult, ...]
    native_revisions: tuple[tuple[str, int, str | None], ...]
    ledger_usd: Decimal | None
    controlled_expected_usd: Decimal | None
    source: str = "controlled_offline_host_arithmetic"


@dataclass(frozen=True)
class FooterAccountingHook:
    sessions: async_sessionmaker[AsyncSession]
    scope: Scope
    binding: ProviderBinding
    native_session: ResourceRef
    root_turn_id: str

    def __post_init__(self) -> None:
        require_owned_sessionmaker(self.sessions)
        self._check_routing()
        uuid.UUID(self.scope.tenant_id)
        if (
            self.scope.is_platform
            or self.scope.is_legacy_host_authorized
            or self.native_session.kind != "session"
            or self.native_session.tenant_id != self.scope.tenant_id
            or self.native_session.account_id != self.scope.account_id
            or self.binding.provider != self.native_session.provider
            or self.binding.legacy_account_id not in (None, self.scope.account_id)
            or self.binding.thread.channel.tenant_id != self.scope.tenant_id
            or self.binding.thread.channel.platform not in ("discord", "slack", "teams")
            or self.binding.native_refs.get("session") != self.native_session.id
            or not self.root_turn_id.strip()
        ):
            raise ValueError("footer accounting requires one authorized adapter binding/root")

    def _check_routing(self) -> None:
        # A read-only, repeatable snapshot must use one connection for every
        # table. Two separately owned engines still cannot supply that fact.
        kwargs = cast(dict[str, object], self.sessions.kw)
        routed = kwargs.get("binds")
        if isinstance(routed, Mapping) and any(
            value is not kwargs.get("bind")
            for value in cast(Mapping[object, object], routed).values()
        ):
            raise ValueError("footer accounting reads require one consistent bind")

    async def snapshot(self) -> AccountingSnapshot:
        """Query independent persisted facts in one read-only consistent snapshot.

        Include all binding revisions so unattributed work cannot disappear from
        a root-specific filter. Every SELECT is tenant-scoped. No outbox claims,
        aggregate manufacture, settlement, applier locks or ledger writes occur.
        """
        require_owned_sessionmaker(self.sessions)
        self._check_routing()
        tenant = uuid.UUID(self.scope.tenant_id)
        async with self.sessions() as session, session.begin():
            await session.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            )
            rows = await read_accounting_revisions(session, tenant_id=tenant, binding=self.binding)
        return AccountingSnapshot(
            rows.binding,
            rows.revisions,
            tuple(SettledRevision(item.row, item.settled) for item in rows.outbox),
            rows.ledger,
        )

    async def evaluate(
        self,
        *,
        capture: DiscordSurfaceCapture,
        terminal: TerminalEvidence | None,
        receipts: tuple[UsagePriceReceipt, ...],
        markup: Decimal,
        turn: int = 1,
        tol_usd: Decimal = Decimal("0.001"),
        tol_pct: Decimal = Decimal("2"),
    ) -> FooterAccountingReport:
        """Join accepted footer, independent DB reads and native usage revisions.

        Caller must capture the native receipt/terminal from the real path, not
        read them back from this snapshot. No executor or product defaults change.
        """
        for value in (markup, tol_usd, tol_pct):
            if not value.is_finite() or value < 0:
                raise ValueError("markup and tolerances must be finite and nonnegative")
        if turn < 1:
            raise ValueError("turn must be positive")
        snapshot = await self.snapshot()
        ids = (capture.evidence_id, *(item.observation.id for item in receipts))
        revisions = tuple(
            (item.observation.id, item.observation.revision, item.observation.native_revision)
            for item in receipts
        )
        ledger_amount: Decimal | None = None
        expected: Decimal | None = None

        def result(status: Status, code: str) -> FooterAccountingReport:
            return FooterAccountingReport(
                checks=tuple(
                    CheckResult(kind=kind, turn=turn, status=status, code=code, evidence_ids=ids)
                    for kind in ("footer_cost_matches_ledger", "ledger_matches_usage")
                ),
                native_revisions=revisions,
                ledger_usd=ledger_amount,
                controlled_expected_usd=expected,
            )

        if snapshot.binding != self.binding:
            return result("PENDING", "BINDING_NOT_CURRENT")
        if (capture.session_id, capture.root_turn_id) != (
            self.native_session.id,
            self.root_turn_id,
        ) or any(
            message.channel_id != self.binding.thread.thread_id for message in capture.messages
        ):
            return result("FAIL", "POSTED_ROOT_IDENTITY_MISMATCH")
        if terminal is None or terminal.authority not in ("record", "reconciled"):
            return result("PENDING", "AUTHORITATIVE_ROOT_TERMINAL_MISSING")
        if (terminal.session_id, terminal.root_turn_id) != (
            self.native_session.id,
            self.root_turn_id,
        ) or terminal.outcome != "completed":
            return result("FAIL", "ROOT_TERMINAL_MISMATCH")
        if not capture.post_capture_complete or not capture.text_capture_complete:
            return result("PENDING", "POST_CAPTURE_INCOMPLETE")
        observations = tuple(item.observation for item in receipts)
        if not observations:
            return result("PENDING", "NATIVE_USAGE_MISSING")
        if len({item.id for item in observations}) != len(observations):
            return result("FAIL", "DUPLICATE_NATIVE_RECEIPT")
        if any(
            item.session != self.native_session or item.turn_id != self.root_turn_id
            for item in observations
        ):
            return result("FAIL", "NATIVE_ROOT_IDENTITY_MISMATCH")
        if any(item.model is None for item in observations):
            return result("PENDING", "NATIVE_MODEL_ATTRIBUTION_UNBOUND")
        if any(item.covers or item.thread_id is not None for item in observations):
            return result("PENDING", "AGGREGATE_COVERAGE_CUT")
        latest = {item.observation_id: item for item in snapshot.usage}
        root = {
            identity: item
            for identity, item in latest.items()
            if item.observation.session == self.native_session
            and item.observation.turn_id == self.root_turn_id
        }
        if any(
            item.observation.session == self.native_session and item.observation.turn_id is None
            for item in latest.values()
        ) or set(root) != {item.id for item in observations}:
            return result("PENDING", "ROOT_USAGE_COVERAGE_INCOMPLETE")
        if any(
            root[item.id].observation.model_dump(exclude={"observed_at"})
            != item.model_dump(exclude={"observed_at"})
            for item in observations
        ):
            return result("FAIL", "NATIVE_DURABLE_REVISION_MISMATCH")
        if any(item.completeness != "measured" or not item.native_meter for item in observations):
            return result("PENDING", "NATIVE_TOTALS_INCOMPLETE")
        selected = tuple(item for item in snapshot.outbox if item.row.observation_id in root)
        if any(not item.settled for item in selected) or any(
            not any(item.row.observation == value and item.settled for item in selected)
            for value in observations
        ):
            return result("PENDING", "OUTBOX_NOT_SETTLED")
        amounts: list[Decimal] = []
        for receipt in receipts:
            price, observation = receipt.price, receipt.observation
            if price is None or not price.verified:
                return result("PENDING", "PRICE_UNVERIFIED")
            if receipt.infrastructure_usd is None or not (
                receipt.infrastructure_evidence_id and receipt.infrastructure_evidence_id.strip()
            ):
                return result("PENDING", "INFRASTRUCTURE_UNVERIFIED")
            if observation.model is None or (price.provider, price.model) != (
                observation.model.provider,
                observation.model.id,
            ):
                return result("PENDING", "PRICE_IDENTITY_UNVERIFIED")
            if price.checked_on > observation.observed_at.date():
                return result("PENDING", "PRICE_POSTDATES_USAGE")
            actual = provider_cost_of(
                observation, price, infrastructure_usd=receipt.infrastructure_usd
            )
            if actual is None:
                return result("PENDING", "NATIVE_TOTALS_INCOMPLETE")
            if observation.session.provider == "anthropic":
                if receipt.infrastructure_usd != 0:
                    return result("PENDING", "ANTHROPIC_CONTAINER_JOIN_UNBOUND")
                legacy = cost_of(
                    observation,
                    ModelRates(
                        input=float(price.input),
                        output=float(price.output),
                        cache_write=float(price.cache_write or 0),
                        cache_read=float(price.cache_read),
                    ),
                )
                if legacy is None:
                    return result("PENDING", "NATIVE_TOTALS_INCOMPLETE")
                amounts.append(debit_amount(legacy, markup=markup))
            else:
                with localcontext() as context:
                    context.prec = 80
                    amounts.append((actual * markup).quantize(Decimal("0.000001")))
        expected = sum(amounts, Decimal(0))
        keys = {
            f"turn:{self.native_session.id}:{item.row.observation_id}"
            if item.row.revision == 1
            else f"adjust:{self.binding.id}:{item.row.observation_id}:{item.row.revision}"
            for item in selected
        }
        ledger = tuple(item for item in snapshot.ledger if item.idempotency_key in keys)
        if any(
            item.channel_id != self.binding.thread.channel.channel_id or item.reason != "turn_debit"
            for item in ledger
        ):
            return result("FAIL", "LEDGER_BILLING_CONTEXT_MISMATCH")
        if not ledger:
            return result("PENDING", "LEDGER_DEBIT_MISSING")
        ledger_amount = -sum((item.delta_usd for item in ledger), Decimal(0))
        if abs(ledger_amount - expected) > expected * tol_pct / Decimal(100):
            return result("FAIL", "LEDGER_NATIVE_USAGE_MISMATCH")
        # Request-span closure and delegated aggregates remain unbound. In
        # particular never synthesize an Anthropic turn total or C12 aggregate.
        complete = len(observations) == 1 and (
            observations[0].grain == "turn"
            and observations[0].basis == "cumulative"
            and not observations[0].covers
            and observations[0].thread_id is None
        )
        ledger_check = CheckResult(
            kind="ledger_matches_usage",
            turn=turn,
            status="PASS" if complete else "PENDING",
            code="CONTROLLED_HOST_ARITHMETIC_MATCH" if complete else "TURN_TOTAL_COVERAGE_UNBOUND",
            evidence_ids=ids,
        )
        footers: list[tuple[str, str]] = []
        for message in capture.messages:
            payload = json.loads(message.payload_json)
            for embed in payload.get("embeds", []):
                footer = embed.get("footer", {}).get("text")
                if isinstance(footer, str):
                    footers.append((message.message_id, footer))
        used = [
            match
            for _, footer in footers
            if (
                match := re.search(
                    r"(?:^|\u2003\u2003)(?:\d+s|\d+m \d+s|\d+h \d+m)\u2003\u2003"
                    r"(<)?\$(\d+(?:\.\d{1,3})?) used"
                    r"(?:\u2003\u2003[^\u2003]*\bleft)?$",
                    footer,
                )
            )
        ]
        if len(used) != 1:
            footer_status: Status = "PENDING"
            footer_code = "POSTED_COST_MISSING" if not used else "POSTED_COST_AMBIGUOUS"
        else:
            shown = Decimal(used[0][2])
            matches = (
                (0 <= ledger_amount < shown)
                if used[0][1]
                else abs(shown - ledger_amount) <= tol_usd
            )
            footer_status = "FAIL" if not matches else "PASS" if complete else "PENDING"
            footer_code = (
                "POSTED_COST_LEDGER_MISMATCH"
                if not matches
                else "CONTROLLED_POSTED_COST_MATCH"
                if complete
                else "TURN_TOTAL_COVERAGE_UNBOUND"
            )
        return FooterAccountingReport(
            checks=(
                CheckResult(
                    kind="footer_cost_matches_ledger",
                    turn=turn,
                    status=footer_status,
                    code=footer_code,
                    evidence_ids=ids,
                ),
                ledger_check,
            ),
            native_revisions=revisions,
            ledger_usd=ledger_amount,
            controlled_expected_usd=expected,
        )

    async def evaluate_turn(
        self,
        *,
        capture: DiscordSurfaceCapture | PlatformSurfaceCapture,
        terminal: TerminalEvidence | None,
        receipts: tuple[UsageObservation, ...],
        attributions: tuple[RevisionRateAttribution, ...] = (),
        inventory: TurnUsageInventory | None = None,
        markup: Decimal,
        turn: int = 1,
        tol_usd: Decimal = Decimal("0.001"),
    ) -> FooterAccountingReport:
        """Join an independently sealed turn to N8's owned accounting read API.

        Inventory and receipts come from native execution, not from queried rows.
        Prices/infrastructure require separate admitted evidence. Complete offline
        arithmetic can pass; the reader never supplies missing native facts.
        """
        if turn < 1 or not tol_usd.is_finite() or tol_usd < 0:
            raise ValueError("turn and footer tolerance must be positive/nonnegative")
        ids = tuple(
            dict.fromkeys(
                (
                    capture.evidence_id,
                    *(event.evidence_id for event in capture.events),
                    *((terminal.evidence_id,) if terminal is not None else ()),
                    *((inventory.evidence_id,) if inventory is not None else ()),
                    *(item.id for item in receipts),
                    *(
                        evidence
                        for item in attributions
                        for evidence in (
                            item.rate_evidence_id,
                            item.model_evidence_id,
                            item.infrastructure_evidence_id,
                        )
                        if evidence
                    ),
                )
            )
        )
        revisions = tuple((item.id, item.revision, item.native_revision) for item in receipts)

        try:
            accounting = await TurnAccountingReader(
                self.sessions, self.scope, self.binding, self.native_session, self.root_turn_id
            ).snapshot(attributions=attributions, inventory=inventory, markup=markup)
        except ValueError as error:
            # A supplied rate cannot bind a missing or unattributed persisted
            # root. That missing join is evidence absence, never a complete cost.
            if str(error) != "rate attribution must name a persisted revision of this root":
                raise
            return FooterAccountingReport(
                checks=tuple(
                    CheckResult(
                        kind=kind,
                        turn=turn,
                        status="PENDING",
                        code="ROOT_USAGE_ATTRIBUTION_UNBOUND",
                        evidence_ids=ids,
                    )
                    for kind in ("footer_cost_matches_ledger", "ledger_matches_usage")
                ),
                native_revisions=revisions,
                ledger_usd=None,
                controlled_expected_usd=None,
            )

        def report(status: Status, code: str) -> FooterAccountingReport:
            return FooterAccountingReport(
                checks=tuple(
                    CheckResult(kind=kind, turn=turn, status=status, code=code, evidence_ids=ids)
                    for kind in ("footer_cost_matches_ledger", "ledger_matches_usage")
                ),
                native_revisions=revisions,
                ledger_usd=accounting.ledger_usd,
                controlled_expected_usd=accounting.expected_ledger_usd,
            )

        if (capture.session_id, capture.root_turn_id) != (
            self.native_session.id,
            self.root_turn_id,
        ) or not _capture_matches_binding(capture, self.binding):
            return report("FAIL", "POSTED_ROOT_IDENTITY_MISMATCH")
        if terminal is None or terminal.authority not in ("record", "reconciled"):
            return report("PENDING", "AUTHORITATIVE_ROOT_TERMINAL_MISSING")
        if (terminal.session_id, terminal.root_turn_id) != (
            self.native_session.id,
            self.root_turn_id,
        ) or terminal.outcome != "completed":
            return report("FAIL", "ROOT_TERMINAL_MISMATCH")
        if not capture.post_capture_complete or not capture.text_capture_complete:
            return report("PENDING", "POST_CAPTURE_INCOMPLETE")
        if not receipts:
            return report("PENDING", "NATIVE_USAGE_MISSING")
        if len({item.id for item in receipts}) != len(receipts):
            return report("FAIL", "DUPLICATE_NATIVE_RECEIPT")
        if any(
            item.session != self.native_session or item.turn_id not in (None, self.root_turn_id)
            for item in receipts
        ):
            return report("FAIL", "NATIVE_ROOT_IDENTITY_MISMATCH")
        current = {
            item.observation.id: item.observation for item in accounting.revisions if item.latest
        }
        if current.keys() != {item.id for item in receipts}:
            return report("PENDING", "ROOT_USAGE_COVERAGE_INCOMPLETE")
        if any(
            current[item.id].model_dump(exclude={"observed_at"})
            != item.model_dump(exclude={"observed_at"})
            for item in receipts
        ):
            return report("FAIL", "NATIVE_DURABLE_REVISION_MISMATCH")
        # A ledger mismatch is meaningful only after the independent coverage,
        # pricing and settlement prerequisites are all established.
        if (
            accounting.coverage_complete
            and accounting.pricing_complete
            and accounting.settlement_complete
            and "LEDGER_COST_MISMATCH" in accounting.issues
        ):
            return report("FAIL", "LEDGER_NATIVE_USAGE_MISMATCH")
        if not accounting.complete:
            return report(
                "PENDING", accounting.issues[0] if accounting.issues else "TURN_COST_UNKNOWN"
            )
        ledger_amount = accounting.ledger_usd
        if ledger_amount is None or accounting.expected_ledger_usd is None:
            return report("PENDING", "TURN_COST_UNKNOWN")
        matches = [
            match
            for footer in _posted_footers(capture)
            if (
                match := re.search(
                    r"(?:^|\u2003\u2003)(?:\d+s|\d+m \d+s|\d+h \d+m)\u2003\u2003"
                    r"(<)?\$(\d+(?:\.\d{1,3})?) used"
                    r"(?:\u2003\u2003[^\u2003]*\bleft)?$",
                    footer,
                )
            )
        ]
        # At this point the real post window and independently priced/settled
        # native inventory are complete. A required absent cost is an executed
        # assertion failure, not missing execution evidence.
        footer_status: Status = "FAIL" if not matches else "PENDING"
        footer_code = "POSTED_COST_MISSING" if not matches else "POSTED_COST_AMBIGUOUS"
        if len(matches) == 1:
            shown = Decimal(matches[0][2])
            agrees = (
                0 <= ledger_amount < shown
                if matches[0][1]
                else abs(shown - ledger_amount) <= tol_usd
            )
            footer_status = "PASS" if agrees else "FAIL"
            footer_code = (
                "CONTROLLED_POSTED_COST_MATCH" if agrees else "POSTED_COST_LEDGER_MISMATCH"
            )
        return FooterAccountingReport(
            checks=(
                CheckResult(
                    kind="footer_cost_matches_ledger",
                    turn=turn,
                    status=footer_status,
                    code=footer_code,
                    evidence_ids=ids,
                ),
                CheckResult(
                    kind="ledger_matches_usage",
                    turn=turn,
                    status="PASS",
                    code="CONTROLLED_HOST_ARITHMETIC_MATCH",
                    evidence_ids=ids,
                ),
            ),
            native_revisions=revisions,
            ledger_usd=ledger_amount,
            controlled_expected_usd=accounting.expected_ledger_usd,
        )


def _capture_matches_binding(
    capture: DiscordSurfaceCapture | PlatformSurfaceCapture, binding: ProviderBinding
) -> bool:
    thread = binding.thread
    if isinstance(capture, DiscordSurfaceCapture):
        return thread.channel.platform == "discord" and all(
            message.channel_id == thread.thread_id for message in capture.messages
        )
    if capture.platform != thread.channel.platform:
        return False
    return all(
        message.destination_id
        == (thread.channel.channel_id if capture.platform == "slack" else thread.thread_id)
        and message.thread_id == (thread.thread_id if capture.platform == "slack" else None)
        for message in capture.messages
    )


def _posted_footers(
    capture: DiscordSurfaceCapture | PlatformSurfaceCapture,
) -> tuple[str, ...]:
    """Read accepted summary leaves; never answer, fallback, control or ledger text."""
    footers: list[str] = []
    for message in capture.messages:
        payload = json.loads(message.payload_json)
        if isinstance(capture, DiscordSurfaceCapture):
            for embed in payload.get("embeds", []):
                footer = embed.get("footer", {}).get("text")
                if isinstance(footer, str):
                    footers.append(footer)
        elif capture.platform == "slack":
            for block in payload.get("blocks", []):
                if block.get("type") == "context":
                    for element in block.get("elements", []):
                        footer = element.get("text")
                        if element.get("type") == "mrkdwn" and isinstance(footer, str):
                            footers.append(footer)
        else:
            for attachment in payload.get("attachments", []):
                if attachment.get("contentType") != "application/vnd.microsoft.card.adaptive":
                    continue
                for node in attachment.get("content", {}).get("body", []):
                    footer = node.get("text")
                    if (
                        node.get("type") == "TextBlock"
                        and node.get("size") == "Small"
                        and node.get("isSubtle") is True
                        and node.get("isVisible") is not False
                        and isinstance(footer, str)
                    ):
                        footers.append(footer)
    return tuple(footers)
