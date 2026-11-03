"""Billing config loading from the environment."""

from __future__ import annotations

import pytest
from daimon.core import billing


def test_load_billing_config_returns_none_when_partial_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Partial env (6 of 7 vars) returns None instead of raising."""
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_xxx")
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("STRIPE_PRICE_10_USD", "price_10")
    monkeypatch.setenv("STRIPE_PRICE_25_USD", "price_25")
    monkeypatch.setenv("STRIPE_PRICE_50_USD", "price_50")
    monkeypatch.setenv("STRIPE_PRICE_100_USD", "price_100")
    monkeypatch.setenv("MCP_PUBLIC_URL", "https://example.test")

    assert billing.load_billing_config() is None, "partial env should return None"


def test_load_billing_config_returns_none_when_no_stripe_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All Stripe vars absent returns None."""
    for key in (
        "STRIPE_SECRET_KEY",
        "STRIPE_WEBHOOK_SECRET",
        "STRIPE_PRICE_10_USD",
        "STRIPE_PRICE_25_USD",
        "STRIPE_PRICE_50_USD",
        "STRIPE_PRICE_100_USD",
        "MCP_PUBLIC_URL",
    ):
        monkeypatch.delenv(key, raising=False)
    assert billing.load_billing_config() is None, "no STRIPE_* env should return None"


def test_load_billing_config_returns_config_when_env_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_xxx")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_xxx")
    monkeypatch.setenv("STRIPE_PRICE_10_USD", "price_10")
    monkeypatch.setenv("STRIPE_PRICE_25_USD", "price_25")
    monkeypatch.setenv("STRIPE_PRICE_50_USD", "price_50")
    monkeypatch.setenv("STRIPE_PRICE_100_USD", "price_100")
    monkeypatch.setenv("MCP_PUBLIC_URL", "https://example.test")

    cfg = billing.load_billing_config()
    assert cfg.secret_key.get_secret_value() == "sk_test_xxx"
    assert cfg.webhook_secret.get_secret_value() == "whsec_xxx"
    assert cfg.prices == {
        10: "price_10",
        25: "price_25",
        50: "price_50",
        100: "price_100",
    }
    assert cfg.success_url == "https://example.test/billing/success"
    assert cfg.cancel_url == "https://example.test/billing/cancel"
