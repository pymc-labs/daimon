"""Operator alerts stay optional and cannot interrupt application work."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from daimon.core import ops_alerts
from pydantic import SecretStr


async def test_ops_alerts_are_off_without_a_webhook(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[str] = []

    async def fake_post(url: str, message: str) -> None:
        sent.append(message)

    monkeypatch.setattr(ops_alerts, "_post", fake_post)
    ops_alerts.alert_ops(None, key="off", message="do not send")
    await asyncio.sleep(0)
    assert sent == []


async def test_ops_alerts_deduplicate_and_disable_mentions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ops_alerts, "_last_sent", {})
    bodies: list[dict[str, object]] = []
    original_client = httpx.AsyncClient

    def respond(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(204)

    def client(**kwargs: object) -> httpx.AsyncClient:
        return original_client(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(ops_alerts.httpx, "AsyncClient", client)
    url = SecretStr("https://discord.com/api/webhooks/test")
    ops_alerts.alert_ops(url, key="overloaded", message="Alert @everyone")
    ops_alerts.alert_ops(url, key="overloaded", message="duplicate")
    await asyncio.sleep(0.05)
    assert bodies == [{"content": "Alert @everyone", "allowed_mentions": {"parse": []}}]


async def test_webhook_failure_never_reaches_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ops_alerts, "_last_sent", {})
    original_client = httpx.AsyncClient

    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unavailable", request=request)

    def client(**kwargs: object) -> httpx.AsyncClient:
        return original_client(transport=httpx.MockTransport(fail), **kwargs)

    monkeypatch.setattr(ops_alerts.httpx, "AsyncClient", client)
    ops_alerts.alert_ops(
        SecretStr("https://discord.com/api/webhooks/test"), key="fail", message="x"
    )
    await asyncio.sleep(0.05)
