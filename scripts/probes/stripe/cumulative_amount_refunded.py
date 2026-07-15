"""Probe: is Stripe's `charge.amount_refunded` cumulative across multiple partial Refunds?

The WR-01 clawback fix assumes a `charge.refunded` event's `amount_refunded` is a
running cumulative total (a $5 refund then a later $3 refund report 500 then 800),
not a per-event delta. This probe characterizes that assumption against the live
Stripe test-mode API rather than trusting docs.

DESTRUCTIVE in test mode: it creates a real test-mode PaymentIntent (confirmed with
`pm_card_visa`) and two partial Refunds on it. It only ever runs against a test key.

Env-gated and operator-run: requires a real test-mode `STRIPE_SECRET_KEY` (`sk_test_...`).
When the key is absent the probe skips loudly and exits 0 — CI never depends on it.
The executor that writes this file cannot run it (no key available); that is expected.

Run:
    STRIPE_SECRET_KEY=sk_test_... uv run python scripts/probes/stripe/cumulative_amount_refunded.py
"""

from __future__ import annotations

import asyncio
import os
import sys

from stripe import StripeClient
from stripe.params._payment_intent_create_params import PaymentIntentCreateParams
from stripe.params._refund_create_params import RefundCreateParams


async def main() -> None:
    key = os.environ.get("STRIPE_SECRET_KEY")
    if not key:
        print("SKIP: STRIPE_SECRET_KEY not set; this probe needs a live sk_test_ key")
        sys.exit(0)

    client = StripeClient(key)

    # Create + confirm a $20 test-mode PaymentIntent with the canonical test card.
    pi_params: PaymentIntentCreateParams = {
        "amount": 2000,  # $20.00 in minor units
        "currency": "usd",
        "payment_method": "pm_card_visa",
        "payment_method_types": ["card"],  # explicit -> no redirect-based methods
        "confirm": True,
    }
    payment_intent = await client.v1.payment_intents.create_async(pi_params)
    print(f"created PaymentIntent {payment_intent.id} status={payment_intent.status}")

    latest_charge = payment_intent.latest_charge
    assert latest_charge is not None, "confirmed PaymentIntent must have a latest_charge"
    charge_id = latest_charge if isinstance(latest_charge, str) else latest_charge.id
    print(f"charge id: {charge_id}")

    # Refund #1: $5. Expect cumulative amount_refunded == 500.
    refund1_params: RefundCreateParams = {"charge": charge_id, "amount": 500}
    await client.v1.refunds.create_async(refund1_params)
    charge_after_1 = await client.v1.charges.retrieve_async(charge_id)
    print(f"after refund #1 ($5): amount_refunded={charge_after_1.amount_refunded}")
    assert charge_after_1.amount_refunded == 500, (
        f"FAIL: after a single $5 refund, amount_refunded must be 500; "
        f"got {charge_after_1.amount_refunded}"
    )

    # Refund #2: $3 more. Expect cumulative amount_refunded == 800 (500 + 300).
    refund2_params: RefundCreateParams = {"charge": charge_id, "amount": 300}
    await client.v1.refunds.create_async(refund2_params)
    charge_after_2 = await client.v1.charges.retrieve_async(charge_id)
    print(f"after refund #2 ($3): amount_refunded={charge_after_2.amount_refunded}")
    assert charge_after_2.amount_refunded == 800, (
        f"FAIL: after a second $3 refund, amount_refunded must be cumulative (800); "
        f"got {charge_after_2.amount_refunded}"
    )

    print("PASS: amount_refunded is cumulative (500 -> 800)")


if __name__ == "__main__":
    asyncio.run(main())
