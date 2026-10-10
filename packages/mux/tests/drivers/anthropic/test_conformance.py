"""A6 real-SDK C01–C18 registration; partial checks never become passes."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport
from mux.conformance import Registry, run
from mux.conformance.runner import Adapter, PendingKind, run_fixture
from mux.contracts.config import CapabilityRequirement, ConfigRevision, ResolvedBackend
from mux.contracts.ids import ResourceRef, Scope
from mux.contracts.resources import Session
from mux.drivers.anthropic import AnthropicManagedAgents
from mux.drivers.anthropic.sessions_lifecycle import AnthropicSessions
from mux.errors import ScopeViolation, UnsupportedCapability

from .conformance_adapter import (
    BETA_QUERY,
    EVENTS_PATH,
    PENDING,
    REF,
    SCOPE,
    SCRIPTED_FIXTURES,
    SESSION_PATH,
    AnthropicOfflineFactory,
    AnthropicScript,
    register,
    session_reply,
)


@pytest.fixture
async def adapters() -> AsyncIterator[AnthropicOfflineFactory]:
    async with AnthropicOfflineFactory() as factory:
        yield factory


def script(adapter: Adapter) -> AnthropicScript:
    assert isinstance(adapter.transport, AnthropicScript)
    return adapter.transport


async def test_matrix_runs_all_eighteen_with_exact_passes_and_typed_gaps() -> None:
    registry = Registry()
    async with register(registry) as factory:
        assert registry.names == ("anthropic.offline",)
        results = await run(registry, "anthropic.offline")
        assert [r.fixture_id for r in results] == [f"C{i:02}" for i in range(1, 19)]
        assert {r.fixture_id for r in results if r.status == "pass"} == SCRIPTED_FIXTURES
        assert {r.fixture_id for r in results if r.status == "pending"} == set(PENDING)
        assert len(factory.scripts) == 18
        assert all(r.status in ("pass", "pending") and r.evidence for r in results)
        for result, native in zip(results, factory.scripts, strict=True):
            assert type(native.driver) is AnthropicManagedAgents
            assert isinstance(native.wire, ScriptedTransport)
            native.wire.assert_consumed()
            if result.fixture_id in PENDING:
                assert result.pending_reason == PENDING[result.fixture_id]
                assert native.wire.requests == []
            else:
                assert result.pending_reason is None
                assert native.wire.requests
    assert all(native.sdk.is_closed() for native in factory.scripts)


@pytest.mark.parametrize("fixture", sorted(SCRIPTED_FIXTURES))
async def test_shared_probe_uses_exact_native_sdk_requests(
    adapters: AnthropicOfflineFactory, fixture: str
) -> None:
    adapter = adapters()
    result = await run_fixture(fixture, adapter)
    assert result.status == "pass", result.evidence
    native = script(adapter)
    expected = [SESSION_PATH] * 3 if fixture == "C15" else [SESSION_PATH, EVENTS_PATH, EVENTS_PATH]
    assert [(r.method, r.path, r.query, r.body) for r in native.wire.requests] == [
        ("GET", path, BETA_QUERY, b"") for path in expected
    ]
    for request in native.wire.requests:
        assert dict(request.protocol_headers)["anthropic-beta"] == "managed-agents-2026-04-01"
        assert dict(request.protocol_headers)["anthropic-version"] == "2023-06-01"
    assert native.mutation_count == 0 and native.deleted_resources == ()
    native.wire.assert_consumed()


@pytest.mark.parametrize("fixture", sorted(PENDING))
async def test_pending_is_typed_and_precedes_http(
    adapters: AnthropicOfflineFactory, fixture: str
) -> None:
    adapter = adapters()
    result = await run_fixture(fixture, adapter)
    assert result.status == "pending" and result.pending_reason == PENDING[fixture]
    assert PENDING[fixture].detail.strip()
    expected = (
        PendingKind.CAPABILITY_UNAVAILABLE if fixture == "C09" else PendingKind.ADAPTER_DEPENDENCY
    )
    assert PENDING[fixture].kind == expected
    assert script(adapter).wire.requests == []


async def test_registration_is_explicit_fresh_and_closes_after_failure() -> None:
    registry = Registry()
    factory = register(registry)
    with pytest.raises(RuntimeError, match="fixture error"):
        async with factory:
            first, second = (
                registry.create("anthropic.offline"),
                registry.create("anthropic.offline"),
            )
            assert first.driver is not second.driver
            assert first.transport is not second.transport
            assert script(first).sdk.max_retries == script(second).sdk.max_retries == 0
            await run_fixture("C15", first)
            assert script(second).wire.requests == []
            raise RuntimeError("fixture error")
    assert all(native.sdk.is_closed() for native in factory.scripts)


async def test_changed_migration_mutant_fails_shared_probe(
    adapters: AnthropicOfflineFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def migrate(
        self: AnthropicSessions,
        scope: Scope,
        ref: ResourceRef,
        target: ConfigRevision,
        *,
        expected: int,
        key: str,
    ) -> Session:
        return await self.retrieve(scope, ref)

    monkeypatch.setattr(AnthropicSessions, "migrate", migrate)
    result = await run_fixture("C15", adapters())
    assert result.status == "fail" and result.pending_reason is None
    assert result.evidence == ("check failed: migration unexpectedly supported",)


async def test_raw_sdk_leak_mutant_fails_shared_probe(
    adapters: AnthropicOfflineFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = adapters()
    monkeypatch.setattr(adapter.driver, "client", script(adapter).sdk, raising=False)
    result = await run_fixture("C16", adapter)
    assert result.status == "fail" and result.pending_reason is None
    assert result.evidence == ("check failed: C16: raw provider handle is public",)


async def test_undeclared_gap_is_fail_and_safe_not_a_silent_pending(
    adapters: AnthropicOfflineFactory,
) -> None:
    original = adapters()
    adapter = Adapter(
        original.driver,
        original.store,
        original.transport,
        pending={id_: reason for id_, reason in PENDING.items() if id_ != "C11"},
    )
    result = await run_fixture("C11", adapter)
    assert result.status == "fail" and result.pending_reason is None
    assert result.evidence == ("probe raised ValueError",)


async def test_unexpected_http_cannot_be_swallowed_as_a_pass(
    adapters: AnthropicOfflineFactory,
) -> None:
    adapter = adapters()
    native = script(adapter)
    # The SDK has zero retries. Fail at the HTTP boundary, without replacing
    # the driver or teaching the adapter to return a verdict.
    native.wire.queue(
        ScriptedReply(
            "GET",
            SESSION_PATH,
            httpx.Response(
                500, json={"error": {"type": "api_error", "message": "fictional-sensitive-detail"}}
            ),
        )
    )
    result = await run_fixture("C15", adapter)
    assert result.status == "fail" and result.pending_reason is None
    assert result.evidence == ("probe raised ProviderError",)
    assert len(native.wire.requests) == 1


async def test_c10_pending_does_not_falsify_the_actual_native_profile(
    adapters: AnthropicOfflineFactory,
) -> None:
    adapter = adapters()
    profile = adapter.driver.capabilities()
    assert profile.support_for("memory_stores") == "native"
    native = script(adapter)
    native.wire.queue(session_reply())
    session = await adapter.driver.sessions.retrieve(SCOPE, REF)
    config = ConfigRevision.create(
        session.binding.thread.channel,
        1,
        ResolvedBackend(
            backend=profile.provider,
            profile=profile.profile_id,
            model="claude-haiku-4-5-20251001",
            requires={"memory_stores": CapabilityRequirement(level="required")},
        ),
    )
    assert "memory_stores" in adapter.driver.admit(config).satisfied
    with pytest.raises(ScopeViolation):
        await adapter.driver.sessions.retrieve(
            SCOPE.model_copy(update={"tenant_id": "foreign"}), REF
        )
    assert len(native.wire.requests) == 1 and native.mutation_count == 0
    native.wire.assert_consumed()


async def test_c05_reconciliation_and_c09_hard_delete_really_are_unavailable(
    adapters: AnthropicOfflineFactory,
) -> None:
    adapter = adapters()
    with pytest.raises(UnsupportedCapability, match="reconcile"):
        await adapter.driver.events.reconcile(SCOPE, REF)
    with pytest.raises(UnsupportedCapability, match="session_delete"):
        await adapter.driver.sessions.delete(SCOPE, REF, key="delete")
    assert script(adapter).wire.requests == []


async def test_c06_native_cancel_eof_then_independent_stop_is_only_partial_evidence(
    adapters: AnthropicOfflineFactory,
) -> None:
    adapter = adapters()
    native = script(adapter)
    native.wire.queue(
        session_reply(running=True),
        ScriptedReply(
            "POST",
            EVENTS_PATH,
            httpx.Response(200, json={"data": None}),
            query=BETA_QUERY,
            request_json={"events": [{"type": "user.interrupt"}]},
            check_json=True,
        ),
        ScriptedReply.stream(EVENTS_PATH + "/stream", []),
        ScriptedReply.stream(
            EVENTS_PATH + "/stream",
            [
                {
                    "id": "stop",
                    "type": "session.status_idle",
                    "session_id": REF.id,
                    "created_at": "2026-10-10T00:00:00Z",
                    "stop_reason": {"type": "end_turn"},
                }
            ],
        ),
    )
    session = await adapter.driver.sessions.retrieve(SCOPE, REF)
    assert session.state == "running" and session.active_root_turn is None
    receipt = await adapter.driver.events.cancel(SCOPE, REF, turn_id="root", key="cancel")
    assert receipt.status == "requested"
    deadline = datetime.now(UTC) + timedelta(seconds=5)
    first = await adapter.driver.events.wait_stopped(SCOPE, receipt, deadline=deadline)
    assert not first.stopped and first.outcome is None
    second = await adapter.driver.events.wait_stopped(SCOPE, receipt, deadline=deadline)
    assert second.stopped and second.outcome == "interrupted"
    assert native.mutation_count == 1
    post = native.wire.requests[1]
    assert post.body == b'{"events":[{"type":"user.interrupt"}]}'
    native.wire.assert_consumed()
    # Available native pieces do not meet the whole C06 root-occupancy probe.
    assert (await run_fixture("C06", adapter)).status == "pending"
