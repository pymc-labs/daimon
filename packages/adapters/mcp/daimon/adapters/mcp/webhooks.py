"""Webhook handlers — INFRA-01 surface, Phase 20 lands the Stripe handler.

Phase 97 removed the GitHub OAuth web flow entirely (D-03/D-10); the GitHub
webhook here is skill-sync's push-driven resync trigger only, decoupled from
App-clone credential resolution (D-07).

Per `guideline:architecture` "no module-level singletons": Stripe billing
config is INJECTED into the factory by `create_mcp_app`. The factory returns
a closure with the config bound. Boot fails fast on misconfig (Pitfall 5).
"""

from __future__ import annotations

import contextlib
import json
import uuid
from collections.abc import Awaitable, Callable
from decimal import Decimal, InvalidOperation
from typing import Any

import structlog
from anthropic import AsyncAnthropic
from cryptography.fernet import MultiFernet
from daimon.core.billing import BillingConfig
from daimon.core.config import GithubSettings
from daimon.core.errors import StoreError
from daimon.core.github_app_auth import verify_signature
from daimon.core.skill_sync.resync import resync_bound_repo
from daimon.core.stores import github_app_installations as install_store
from daimon.core.stores import payment_events, tenant_ledger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import Response

log = structlog.get_logger(__name__)


def _get(d: dict[str, Any], key: str) -> Any:  # pyright: ignore[reportExplicitAny]
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
    anthropic: AsyncAnthropic,
    fernet: MultiFernet,
) -> Callable[[Request], Awaitable[Response]]:
    """Construct a GitHub App webhook handler with collaborators bound. SC-3.

    Handler flow:
      1. Read raw body bytes (Pitfall 4 — never re-serialize via json.dumps).
      2. Verify HMAC-SHA256 signature (X-Hub-Signature-256) BEFORE parsing JSON.
         Forged/unsigned -> 401 immediately (SC-3).
      3. Dispatch on X-GitHub-Event header:
         - push: extract repository.full_name + ref; missing -> 200 no-op + log.warning.
           Otherwise: Response(200, background=BackgroundTask(resync_bound_repo, ...)).
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

        # --- push: schedule resync as a BackgroundTask ---
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
            full_name: str = str(full_name_val) if isinstance(full_name_val, str) else ""
            ref: str = ref_raw
            if not full_name:
                log.warning(
                    "github.webhook.malformed_push",
                    delivery_id=delivery_id,
                    missing="repository.full_name",
                )
                return Response(status_code=200)

            log.info(
                "github.webhook.push_received",
                delivery_id=delivery_id,
                repo=full_name,
                ref=ref,
            )
            return Response(
                status_code=200,
                background=BackgroundTask(
                    resync_bound_repo,
                    repo_full_name=full_name,
                    ref=ref,
                    sessionmaker=sessionmaker,
                    fernet=fernet,
                    anthropic_client=anthropic,
                    github_settings=github_settings,
                    # http_client=None: resync_bound_repo creates its own
                ),
            )

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
    """Handle installation created / deleted events."""
    action = _get(payload, "action")
    install_info = _get(payload, "installation")
    if not isinstance(install_info, dict):
        log.warning("github.webhook.malformed_installation", delivery_id=delivery_id)
        return Response(status_code=200)

    installation_id_raw = _get(install_info, "id")  # pyright: ignore[reportUnknownArgumentType]
    account_info = _get(install_info, "account")  # pyright: ignore[reportUnknownArgumentType]
    if isinstance(account_info, dict):
        account_login_val = _get(account_info, "login")  # pyright: ignore[reportUnknownArgumentType]
        account_login: str = str(account_login_val) if isinstance(account_login_val, str) else ""
    else:
        account_login = ""

    repos_raw = _get(payload, "repositories")
    repos_list: list[Any] = list(repos_raw) if isinstance(repos_raw, list) else []  # pyright: ignore[reportExplicitAny,reportUnknownArgumentType]
    repo_names: list[str] = [
        str(r.get("full_name", ""))  # pyright: ignore[reportUnknownMemberType,reportUnknownArgumentType]
        for r in repos_list
        if isinstance(r, dict) and r.get("full_name")  # pyright: ignore[reportUnknownMemberType]
    ]

    if not isinstance(installation_id_raw, int):
        log.warning(
            "github.webhook.malformed_installation_id",
            delivery_id=delivery_id,
        )
        return Response(status_code=200)
    installation_id: int = installation_id_raw

    if action == "deleted":
        async with sessionmaker.begin() as session:
            with contextlib.suppress(StoreError):  # no-op when installation not found (idempotent)
                await install_store.delete_installation(session, installation_id=installation_id)
        log.info(
            "github.webhook.installation_deleted",
            delivery_id=delivery_id,
            installation_id=installation_id,
        )
    else:
        async with sessionmaker.begin() as session:
            await install_store.upsert(
                session,
                installation_id=installation_id,
                account_login=account_login,
                repo_full_names=repo_names,
            )
        log.info(
            "github.webhook.installation_upserted",
            delivery_id=delivery_id,
            installation_id=installation_id,
            repo_count=len(repo_names),
        )

    return Response(status_code=200)


async def _handle_installation_repositories(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    payload: dict[str, Any],
    delivery_id: str,
) -> Response:
    """Handle installation_repositories added / removed events."""
    install_info = _get(payload, "installation")
    if not isinstance(install_info, dict):
        log.warning("github.webhook.malformed_installation_repositories", delivery_id=delivery_id)
        return Response(status_code=200)

    installation_id_raw = _get(install_info, "id")  # pyright: ignore[reportUnknownArgumentType]
    if not isinstance(installation_id_raw, int):
        log.warning(
            "github.webhook.malformed_installation_id",
            delivery_id=delivery_id,
        )
        return Response(status_code=200)
    installation_id: int = installation_id_raw

    added_raw = _get(payload, "repositories_added")
    removed_raw = _get(payload, "repositories_removed")
    added_list: list[Any] = list(added_raw) if isinstance(added_raw, list) else []  # pyright: ignore[reportExplicitAny,reportUnknownArgumentType]
    removed_list: list[Any] = list(removed_raw) if isinstance(removed_raw, list) else []  # pyright: ignore[reportExplicitAny,reportUnknownArgumentType]
    added: list[str] = [
        str(r.get("full_name", ""))  # pyright: ignore[reportUnknownMemberType,reportUnknownArgumentType]
        for r in added_list
        if isinstance(r, dict) and r.get("full_name")  # pyright: ignore[reportUnknownMemberType]
    ]
    removed: list[str] = [
        str(r.get("full_name", ""))  # pyright: ignore[reportUnknownMemberType,reportUnknownArgumentType]
        for r in removed_list
        if isinstance(r, dict) and r.get("full_name")  # pyright: ignore[reportUnknownMemberType]
    ]

    async with sessionmaker.begin() as session:
        if added:
            with contextlib.suppress(StoreError):  # no-op if installation row not found
                await install_store.add_repos(session, installation_id=installation_id, repos=added)
        if removed:
            with contextlib.suppress(StoreError):  # no-op if installation row not found
                await install_store.remove_repos(
                    session, installation_id=installation_id, repos=removed
                )

    log.info(
        "github.webhook.installation_repositories_updated",
        delivery_id=delivery_id,
        installation_id=installation_id,
        added_count=len(added),
        removed_count=len(removed),
    )
    return Response(status_code=200)


def build_stripe_webhook(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    billing_config: BillingConfig,
) -> Callable[[Request], Awaitable[Response]]:
    """Construct a Stripe webhook handler with sessionmaker + config bound. D-22.

    Handler flow:
      1. Read raw body bytes (Pitfall 4 — never re-serialize via json.dumps)
      2. Verify signature via stripe.Webhook.construct_event; on fail -> 400
      3. Dispatch on event.type:
         - checkout.session.completed  -> credit (CAS-gated, D-16)
         - charge.refunded             -> clawback (idempotent negative row, D-17)
         - charge.dispute.created      -> clawback (idempotent negative row, D-17)
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

        # --- completed checkout: credit the tenant ledger (D-16) ---
        if event_type == "checkout.session.completed":
            return await _handle_completed(sessionmaker, event_id, event)

        # --- refund / dispute: clawback (D-17) ---
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
    event: Any,  # pyright: ignore[reportExplicitAny]
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

    payment_intent: str | None = getattr(event.data.object, "payment_intent", None)

    async with sessionmaker() as s, s.begin():
        await payment_events.upsert_for_dedup(
            s,
            event_id=event_id,
            amount_usd=amount_usd,
            source="stripe",
            tenant_id=tenant_id,
        )
        claimed = await payment_events.try_claim_credit(s, event_id)
        if claimed:
            # WR-03: act on the bool return; raise if insert unexpectedly no-ops after
            # winning the CAS — that would mean a credit was silently lost.
            inserted = await tenant_ledger.insert_entry(
                s,
                tenant_id=tenant_id,
                delta_usd=amount_usd,
                reason="topup",
                idempotency_key=f"topup:{event_id}",
                payment_event_id=event_id,
                payment_intent=str(payment_intent) if payment_intent else None,
            )
            if not inserted:
                raise RuntimeError(
                    f"try_claim_credit won CAS for {event_id!r} but insert_entry returned False "
                    "(idempotency_key conflict after a claimed credit — credit silently lost)"
                )

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
    charge: Any,  # pyright: ignore[reportExplicitAny]
    original_credit: Decimal,
) -> Decimal:
    """Return the actual money to claw back (positive Decimal, in USD).

    CR-03: use the event-reported amount, not the full original credit.
      - charge.refunded  -> charge.amount_refunded (minor units)
      - charge.dispute.created -> dispute.amount (minor units)
    Clamped to original_credit so we never claw back more than was credited.
    """
    if event_type == "charge.refunded":
        raw: Any = getattr(charge, "amount_refunded", None)  # pyright: ignore[reportExplicitAny]
    else:
        # charge.dispute.created: the dispute object has an `amount` field.
        raw = getattr(charge, "amount", None)

    if raw is None:
        # Fallback to full credit if the field is absent (defensive).
        return original_credit
    try:
        amount = Decimal(int(raw)) / 100
    except (TypeError, ValueError, InvalidOperation):
        return original_credit
    # Never claw back more than the original credit.
    return min(amount, original_credit)


async def _handle_clawback(
    sessionmaker: async_sessionmaker[AsyncSession],
    event_id: str,
    event_type: str,
    event: Any,  # pyright: ignore[reportExplicitAny]
) -> Response:
    """Append a clawback ledger row on charge.refunded / charge.dispute.created.

    D-17: resolve tenant + amount from the original credit via get_by_payment_intent.

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
    pi: Any = getattr(charge, "payment_intent", None)  # pyright: ignore[reportExplicitAny]

    async with sessionmaker() as s, s.begin():
        credit = (
            await tenant_ledger.get_by_payment_intent(s, payment_intent=str(pi))
            if pi is not None
            else None
        )
        if credit is None:
            log.warning(
                "stripe.webhook.clawback_no_credit",
                event_id=event_id,
                payment_intent=str(pi) if pi is not None else None,
            )
            return Response(status_code=200)

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
        already_clawed_back = await tenant_ledger.get_clawed_back_total(s, payment_intent=str(pi))
        new_delta = target_clawback - already_clawed_back

        if new_delta <= 0:
            # Redundant event (e.g. dispute after a full refund, or a replayed/older
            # cumulative total). Clean no-op before writing any dedup/clawback row.
            log.info(
                "stripe.webhook.clawback_noop",
                event_id=event_id,
                event_type=event_type,
                target_clawback=str(target_clawback),
                already_clawed_back=str(already_clawed_back),
            )
            return Response(status_code=200)

        await payment_events.upsert_for_dedup(
            s,
            event_id=event_id,
            amount_usd=new_delta,
            source="stripe",
            tenant_id=credit.tenant_id,
        )
        await payment_events.try_claim_credit(s, event_id)

        # Per-event idempotency key so distinct growing events each get a durable row;
        # clawback rows carry payment_intent so get_clawed_back_total sums them.
        await tenant_ledger.insert_entry(
            s,
            tenant_id=credit.tenant_id,
            delta_usd=-new_delta,
            reason=event_type,
            idempotency_key=f"clawback:{str(pi)}:{event_id}",
            payment_event_id=event_id,
            payment_intent=str(pi),
        )

    log.info(
        "stripe.webhook.clawback_processed",
        event_id=event_id,
        event_type=event_type,
        tenant_id=str(credit.tenant_id),
        amount=str(new_delta),
    )
    return Response(status_code=200)
