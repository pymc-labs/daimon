"""Discord overload alerts use one dedup key and leave spend caps to their own alert."""

from __future__ import annotations

import uuid

import httpx
import pytest
from anthropic import RateLimitError
from daimon.adapters.discord import bot
from daimon.core.errors import TurnError
from pydantic import SecretStr


def test_overload_alert_uses_shared_key_but_spend_cap_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alerts: list[str] = []

    def record_alert(url: SecretStr | None, *, key: str, message: str) -> None:
        alerts.append(key)

    monkeypatch.setattr(bot, "alert_ops", record_alert)
    tenant_id = uuid.uuid4()
    request = httpx.Request("GET", "https://api.anthropic.com/v1/models")
    ordinary = httpx.Response(
        429,
        json={"error": {"type": "rate_limit_error"}},
        request=request,
    )
    spend_cap = httpx.Response(
        429,
        json={
            "error": {
                "type": "rate_limit_error",
                "details": {"error_code": "enforced_spend_limit_reached"},
            }
        },
        request=request,
    )
    bot.log_anthropic_overload(
        TurnError(kind="upstream", cause=RateLimitError("rate", response=ordinary, body=None)),
        tenant_id=tenant_id,
        path="mention",
        alert_webhook_url=SecretStr("https://discord.com/webhook"),
    )
    bot.log_anthropic_overload(
        RateLimitError("cap", response=spend_cap, body=None),
        tenant_id=tenant_id,
        path="mention",
        alert_webhook_url=SecretStr("https://discord.com/webhook"),
    )
    assert alerts == ["overloaded"]
