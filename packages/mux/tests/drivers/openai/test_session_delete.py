"""Cleanup races through the actual pinned SDK; no provider calls."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from mux.drivers.openai import OpenAIDriver, sessions
from mux.drivers.openai.transport import SDKTransport
from mux.drivers.openai.turn import MemoryRecoveryJournal
from mux.drivers.openai.usage import MemoryUsageRevisions
from mux.errors import ProviderError, ScopeViolation
from openai import AsyncOpenAI

from .conftest import REF, SCOPE, native_session


@pytest.mark.asyncio
@pytest.mark.parametrize("initial", ["idle", "in_progress", "requires_action"])
async def test_delete_waits_for_idle_and_recovers_a_definite_conflict(
    initial: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[httpx.Request] = []
    states = iter([initial, "idle", "in_progress", "failed"])
    reads, deletes = 0, 0
    current = ""
    monkeypatch.setattr(sessions, "_DELETE_POLL_INTERVAL", 0.001)

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal reads, deletes, current
        seen.append(request)
        assert request.url.path == "/v1/agents/sessions/s"
        if request.method == "GET":
            reads += 1
            current = next(states)
            raw = native_session(current)
            raw["vault_ids"] = ["shared-vault"]
            return httpx.Response(200, json=raw)
        assert request.method == "DELETE" and current in ("idle", "failed")
        deletes += 1
        if deletes == 1:
            return httpx.Response(409, json={"error": {"message": "sensitive"}})
        return httpx.Response(200, json={"id": "s", "deleted": True})

    async with AsyncOpenAI(
        api_key="offline-placeholder",
        max_retries=5,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    ) as sdk:
        driver = OpenAIDriver(
            SDKTransport(sdk),
            account_scope_id="project",
            journal=MemoryRecoveryJournal(),
            usage_revisions=MemoryUsageRevisions(),
            authorization=lambda s, k, i: s == SCOPE,
        )
        receipt = await driver.sessions.delete(SCOPE, REF, key="cleanup-key")
    assert receipt.deleted == (REF,) and receipt.operation_id == "cleanup-key"
    assert tuple(r.id for r in receipt.retained) == ("shared-vault",)
    assert deletes == 2
    assert reads == (2 if initial == "idle" else 4)
    assert all(r.headers["Idempotency-Key"] == "cleanup-key" for r in seen if r.method == "DELETE")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "busy",
        "conflict",
        "hung_read",
        "hung_delete",
        "foreign",
        "wrong_id",
        "malformed",
        "permission",
        "network",
    ],
)
async def test_cleanup_is_bounded_and_never_replays_uncertain_or_unsafe_deletes(
    case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads, deletes = 0, 0
    monkeypatch.setattr(sessions, "_DELETE_TIMEOUT", 1.0)
    monkeypatch.setattr(sessions, "_DELETE_POLL_INTERVAL", 0.6)

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal reads, deletes
        if request.method == "GET":
            reads += 1
            if case == "hung_read":
                await asyncio.Event().wait()
            raw = native_session("in_progress" if case == "busy" else "idle")
            if reads > 1 and case == "foreign":
                raw["metadata"] = {"mux_tenant": "foreign"}
            if reads > 1 and case == "wrong_id":
                raw["id"] = "another"
            if case == "malformed":
                raw["status"] = "unknown"
            return httpx.Response(200, json=raw)
        assert request.method == "DELETE"
        deletes += 1
        if case == "hung_delete":
            await asyncio.Event().wait()
        if case == "network":
            raise httpx.ReadError("sensitive", request=request)
        return httpx.Response(
            403 if case == "permission" else 409, json={"error": {"message": "sensitive"}}
        )

    async with AsyncOpenAI(
        api_key="offline-placeholder",
        max_retries=5,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    ) as sdk:
        driver = OpenAIDriver(
            SDKTransport(sdk),
            account_scope_id="project",
            journal=MemoryRecoveryJournal(),
            usage_revisions=MemoryUsageRevisions(),
            authorization=lambda s, k, i: s == SCOPE,
        )
        with pytest.raises(ScopeViolation if case == "foreign" else ProviderError) as exc:
            async with asyncio.timeout(3):
                await driver.sessions.delete(SCOPE, REF, key="cleanup-key")
    error = exc.value
    assert "sensitive" not in str(error)
    assert deletes <= 2
    if case in ("busy", "hung_read", "malformed"):
        assert deletes == 0
    elif case != "conflict":
        assert deletes == 1
    if case in ("busy", "conflict", "hung_read"):
        assert isinstance(error, ProviderError)
        assert error.category == "conflict" and error.native_code == "session_delete_deadline"
    if case == "hung_delete":
        assert isinstance(error, ProviderError)
        assert error.native_code == "session_delete_outcome_unknown" and not error.retryable
    if case == "network":
        assert isinstance(error, ProviderError) and error.category == "transient_network"
    if case == "permission":
        assert isinstance(error, ProviderError) and error.category == "permission"
