"""Tests for the Stripe webhook handler — BILL-03 + TOPUP-02."""

from __future__ import annotations

import json
import time
import uuid
from decimal import Decimal
from typing import Any

import httpx
import pytest
import stripe
from daimon.adapters.mcp.server import create_mcp_app
from daimon.core._models import PaymentEvent
from daimon.core.billing import BillingConfig
from daimon.core.config import (
    AnthropicSettings,
    DatabaseSettings,
    McpSettings,
    Settings,
)
from daimon.core.stores import payment_events, tenant_ledger
from daimon.testing.factories import make_tenant
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.applications import Starlette

pytestmark = pytest.mark.asyncio

_WEBHOOK_SECRET = "whsec_test"


def _make_signed_header(payload: str, secret: str, ts: int | None = None) -> str:
    ts = ts or int(time.time())
    signed = f"{ts}.{payload}"
    # WebhookSignature is a public class on the stripe namespace;
    # _compute_signature is the documented test-helper escape per RESEARCH
    # §"Test forge pattern".
    sig = stripe.WebhookSignature._compute_signature(signed, secret)  # pyright: ignore[reportPrivateUsage, reportUnknownMemberType, reportUnknownVariableType]
    scheme = stripe.WebhookSignature.EXPECTED_SCHEME  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    return f"t={ts},{scheme}={sig}"


def _build_billing_config() -> BillingConfig:
    return BillingConfig(
        secret_key=SecretStr("sk_test"),
        webhook_secret=SecretStr(_WEBHOOK_SECRET),
        prices={10: "price_10", 25: "price_25", 50: "price_50", 100: "price_100"},
        success_url="http://test/success",
        cancel_url="http://test/cancel",
    )


def _build_app(sessionmaker: async_sessionmaker[AsyncSession]) -> Starlette:
    return create_mcp_app(
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
            anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
            mcp=McpSettings(jwt_secret=SecretStr("a" * 32), public_url=HttpUrl("https://x/mcp")),
        ),
        sessionmaker=sessionmaker,
        auth=StaticTokenVerifier(tokens={}),
        billing_config=_build_billing_config(),
    )


def _checkout_session_completed_payload(
    event_id: str = "evt_1",
    tenant_id: str | None = None,
    amount_total: int = 1000,  # Stripe minor units (cents); default $10.00
    metadata_amount_usd: str = "10",  # kept for callers that pass it, ignored by handler
) -> dict[str, Any]:
    metadata: dict[str, str] = {}
    if tenant_id is not None:
        metadata["tenant_id"] = tenant_id
    return {
        "id": event_id,
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "payment_intent": f"pi_{event_id}",
                "amount_total": amount_total,
                "metadata": metadata,
            },
        },
    }


def _charge_refunded_payload(
    event_id: str,
    payment_intent: str,
    amount_refunded: int = 1000,  # Stripe minor units; default full refund of $10.00
    amount: int = 1000,  # total charge amount in minor units
) -> dict[str, Any]:
    return {
        "id": event_id,
        "type": "charge.refunded",
        "data": {
            "object": {
                "payment_intent": payment_intent,
                "amount_refunded": amount_refunded,
                "amount": amount,
            },
        },
    }


def _charge_dispute_payload(
    event_id: str,
    payment_intent: str,
    amount: int = 1000,  # disputed amount in minor units; default $10.00
) -> dict[str, Any]:
    return {
        "id": event_id,
        "type": "charge.dispute.created",
        "data": {
            "object": {
                "payment_intent": payment_intent,
                "amount": amount,
            },
        },
    }


async def _post_signed(
    app: Starlette,
    payload_dict: dict[str, Any],
    secret: str = _WEBHOOK_SECRET,
) -> httpx.Response:
    payload = json.dumps(payload_dict)
    header = _make_signed_header(payload, secret=secret)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        return await ac.post(
            "/webhooks/stripe",
            content=payload,
            headers={"stripe-signature": header},
        )


# ---------------------------------------------------------------------------
# Existing signature + routing tests
# ---------------------------------------------------------------------------


async def test_invalid_signature_returns_400(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    app = _build_app(sessionmaker)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/webhooks/stripe",
            content=json.dumps(_checkout_session_completed_payload()),
            headers={"stripe-signature": "t=1,v1=bogus"},
        )
    assert r.status_code == 400, "bad signature must return 400"


async def test_valid_signature_writes_dedup_row_returns_200(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Completed event with a valid tenant_id writes dedup row + claims credit."""
    tenant = await _seed_tenant_via_sessionmaker(sessionmaker)
    payload = json.dumps(_checkout_session_completed_payload("evt_valid", tenant_id=str(tenant)))
    header = _make_signed_header(payload, secret=_WEBHOOK_SECRET)

    app = _build_app(sessionmaker)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/webhooks/stripe",
            content=payload,
            headers={"stripe-signature": header},
        )
    assert r.status_code == 200, "valid signature + checkout.session.completed -> 200"

    async with sessionmaker() as s:
        result = await s.execute(select(PaymentEvent).where(PaymentEvent.id == "evt_valid"))
        row = result.scalar_one()
    assert row.amount_usd == Decimal("10"), "dedup row should record amount_usd from amount_total"
    assert row.credited_at is not None, "try_claim_credit should have stamped credited_at"


async def test_replay_is_idempotent_returns_200_no_new_row(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await _seed_tenant_via_sessionmaker(sessionmaker)
    payload = json.dumps(_checkout_session_completed_payload("evt_replay", tenant_id=str(tenant)))
    header = _make_signed_header(payload, secret=_WEBHOOK_SECRET)

    app = _build_app(sessionmaker)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        r1 = await ac.post(
            "/webhooks/stripe",
            content=payload,
            headers={"stripe-signature": header},
        )
        r2 = await ac.post(
            "/webhooks/stripe",
            content=payload,
            headers={"stripe-signature": header},
        )
    assert r1.status_code == 200, "first delivery returns 200"
    assert r2.status_code == 200, "replay returns 200"

    async with sessionmaker() as s:
        result = await s.execute(
            select(func.count()).select_from(PaymentEvent).where(PaymentEvent.id == "evt_replay")
        )
        count = result.scalar_one()
    assert count == 1, "replay must not create a second row"


async def test_unhandled_event_type_returns_200_noop(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    payload_obj: dict[str, Any] = {
        "id": "evt_other",
        "type": "customer.created",
        "data": {"object": {}},
    }
    payload = json.dumps(payload_obj)
    header = _make_signed_header(payload, secret=_WEBHOOK_SECRET)

    app = _build_app(sessionmaker)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/webhooks/stripe",
            content=payload,
            headers={"stripe-signature": header},
        )
    assert r.status_code == 200, "non-checkout events must ack with 200"

    async with sessionmaker() as s:
        result = await s.execute(select(PaymentEvent).where(PaymentEvent.id == "evt_other"))
        assert result.scalar_one_or_none() is None, "non-checkout events write no DB row"


async def test_missing_metadata_returns_200_logs_warn(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    payload_obj: dict[str, Any] = {
        "id": "evt_no_meta",
        "type": "checkout.session.completed",
        "data": {"object": {"metadata": {}}},  # missing all keys
    }
    payload = json.dumps(payload_obj)
    header = _make_signed_header(payload, secret=_WEBHOOK_SECRET)

    app = _build_app(sessionmaker)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/webhooks/stripe",
            content=payload,
            headers={"stripe-signature": header},
        )
    assert r.status_code == 200, "missing metadata must NOT trigger Stripe retry — 200 ack"

    async with sessionmaker() as s:
        result = await s.execute(select(PaymentEvent).where(PaymentEvent.id == "evt_no_meta"))
        assert result.scalar_one_or_none() is None, "no row when metadata missing"


# ---------------------------------------------------------------------------
# Credit tests (TOPUP-02)
# ---------------------------------------------------------------------------


async def test_credit_completed_event_writes_ledger_row(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Test 1: checkout.session.completed credits one +amount ledger row (reason=topup)."""
    tenant = await _seed_tenant_via_sessionmaker(sessionmaker)
    r = await _post_signed(
        _build_app(sessionmaker),
        _checkout_session_completed_payload("evt_credit_1", tenant_id=str(tenant)),
    )
    assert r.status_code == 200, "completed event must return 200"

    async with sessionmaker() as s:
        balance = await tenant_ledger.get_balance(s, tenant_id=tenant)
    assert balance == Decimal("10"), "ledger balance must equal the credited amount"


async def test_credit_replay_no_double_credit(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Test 2: replaying the same event_id yields exactly one ledger row."""
    tenant = await _seed_tenant_via_sessionmaker(sessionmaker)
    app = _build_app(sessionmaker)
    payload_dict = _checkout_session_completed_payload("evt_replay_credit", tenant_id=str(tenant))

    await _post_signed(app, payload_dict)
    await _post_signed(app, payload_dict)  # replay

    async with sessionmaker() as s:
        balance = await tenant_ledger.get_balance(s, tenant_id=tenant)
    assert balance == Decimal("10"), (
        "replay must not double-credit; balance must equal one topup amount"
    )


async def test_credit_missing_tenant_id_returns_200_noop(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Test 3: completed event with no metadata.tenant_id -> 200 no-op, no ledger row."""
    app = _build_app(sessionmaker)
    r = await _post_signed(
        app,
        _checkout_session_completed_payload("evt_no_tenant"),  # no tenant_id in metadata
    )
    assert r.status_code == 200, "missing tenant_id must return 200 no-op"

    async with sessionmaker() as s:
        pe = await payment_events.get(s, "evt_no_tenant")
    assert pe is None, "no payment_events row must be written when tenant_id missing"


async def test_credit_invalid_tenant_id_returns_200_noop(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Test 4: non-UUID metadata.tenant_id -> 200 no-op, no ledger row."""
    payload_obj = {
        "id": "evt_bad_tenant",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "amount_total": 1000,
                "metadata": {
                    "tenant_id": "not-a-valid-uuid",
                },
            },
        },
    }
    app = _build_app(sessionmaker)
    r = await _post_signed(app, payload_obj)
    assert r.status_code == 200, "invalid tenant_id must return 200 no-op"

    async with sessionmaker() as s:
        pe = await payment_events.get(s, "evt_bad_tenant")
    assert pe is None, "no payment_events row must be written when tenant_id is invalid"


# ---------------------------------------------------------------------------
# Clawback tests (D-17)
# ---------------------------------------------------------------------------


async def test_clawback_charge_refunded_writes_negative_ledger_row(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Test 5: charge.refunded -> negative ledger row (reason=charge.refunded), idempotent."""
    tenant = await _seed_tenant_via_sessionmaker(sessionmaker)
    app = _build_app(sessionmaker)

    # Seed a credit first so get_by_payment_intent can resolve it.
    credit_payload = _checkout_session_completed_payload("evt_original_5", tenant_id=str(tenant))
    await _post_signed(app, credit_payload)

    # Now send the refund event referencing the same payment_intent.
    r = await _post_signed(
        app,
        _charge_refunded_payload("evt_refund_5", payment_intent="pi_evt_original_5"),
    )
    assert r.status_code == 200, "charge.refunded must return 200"

    async with sessionmaker() as s:
        balance = await tenant_ledger.get_balance(s, tenant_id=tenant)
    assert balance == Decimal("0"), "after credit + clawback of equal amount, balance must be zero"


async def test_clawback_charge_dispute_writes_negative_ledger_row(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Test 6: charge.dispute.created -> negative ledger row, idempotent."""
    tenant = await _seed_tenant_via_sessionmaker(sessionmaker)
    app = _build_app(sessionmaker)

    credit_payload = _checkout_session_completed_payload("evt_original_6", tenant_id=str(tenant))
    await _post_signed(app, credit_payload)

    r = await _post_signed(
        app,
        _charge_dispute_payload("evt_dispute_6", payment_intent="pi_evt_original_6"),
    )
    assert r.status_code == 200, "charge.dispute.created must return 200"

    async with sessionmaker() as s:
        balance = await tenant_ledger.get_balance(s, tenant_id=tenant)
    assert balance == Decimal("0"), (
        "after credit + dispute clawback of equal amount, balance must be zero"
    )


async def test_clawback_idempotent_no_double_clawback(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Replaying a clawback event must not write a second negative row."""
    tenant = await _seed_tenant_via_sessionmaker(sessionmaker)
    app = _build_app(sessionmaker)

    credit_payload = _checkout_session_completed_payload("evt_original_8", tenant_id=str(tenant))
    await _post_signed(app, credit_payload)

    refund = _charge_refunded_payload("evt_refund_8", payment_intent="pi_evt_original_8")
    await _post_signed(app, refund)
    await _post_signed(app, refund)  # replay

    async with sessionmaker() as s:
        balance = await tenant_ledger.get_balance(s, tenant_id=tenant)
    assert balance == Decimal("0"), (
        "replayed clawback must be idempotent; balance must be zero (not negative)"
    )


# ---------------------------------------------------------------------------
# CR-01: double-clawback across refund + dispute for the same payment_intent
# ---------------------------------------------------------------------------


async def test_cr01_clawback_idempotent_across_refund_and_dispute(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """CR-01 (BLOCKER): refund + dispute on the same payment_intent must produce
    exactly ONE negative ledger row — not two. Before the fix this writes two rows
    because the CAS key is the inbound event_id, which differs between event types.
    """
    tenant = await _seed_tenant_via_sessionmaker(sessionmaker)
    app = _build_app(sessionmaker)

    # Seed a $10 credit.
    await _post_signed(
        app,
        _checkout_session_completed_payload("evt_cr01_orig", tenant_id=str(tenant)),
    )

    # Deliver a refund event for that payment_intent.
    r1 = await _post_signed(
        app,
        _charge_refunded_payload("evt_cr01_refund", payment_intent="pi_evt_cr01_orig"),
    )
    assert r1.status_code == 200, "charge.refunded must return 200"

    # Deliver a dispute event for the SAME payment_intent (different event id).
    r2 = await _post_signed(
        app,
        _charge_dispute_payload("evt_cr01_dispute", payment_intent="pi_evt_cr01_orig"),
    )
    assert r2.status_code == 200, "charge.dispute.created must return 200"

    async with sessionmaker() as s:
        balance = await tenant_ledger.get_balance(s, tenant_id=tenant)

    # $10 credit - exactly one $10 clawback = $0. If bug present: $10 - $20 = -$10.
    assert balance == Decimal("0"), (
        f"refund + dispute on same payment_intent must produce exactly one reversal; "
        f"balance={balance} (expected 0)"
    )


# ---------------------------------------------------------------------------
# WR-01: a second distinct partial refund (growing cumulative amount_refunded)
# on the same payment_intent must not be dropped
# ---------------------------------------------------------------------------


async def test_wr01_second_partial_refund_not_dropped(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """WR-01: two distinct charge.refunded events for one payment_intent with a
    growing cumulative amount_refunded ($4 then $7 total) on a $10 credit must
    leave balance $3 (10 - 7). Before the fix the second partial refund hit
    ON CONFLICT DO NOTHING on the payment_intent-only key and was silently dropped.
    """
    tenant = await _seed_tenant_via_sessionmaker(sessionmaker)
    app = _build_app(sessionmaker)

    # Seed a $10 credit; payment_intent becomes "pi_evt_wr01_orig".
    await _post_signed(
        app,
        _checkout_session_completed_payload(
            "evt_wr01_orig",
            tenant_id=str(tenant),
            amount_total=1000,
            metadata_amount_usd="10",
        ),
    )

    # Refund #1: $4 cumulative refunded so far.
    r1 = await _post_signed(
        app,
        _charge_refunded_payload(
            "evt_wr01_refund1",
            payment_intent="pi_evt_wr01_orig",
            amount_refunded=400,  # $4.00
            amount=1000,
        ),
    )
    assert r1.status_code == 200, "first partial refund must return 200"

    # Refund #2: distinct event_id, $7 cumulative refunded.
    r2 = await _post_signed(
        app,
        _charge_refunded_payload(
            "evt_wr01_refund2",
            payment_intent="pi_evt_wr01_orig",
            amount_refunded=700,  # $7.00 cumulative
            amount=1000,
        ),
    )
    assert r2.status_code == 200, "second partial refund must return 200"

    async with sessionmaker() as s:
        balance = await tenant_ledger.get_balance(s, tenant_id=tenant)

    # $10 credit - $7 cumulative refund = $3. Second partial refund must not be dropped.
    assert balance == Decimal("3"), (
        f"two growing partial refunds ($4 then $7) on a $10 credit must leave $3; "
        f"the second partial refund must not be dropped; balance={balance}"
    )


# ---------------------------------------------------------------------------
# CR-02: credit must use Stripe-authoritative amount_total, not metadata
# ---------------------------------------------------------------------------


async def test_cr02_credit_uses_amount_total_not_metadata(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """CR-02 (BLOCKER): when metadata amount_usd differs from amount_total, the
    ledger credit must reflect amount_total (Stripe-authoritative), not metadata.
    Before the fix, an attacker can inflate their credit by sending a low amount_usd
    after paying a different amount via Stripe.
    """
    tenant = await _seed_tenant_via_sessionmaker(sessionmaker)
    # amount_total = 2500 cents = $25.00; metadata claims only $10 (or vice versa).
    r = await _post_signed(
        _build_app(sessionmaker),
        _checkout_session_completed_payload(
            "evt_cr02",
            tenant_id=str(tenant),
            amount_total=2500,  # Stripe-authoritative: $25.00
            metadata_amount_usd="10",  # self-supplied lie: claims $10
        ),
    )
    assert r.status_code == 200, "completed event must return 200"

    async with sessionmaker() as s:
        balance = await tenant_ledger.get_balance(s, tenant_id=tenant)

    assert balance == Decimal("25.00"), (
        f"credit must use Stripe-authoritative amount_total ($25.00), "
        f"not metadata amount_usd ($10); balance={balance}"
    )


# ---------------------------------------------------------------------------
# CR-03: partial refund claws back only the refunded amount, not the full credit
# ---------------------------------------------------------------------------


async def test_cr03_partial_refund_claws_back_only_refunded_amount(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """CR-03 (BLOCKER): a $5 partial refund on a $100 credit must debit exactly $5,
    not the full $100. Before the fix, the handler reverses credit.delta_usd regardless
    of the actual amount_refunded on the charge event.
    """
    tenant = await _seed_tenant_via_sessionmaker(sessionmaker)
    app = _build_app(sessionmaker)

    # Seed a $100 credit (amount_total=10000 cents).
    await _post_signed(
        app,
        _checkout_session_completed_payload(
            "evt_cr03_orig",
            tenant_id=str(tenant),
            amount_total=10000,  # $100.00
            metadata_amount_usd="100",
        ),
    )

    # Partial refund: $5 refunded out of $100 total.
    r = await _post_signed(
        app,
        _charge_refunded_payload(
            "evt_cr03_refund",
            payment_intent="pi_evt_cr03_orig",
            amount_refunded=500,  # $5.00
            amount=10000,  # $100.00 total charge
        ),
    )
    assert r.status_code == 200, "partial refund event must return 200"

    async with sessionmaker() as s:
        balance = await tenant_ledger.get_balance(s, tenant_id=tenant)

    # $100 credit - $5 clawback = $95.
    assert balance == Decimal("95.00"), (
        f"partial $5 refund on $100 credit must leave $95 balance; balance={balance}"
    )


async def test_cr03_dispute_claws_back_dispute_amount(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """CR-03 (BLOCKER): dispute amount drives the clawback, not the full credit amount."""
    tenant = await _seed_tenant_via_sessionmaker(sessionmaker)
    app = _build_app(sessionmaker)

    # Seed a $100 credit.
    await _post_signed(
        app,
        _checkout_session_completed_payload(
            "evt_cr03_disp_orig",
            tenant_id=str(tenant),
            amount_total=10000,
            metadata_amount_usd="100",
        ),
    )

    # Dispute for only $25.
    r = await _post_signed(
        app,
        _charge_dispute_payload(
            "evt_cr03_dispute",
            payment_intent="pi_evt_cr03_disp_orig",
            amount=2500,  # $25.00 disputed
        ),
    )
    assert r.status_code == 200, "dispute event must return 200"

    async with sessionmaker() as s:
        balance = await tenant_ledger.get_balance(s, tenant_id=tenant)

    # $100 - $25 = $75.
    assert balance == Decimal("75.00"), (
        f"$25 dispute on $100 credit must leave $75 balance; balance={balance}"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_tenant_via_sessionmaker(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> uuid.UUID:
    """Seed a Tenant row and return its id.

    Uses a direct session (not the shared db_session) so the tenant is visible
    to the webhook handler's own session (same ephemeral schema, same engine).
    """
    async with sessionmaker() as s, s.begin():
        tenant = await make_tenant(s)
    return tenant.id
