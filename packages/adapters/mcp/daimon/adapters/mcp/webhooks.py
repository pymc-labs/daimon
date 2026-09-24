"""Webhook handlers.

The GitHub OAuth web flow has been removed entirely; the GitHub
webhook here is skill-sync's push-driven resync trigger only, decoupled from
App-clone credential resolution.

Per `guideline:architecture` "no module-level singletons": Stripe billing
config is INJECTED into the factory by `create_mcp_app`. The factory returns
a closure with the config bound. Boot fails fast on misconfig (Pitfall 5).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

import structlog
from daimon.core.billing import BillingConfig
from daimon.core.config import GithubSettings
from daimon.core.github_app_auth import verify_signature
from daimon.core.github_repo_auth import normalize_owner_repo
from daimon.core.stores import (
    github_installation_reconciliation,
    github_push_resync,
    payment_events,
    pending_clawbacks,
    tenant_ledger,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request
from starlette.responses import Response

if TYPE_CHECKING:
    # Billing is optional; ``stripe`` is imported lazily at runtime inside the handler.
    import stripe

log = structlog.get_logger(__name__)

_PENDING_CLAWBACK_RETENTION = timedelta(days=90)


class _PaymentCreditConflict(RuntimeError):
    """A payment intent already has a credit that does not match this event."""

    def __init__(
        self,
        *,
        payment_intent: str | None,
        tenant_id: str,
        existing_tenant_id: str | None,
        amount_usd: str,
        existing_amount_usd: str | None,
    ) -> None:
        super().__init__("payment intent credit conflicts with completed event")
        self.payment_intent = payment_intent
        self.tenant_id = tenant_id
        self.existing_tenant_id = existing_tenant_id
        self.amount_usd = amount_usd
        self.existing_amount_usd = existing_amount_usd


def _get(d: dict[str, Any], key: str) -> object:
    """Type-safe dict.get wrapper for JSON payload dicts.

    Pyright strict mode reports 'partially unknown' on dict[str, Any].get() because
    the return type includes Any. This helper collapses that into a single typed
    extraction point so call sites don't need individual pyright: ignore comments.
    """
    return d.get(key)  # pyright: ignore[reportUnknownMemberType]


def build_github_webhook(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    github_settings: GithubSettings,
) -> Callable[[Request], Awaitable[Response]]:
    """Construct a GitHub App webhook handler with collaborators bound. SC-3.

    Handler flow:
      1. Read raw body bytes (Pitfall 4 — never re-serialize via json.dumps).
      2. Verify HMAC-SHA256 signature (X-Hub-Signature-256) BEFORE parsing JSON.
         Forged/unsigned -> 401 immediately (SC-3).
      3. Dispatch on X-GitHub-Event header:
         - push: extract repository.full_name + ref; missing -> 200 no-op + log.warning.
           Otherwise: persist a coalesced resync job and delivery receipt before 200.
         - installation: upsert install store (created / deleted); 200.
         - installation_repositories: add_repos / remove_repos; 200.
         - anything else: log info + 200 no-op.
      4. Every path logs x-github-delivery; NEVER logs the secret, PEM, token, or PAT.
    """
    webhook_secret = github_settings.webhook_secret
    if webhook_secret is None:
        raise ValueError(
            "GitHub App webhook_secret must be configured to mount /webhooks/github "
            "(DAIMON_GITHUB__WEBHOOK_SECRET env var)"
        )

    async def handler(request: Request) -> Response:
        body = await request.body()  # raw bytes — BEFORE json.loads (Pitfall 4)
        delivery_id = request.headers.get("x-github-delivery", "")
        sig_header = request.headers.get("x-hub-signature-256", "")

        # SC-3: verify signature BEFORE parsing — reject forged/unsigned deliveries
        if not verify_signature(webhook_secret.get_secret_value(), body, sig_header):
            log.warning(
                "github.webhook.bad_signature",
                delivery_id=delivery_id,
            )
            return Response(status_code=401)

        event = request.headers.get("x-github-event", "")

        try:
            parsed: Any = json.loads(body)  # pyright: ignore[reportExplicitAny]
        except (json.JSONDecodeError, ValueError):
            log.warning("github.webhook.parse_error", delivery_id=delivery_id, event=event)
            return Response(status_code=200)
        if not isinstance(parsed, dict):
            log.warning("github.webhook.non_object_payload", delivery_id=delivery_id, event=event)
            return Response(status_code=200)
        payload: dict[str, Any] = parsed  # pyright: ignore[reportUnknownVariableType]

        # --- push: durably enqueue resync before acknowledging the delivery ---
        if event == "push":
            repo_info = _get(payload, "repository")
            ref_raw = _get(payload, "ref")
            if not isinstance(repo_info, dict) or not isinstance(ref_raw, str):
                log.warning(
                    "github.webhook.malformed_push",
                    delivery_id=delivery_id,
                    missing="repository or ref",
                )
                return Response(status_code=200)
            full_name_val = _get(repo_info, "full_name")  # pyright: ignore[reportUnknownArgumentType]
            full_name_raw: str = str(full_name_val) if isinstance(full_name_val, str) else ""
            ref: str = ref_raw
            if not full_name_raw:
                log.warning(
                    "github.webhook.malformed_push",
                    delivery_id=delivery_id,
                    missing="repository.full_name",
                )
                return Response(status_code=400)
            if not delivery_id or len(delivery_id) > 255:
                log.warning("github.webhook.malformed_delivery_id", event=event)
                return Response(status_code=400)

            full_name = normalize_owner_repo(full_name_raw)
            repo_parts = full_name.split("/")
            if len(repo_parts) != 2 or not all(repo_parts):
                log.warning(
                    "github.webhook.malformed_push",
                    delivery_id=delivery_id,
                    missing="canonical repository owner/name",
                )
                return Response(status_code=400)

            async with sessionmaker.begin() as session:
                enqueued = await github_push_resync.enqueue(
                    session,
                    repo_full_name=full_name,
                    ref=ref,
                    delivery_id=delivery_id,
                )
            log.info(
                "github.webhook.push_persisted",
                delivery_id=delivery_id,
                repo=full_name,
                ref=ref,
                enqueued=enqueued,
            )
            return Response(status_code=200)

        # --- installation lifecycle: upsert / delete ---
        if event == "installation":
            return await _handle_installation(
                sessionmaker=sessionmaker,
                payload=payload,
                delivery_id=delivery_id,
            )

        # --- installation_repositories: add / remove repos ---
        if event == "installation_repositories":
            return await _handle_installation_repositories(
                sessionmaker=sessionmaker,
                payload=payload,
                delivery_id=delivery_id,
            )

        log.info(
            "github.webhook.unhandled_type",
            github_event=event,
            delivery_id=delivery_id,
        )
        return Response(status_code=200)

    return handler


async def _handle_installation(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    payload: dict[str, Any],
    delivery_id: str,
) -> Response:
    """Queue an authoritative installation refresh; delete clears cache now."""
    action = _get(payload, "action")
    if action not in ("created", "deleted"):
        log.info(
            "github.webhook.installation_action_ignored",
            action=action,
            delivery_id=delivery_id,
        )
        return Response(status_code=200)

    install_info = _get(payload, "installation")
    if not isinstance(install_info, dict):
        log.warning("github.webhook.malformed_installation", delivery_id=delivery_id)
        return Response(status_code=200)

    installation_id_raw = _get(install_info, "id")  # pyright: ignore[reportUnknownArgumentType]
    if (
        not isinstance(installation_id_raw, int)
        or isinstance(installation_id_raw, bool)
        or installation_id_raw <= 0
    ):
        log.warning(
            "github.webhook.malformed_installation_id",
            delivery_id=delivery_id,
        )
        return Response(status_code=400)
    installation_id: int = installation_id_raw
    if not delivery_id or len(delivery_id) > 255:
        log.warning("github.webhook.malformed_delivery_id", event="installation")
        return Response(status_code=400)

    async with sessionmaker.begin() as session:
        enqueued = await github_installation_reconciliation.enqueue(
            session,
            installation_id=installation_id,
            delivery_id=delivery_id,
            event="installation",
            deleted=action == "deleted",
            now=datetime.now(UTC),
        )
    log.info(
        "github.webhook.installation_reconciliation_enqueued",
        delivery_id=delivery_id,
        installation_id=installation_id,
        deleted=action == "deleted",
        enqueued=enqueued,
    )

    return Response(status_code=200)


async def _handle_installation_repositories(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    payload: dict[str, Any],
    delivery_id: str,
) -> Response:
    """Queue a repository-set refresh; delta payloads are ordering signals only."""
    action = _get(payload, "action")
    if action not in ("added", "removed"):
        log.info(
            "github.webhook.installation_repositories_action_ignored",
            action=action,
            delivery_id=delivery_id,
        )
        return Response(status_code=200)

    install_info = _get(payload, "installation")
    if not isinstance(install_info, dict):
        log.warning("github.webhook.malformed_installation_repositories", delivery_id=delivery_id)
        return Response(status_code=200)

    installation_id_raw = _get(install_info, "id")  # pyright: ignore[reportUnknownArgumentType]
    if (
        not isinstance(installation_id_raw, int)
        or isinstance(installation_id_raw, bool)
        or installation_id_raw <= 0
    ):
        log.warning(
            "github.webhook.malformed_installation_id",
            delivery_id=delivery_id,
        )
        return Response(status_code=400)
    installation_id: int = installation_id_raw
    if not delivery_id or len(delivery_id) > 255:
        log.warning("github.webhook.malformed_delivery_id", event="installation_repositories")
        return Response(status_code=400)
    async with sessionmaker.begin() as session:
        enqueued = await github_installation_reconciliation.enqueue(
            session,
            installation_id=installation_id,
            delivery_id=delivery_id,
            event="installation_repositories",
            deleted=False,
            now=datetime.now(UTC),
        )

    log.info(
        "github.webhook.installation_reconciliation_enqueued",
        delivery_id=delivery_id,
        installation_id=installation_id,
        enqueued=enqueued,
    )
    return Response(status_code=200)


def build_stripe_webhook(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    billing_config: BillingConfig,
) -> Callable[[Request], Awaitable[Response]]:
    """Construct a Stripe webhook handler with sessionmaker + config bound.

    Handler flow:
      1. Read raw body bytes (Pitfall 4 — never re-serialize via json.dumps)
      2. Verify signature via stripe.Webhook.construct_event; on fail -> 400
      3. Dispatch on event.type:
         - checkout.session.completed  -> credit (CAS-gated)
         - charge.refunded             -> clawback (idempotent negative row)
         - charge.dispute.created      -> clawback (idempotent negative row)
         - anything else               -> 200 no-op
      4. For completed: validate metadata (tenant_id, amount_total);
         missing/malformed -> 200 no-op (RESEARCH OQ #4)
      5. payment_events.upsert_for_dedup(...) + try_claim_credit(...)
      6. If claimed: tenant_ledger.insert_entry (credit for completed; negative for clawback)
      7. 200
    """
    import stripe
    from stripe import SignatureVerificationError

    async def handler(request: Request) -> Response:
        body = await request.body()  # Pitfall 4 — raw bytes, no json round-trip
        sig = request.headers.get("stripe-signature", "")

        try:
            event = stripe.Webhook.construct_event(  # pyright: ignore[reportUnknownMemberType]
                payload=body,
                sig_header=sig,
                secret=billing_config.webhook_secret.get_secret_value(),
            )
        except SignatureVerificationError:
            return Response(status_code=400)
        except ValueError:
            return Response(status_code=400)

        event_id: str = event.id
        event_type: str = event.type

        # --- completed checkout: credit the tenant ledger ---
        if event_type == "checkout.session.completed":
            try:
                return await _handle_completed(sessionmaker, event_id, event)
            except _PaymentCreditConflict as err:
                log.error(
                    "stripe.webhook.payment_credit_conflict",
                    event_id=event_id,
                    payment_intent=err.payment_intent,
                    tenant_id=err.tenant_id,
                    existing_tenant_id=err.existing_tenant_id,
                    amount_usd=err.amount_usd,
                    existing_amount_usd=err.existing_amount_usd,
                )
                # Preserve Stripe retries so an operator can repair inconsistent
                # ledger state and replay the event. The failed transaction rolls
                # back the dedup row and claim, so no cross-tenant credit is recorded.
                return Response(status_code=500)

        # --- refund / dispute: clawback ---
        if event_type in ("charge.refunded", "charge.dispute.created"):
            return await _handle_clawback(sessionmaker, event_id, event_type, event)

        log.info(
            "stripe.webhook.unhandled_type",
            event_type=event_type,
            event_id=event_id,
        )
        return Response(status_code=200)

    return handler


async def _handle_completed(
    sessionmaker: async_sessionmaker[AsyncSession],
    event_id: str,
    event: stripe.Event,
) -> Response:
    """Credit the tenant balance for a completed Checkout Session.

    CR-02: credit amount is read from session.amount_total (Stripe-authoritative integer
    minor units), NOT from metadata["amount_usd"] which the caller controls. Tenant routing
    still uses metadata.tenant_id (our own value, embedded at checkout creation time).
    """
    metadata_raw: Any = getattr(event.data.object, "metadata", None) or {}  # pyright: ignore[reportExplicitAny]
    if not isinstance(metadata_raw, dict):
        log.warning("stripe.webhook.non_dict_metadata", event_id=event_id)
        return Response(status_code=200)
    metadata: dict[str, Any] = dict(metadata_raw)  # pyright: ignore[reportUnknownArgumentType]

    tenant_raw: Any = metadata.get("tenant_id")  # pyright: ignore[reportExplicitAny]
    if tenant_raw is None:
        log.warning("stripe.webhook.missing_tenant_id", event_id=event_id)
        return Response(status_code=200)

    # CR-02: read the Stripe-authoritative amount, not the self-supplied metadata.
    # amount_total is an integer in minor units (cents). Convert to USD Decimal.
    amount_total_raw: Any = getattr(event.data.object, "amount_total", None)  # pyright: ignore[reportExplicitAny]
    if amount_total_raw is None:
        log.warning("stripe.webhook.missing_amount_total", event_id=event_id)
        return Response(status_code=200)
    try:
        amount_usd = Decimal(int(amount_total_raw)) / 100
    except (TypeError, ValueError, InvalidOperation):
        log.warning(
            "stripe.webhook.malformed_amount_total",
            event_id=event_id,
            amount_total_raw=amount_total_raw,
        )
        return Response(status_code=200)

    # Validate tenant_id from metadata as a real UUID before crediting (Security Domain).
    try:
        tenant_id = uuid.UUID(str(tenant_raw))
    except (ValueError, TypeError):
        log.warning(
            "stripe.webhook.bad_tenant_id",
            event_id=event_id,
            tenant_raw=tenant_raw,  # pyright: ignore[reportExplicitAny]
        )
        return Response(status_code=200)

    payment_intent_raw: Any = getattr(  # pyright: ignore[reportExplicitAny]
        event.data.object, "payment_intent", None
    )
    payment_intent = str(payment_intent_raw) if payment_intent_raw is not None else None
    payment_intent = payment_intent or None

    async with sessionmaker() as s, s.begin():
        if payment_intent:
            await pending_clawbacks.lock_payment_intent(s, payment_intent=payment_intent)
        await _expire_pending_clawbacks(s)
        await payment_events.upsert_for_dedup(
            s,
            event_id=event_id,
            amount_usd=amount_usd,
            source="stripe",
            tenant_id=tenant_id,
        )
        claimed = await payment_events.try_claim_credit(s, event_id)
        if claimed:
            # Distinct Stripe event IDs can describe the same payment. Check
            # existing rows keyed by the older event-ID scheme, then use the
            # payment intent as the idempotency key for concurrent new events.
            existing = (
                await tenant_ledger.get_by_payment_intent(s, payment_intent=str(payment_intent))
                if payment_intent
                else None
            )
            inserted = False
            if existing is None:
                inserted = await tenant_ledger.insert_entry(
                    s,
                    tenant_id=tenant_id,
                    delta_usd=amount_usd,
                    reason="topup",
                    idempotency_key=(
                        f"topup:pi:{payment_intent}" if payment_intent else f"topup:{event_id}"
                    ),
                    payment_event_id=event_id,
                    payment_intent=str(payment_intent) if payment_intent else None,
                )
            if not inserted:
                existing = existing or (
                    await tenant_ledger.get_by_payment_intent(s, payment_intent=str(payment_intent))
                    if payment_intent
                    else None
                )
                if (
                    existing is None
                    or existing.tenant_id != tenant_id
                    or existing.delta_usd != amount_usd
                ):
                    raise _PaymentCreditConflict(
                        payment_intent=payment_intent,
                        tenant_id=str(tenant_id),
                        existing_tenant_id=(
                            str(existing.tenant_id) if existing is not None else None
                        ),
                        amount_usd=str(amount_usd),
                        existing_amount_usd=(
                            str(existing.delta_usd) if existing is not None else None
                        ),
                    )
        if payment_intent:
            credit = await tenant_ledger.get_by_payment_intent(
                s, payment_intent=payment_intent, for_update=True
            )
            if credit is not None:
                await _drain_pending_clawbacks(s, payment_intent=payment_intent, credit=credit)

    log.info(
        "stripe.webhook.processed",
        event_id=event_id,
        claimed=claimed,
        amount_usd=str(amount_usd),
        tenant_id=str(tenant_id),
    )
    return Response(status_code=200)


def _clawback_amount_from_event(
    event_type: str,
    charge: dict[str, Any],  # Stripe declares Event.Data.object as Dict[str, Any]
    original_credit: Decimal,
) -> Decimal:
    """Return the actual money to claw back (positive Decimal, in USD).

    CR-03: use the event-reported amount, not the full original credit.
      - charge.refunded  -> charge.amount_refunded (minor units)
      - charge.dispute.created -> dispute.amount (minor units)
    Clamped to original_credit so we never claw back more than was credited.
    """
    target = _clawback_target_from_event(event_type, charge)
    if target is None:
        return original_credit
    return min(target, original_credit)


def _clawback_target_from_event(
    event_type: str,
    charge: dict[str, Any],  # Stripe declares Event.Data.object as Dict[str, Any]
) -> Decimal | None:
    """Return the event's unbounded cumulative target, or None for full credit."""
    if event_type == "charge.refunded":
        raw: Any = getattr(charge, "amount_refunded", None)  # pyright: ignore[reportExplicitAny]
    else:
        # charge.dispute.created: the dispute object has an `amount` field.
        raw = getattr(charge, "amount", None)

    if raw is None:
        return None
    try:
        amount = Decimal(int(raw)) / 100
    except (TypeError, ValueError, InvalidOperation):
        return None
    return max(amount, Decimal("0"))


async def _expire_pending_clawbacks(session: AsyncSession) -> None:
    cutoff = datetime.now(UTC) - _PENDING_CLAWBACK_RETENTION
    expired = await pending_clawbacks.expire_before(session, cutoff=cutoff)
    if expired:
        log.info("stripe.webhook.pending_clawbacks_expired", count=expired)


async def _record_clawback_delta(
    session: AsyncSession,
    *,
    event_id: str,
    event_type: str,
    payment_intent: str,
    tenant_id: uuid.UUID,
    target_amount: Decimal,
    already_clawed_back: Decimal,
) -> Decimal:
    new_delta = target_amount - already_clawed_back
    if new_delta <= 0:
        log.info(
            "stripe.webhook.clawback_noop",
            event_id=event_id,
            event_type=event_type,
            target_clawback=str(target_amount),
            already_clawed_back=str(already_clawed_back),
        )
        return already_clawed_back

    await payment_events.upsert_for_dedup(
        session,
        event_id=event_id,
        amount_usd=new_delta,
        source="stripe",
        tenant_id=tenant_id,
    )
    await payment_events.try_claim_credit(session, event_id)
    inserted = await tenant_ledger.insert_entry(
        session,
        tenant_id=tenant_id,
        delta_usd=-new_delta,
        reason=event_type,
        idempotency_key=f"clawback:{payment_intent}:{event_id}",
        payment_event_id=event_id,
        payment_intent=payment_intent,
    )
    if not inserted:
        raise RuntimeError(f"clawback insert conflict for {event_id!r} after a positive delta")
    return target_amount


async def _drain_pending_clawbacks(
    session: AsyncSession,
    *,
    payment_intent: str,
    credit: tenant_ledger.TenantLedgerRow,
) -> None:
    if credit.payment_event_id is None:
        log.warning(
            "stripe.webhook.pending_clawback_no_payment_event",
            payment_intent=payment_intent,
        )
        return
    original_pe = await payment_events.get(session, credit.payment_event_id)
    if original_pe is None:
        log.warning(
            "stripe.webhook.pending_clawback_no_payment_event",
            payment_intent=payment_intent,
            payment_event_id=credit.payment_event_id,
        )
        return

    rows = await pending_clawbacks.list_for_payment_intent(session, payment_intent=payment_intent)
    already_clawed_back = await tenant_ledger.get_clawed_back_total(
        session, payment_intent=payment_intent
    )
    for row in rows:
        target = (
            credit.delta_usd
            if row.target_amount_usd is None
            else min(row.target_amount_usd, credit.delta_usd)
        )
        already_clawed_back = await _record_clawback_delta(
            session,
            event_id=row.event_id,
            event_type=row.event_type,
            payment_intent=payment_intent,
            tenant_id=credit.tenant_id,
            target_amount=target,
            already_clawed_back=already_clawed_back,
        )
        await pending_clawbacks.remove(session, event_id=row.event_id)


async def _handle_clawback(
    sessionmaker: async_sessionmaker[AsyncSession],
    event_id: str,
    event_type: str,
    event: stripe.Event,
) -> Response:
    """Append a clawback ledger row on charge.refunded / charge.dispute.created.

    resolve tenant + amount from the original credit via get_by_payment_intent.

    Model A (uniform high-water-mark): refunds and disputes share one cumulative
    clawed-back total per payment_intent. The pure _clawback_amount_from_event
    returns target_clawback = min(event_amount, original_credit); this shell reads
    already_clawed_back and writes a row only for new_delta = target - already > 0,
    keyed per-event (clawback:{pi}:{event_id}) so distinct growing events each get a
    durable, replay-safe row. Redundancy is enforced by the new_delta > 0 gate and the
    cumulative get_clawed_back_total, NOT by key collision. charge.refunded's
    amount_refunded is cumulative, so a second growing partial refund is no longer
    dropped (WR-01). A full refund followed by a full dispute yields new_delta == 0,
    so the dispute is a no-op (CR-01, now emergent). new_delta is clamped so we never
    claw back more than the original credit (CR-03).
    """
    charge = event.data.object
    pi_raw: Any = getattr(charge, "payment_intent", None)  # pyright: ignore[reportExplicitAny]
    payment_intent = str(pi_raw) if pi_raw is not None else None
    payment_intent = payment_intent or None

    async with sessionmaker() as s, s.begin():
        if payment_intent:
            await pending_clawbacks.lock_payment_intent(s, payment_intent=payment_intent)
        await _expire_pending_clawbacks(s)
        credit = (
            await tenant_ledger.get_by_payment_intent(
                s, payment_intent=payment_intent, for_update=True
            )
            if payment_intent is not None
            else None
        )
        if credit is None:
            if payment_intent is not None:
                await pending_clawbacks.enqueue(
                    s,
                    event_id=event_id,
                    payment_intent=payment_intent,
                    event_type=event_type,
                    target_amount_usd=_clawback_target_from_event(event_type, charge),
                )
                log.info(
                    "stripe.webhook.clawback_pending",
                    event_id=event_id,
                    payment_intent=payment_intent,
                )
            else:
                log.warning("stripe.webhook.clawback_missing_payment_intent", event_id=event_id)
            return Response(status_code=200)
        assert payment_intent is not None

        # Fetch the original credit's payment_events row to confirm tenant routing
        # before clawing back. credit.payment_event_id is the original event_id set
        # on the topup insert.
        original_pe = (
            await payment_events.get(s, credit.payment_event_id)
            if credit.payment_event_id is not None
            else None
        )
        if original_pe is None:
            # The credit row exists but its source payment_events row is gone.
            # Cannot confirm tenant routing — log and no-op.
            log.warning(
                "stripe.webhook.clawback_no_payment_event",
                event_id=event_id,
                payment_event_id=credit.payment_event_id,
            )
            return Response(status_code=200)

        # Model A: target_clawback is the cumulative high-water-mark this event implies
        # (min(event_amount, original_credit)); new_delta is what is not yet clawed back.
        target_clawback = _clawback_amount_from_event(event_type, charge, credit.delta_usd)
        already_clawed_back = await tenant_ledger.get_clawed_back_total(
            s, payment_intent=payment_intent
        )
        applied_total = await _record_clawback_delta(
            s,
            event_id=event_id,
            event_type=event_type,
            payment_intent=payment_intent,
            tenant_id=credit.tenant_id,
            target_amount=target_clawback,
            already_clawed_back=already_clawed_back,
        )

    log.info(
        "stripe.webhook.clawback_processed",
        event_id=event_id,
        event_type=event_type,
        tenant_id=str(credit.tenant_id),
        amount=str(max(Decimal("0"), applied_total - already_clawed_back)),
    )
    return Response(status_code=200)
