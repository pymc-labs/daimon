"""Actual OpenAI SDK -> host -> committed C18 snapshots, with failure controls."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from daimon.core._models import TurnOutcome
from daimon.core.stores.turn_outcomes import OutcomeRecord
from daimon.core.turn import outcomes
from daimon.core.turn.prepare import bind_session
from daimon.core.turn.run import RunOutcome, run_prepared_turn
from daimon.core.turn.termination import TerminationReason
from daimon.testing.conformance.runner import Registry, run_fixture
from daimon.testing.db import preflight_test_database
from daimon.testing.drivers.openai.conformance import register
from daimon.testing.isolated_sdk import SDK_ENV_VARS, build_async_openai
from daimon.testing.openai_conformance import OpenAIHostTransport, compose_openai_host
from daimon.testing.openai_termination import (
    REQUIRED_REASONS,
    UNREACHABLE_REASONS,
    OpenAITerminationEnvelope,
)
from daimon.testing.turn_conformance import TerminationCase, TerminationSnapshot
from daimon.testing.turn_fakes import RecordingLifecycle
from mux.errors import ScopeViolation
from sqlalchemy import event as sql_event
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from . import test_openai_conformance as native_fixture
from .test_openai_conformance import Host, NativeStream

native_host = native_fixture.native_host
db_preflight = native_fixture.db_preflight
owned_database_preflight = native_fixture.owned_database_preflight


@pytest.mark.parametrize(
    ("url", "skipped"),
    [
        ("postgresql+asyncpg://test:test@foreign.invalid/daimon_test_nc_n11", False),
        ("postgresql+asyncpg://test:test@localhost/unowned_test", False),
        ("postgresql+asyncpg://test:test@localhost/daimon_test_nc_n11?host=foreign.invalid", False),
        ("postgresql+asyncpg://test:test@localhost/daimon_test", True),
    ],
)
def test_database_preflight_precedes_connections(tmp_path: Path, url: str, skipped: bool) -> None:
    """Exercise real pytest fixture ordering; every attempted connection is trapped."""
    trap = tmp_path / "connection-attempted"
    plugin = tmp_path / "c18_connection_trap.py"
    plugin.write_text(
        "from pathlib import Path\n"
        "from sqlalchemy import event\n"
        "from sqlalchemy.engine import Engine\n"
        "def pytest_sessionstart(session):\n"
        "    def forbid(*args, **kwargs):\n"
        f"        Path({str(trap)!r}).write_text('connection attempted')\n"
        "        raise RuntimeError('OpenAI host refused URL reached connection setup')\n"
        "    event.listen(Engine, 'do_connect', forbid)\n"
    )
    env = dict(os.environ)
    for name in ("GITHUB_ACTIONS", "CI", "PYTEST_ADDOPTS"):
        env.pop(name, None)
    env["DAIMON_DATABASE__TEST_URL"] = url
    env["PYTHONPATH"] = os.pathsep.join((str(tmp_path), env.get("PYTHONPATH", "")))
    selected = (
        f"{Path(__file__).resolve()}"
        "::test_native_final_host_outcome_has_exact_committed_snapshot[completed-completed-False]"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-c",
            "pyproject.toml",
            "-o",
            "addopts=",
            "-n",
            "0",
            "-p",
            "c18_connection_trap",
            selected,
            "-q",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert not trap.exists(), result.stdout + result.stderr
    if skipped:
        assert result.returncode == 0 and "1 skipped" in result.stdout
    else:
        assert result.returncode == 1, result.stdout + result.stderr
        assert "Test hooks require owned local lane engines" in result.stdout
        assert "skipped" not in result.stdout


@pytest.mark.parametrize("registered", [True, False])
def test_database_preflight_honors_registered_isolated_ci(
    monkeypatch: pytest.MonkeyPatch, registered: bool
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("CI", "true")
    schema = f"test_w{os.getpid()}_1234abcd" if registered else "unowned_schema"
    url = "postgresql+asyncpg://test:test@localhost/daimon_test"
    monkeypatch.setenv("DAIMON_DATABASE__TEST_URL", url)
    if registered:
        preflight_test_database(schema)
    else:
        with pytest.raises(ValueError, match="owned local lane engines"):
            preflight_test_database(schema)


class FailureWire(native_fixture.NativeWire):
    reject_input = False

    def handle(self, req: httpx.Request) -> httpx.Response:
        if self.reject_input and req.method == "POST" and req.url.path.endswith("/events"):
            self.requests.append(req)
            return httpx.Response(
                429,
                json={"error": {"type": "rate_limit_error", "code": "rate_limit_exceeded"}},
            )
        return super().handle(req)


@pytest.fixture(autouse=True)
def clean_sdk_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in SDK_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(native_fixture, "NativeWire", FailureWire)


@pytest.mark.parametrize(
    ("native_status", "expected", "replay"),
    [
        ("completed", TerminationReason.COMPLETED, False),
        ("failed", TerminationReason.UPSTREAM, False),
        ("cancelled", TerminationReason.INTERRUPTED, False),
        ("session_failed", TerminationReason.SESSION_TERMINATED, False),
        ("failed", TerminationReason.UPSTREAM, True),
        ("cancelled", TerminationReason.INTERRUPTED, True),
    ],
)
async def test_native_final_host_outcome_has_exact_committed_snapshot(
    native_host: Host,
    monkeypatch: pytest.MonkeyPatch,
    native_status: str,
    expected: TerminationReason,
    replay: bool,
    record_property: Callable[[str, object], None],
) -> None:
    request, prepared, _, store, wire, _ = native_host
    results: list[RunOutcome] = []

    class TerminalStream(NativeStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            if replay and len(self.wire.native_streams) > 1:
                await asyncio.Event().wait()
            await self.wire.sent.wait()
            root = self.wire.turns[-1]
            root["status"] = native_status if native_status != "session_failed" else "in_progress"
            records: list[dict[str, Any]] = [
                {
                    "type": "agent.session.turn.in_progress",
                    "event_id": root["id"] + ":running",
                    "turn": {**root, "status": "in_progress"},
                },
            ]
            if not replay:
                records.append(
                    {
                        "type": "agent.session.failed"
                        if native_status == "session_failed"
                        else "agent.session.turn." + native_status,
                        "event_id": root["id"] + ":done",
                        "turn": dict(root),
                    }
                )
            for record in records:
                record["session_id"] = self.wire.session_id
                yield ("data: " + json.dumps(record) + "\n\n").encode()
            if replay:
                raise httpx.ReadError("offline disconnect before terminal")
            await asyncio.Event().wait()

    monkeypatch.setattr(native_fixture, "NativeStream", TerminalStream)

    async def run():
        async def reseed() -> str:
            raise AssertionError("a native terminal cannot resend its input")

        result = await run_prepared_turn(
            request.deps,
            prepared,
            tenant_id=request.tenant_id,
            platform="slack",
            thread_id="thread",
            external_user_id="caller",
            user_message="offline terminal proof",
            lifecycle=RecordingLifecycle(),
            cancel=asyncio.Event(),
            reseed_user_message=reseed,
            recovery_lifecycle=lambda cancel: RecordingLifecycle(),
            render_interval_s=0.01,
            operation_key="c18-" + native_status + str(replay),
        )
        results.append(result)
        return result

    envelope = OpenAITerminationEnvelope(
        request.deps.sessionmaker,
        request.scope,
        "slack",
        (
            TerminationCase(
                "native-" + native_status + ("-replay" if replay else "-stream"),
                expected,
                prepared.session_ref,
                run,
                expected_error_class="TurnError" if expected.is_failure or replay else None,
            ),
        ),
    )
    facts = await envelope.evidence()
    assert facts["required_reasons"] == tuple(reason.value for reason in REQUIRED_REASONS)
    assert len(facts["terminations"]) == 1
    row = facts["terminations"][0]
    assert set(row) == set(TerminationSnapshot.__annotations__)
    assert row["persisted_rows"] == 1
    assert row["expected_reason"] == row["actual_reason"] == row["persisted_reason"] == expected
    assert row["expected_error_class"] == row["actual_error_class"] == row["persisted_error_class"]
    assert (
        row["expected_session"]
        == row["actual_session"]
        == row["persisted_session"]
        == wire.session_id
    )
    assert row["expected_account"] == row["persisted_account"] == request.scope.account_id
    assert row["expected_tenant"] == row["persisted_tenant"] == request.scope.tenant_id
    assert row["expected_platform"] == row["persisted_platform"] == "slack"
    assert (
        row["expected_recovered"] is row["actual_recovered"] is row["persisted_recovered"] is False
    )
    assert results[0].state.termination == expected
    rows = await store.read_events(wire.session_id)
    assert any(
        event.type
        == (
            "session.status_terminated"
            if native_status == "session_failed"
            else "session.turn_ended"
        )
        for event in rows
    )
    assert sum(r.method == "POST" and r.url.path.endswith("/events") for r in wire.requests) == 1
    assert all(stream.closed for stream in wire.native_streams)
    # A replay is not a replaced-session recovery; the real host supplies False.
    assert results[0].recovered is False
    record_property("termination_evidence", json.dumps(facts))


async def test_partial_native_envelope_keeps_c18_pending_before_execution(
    native_host: Host,
) -> None:
    request, prepared, _, _, wire, _ = native_host

    async def never_run():
        raise AssertionError("partial evidence must not be dispatched as a complete C18 case")

    envelope = OpenAITerminationEnvelope(
        request.deps.sessionmaker,
        request.scope,
        "slack",
        (
            TerminationCase(
                "only-root", TerminationReason.COMPLETED, prepared.session_ref, never_run
            ),
        ),
    )
    registry = Registry()
    async with register(registry, sdk_factory=build_async_openai):
        original = registry.create("openai.offline")
        adapter = compose_openai_host(original, termination=envelope)
        assert isinstance(adapter.transport, OpenAIHostTransport)
        assert adapter.transport.host_termination_evidence is not None
        assert not inspect.signature(adapter.transport.host_termination_evidence).parameters
        assert inspect.iscoroutinefunction(adapter.transport.host_termination_evidence)
        assert "requires_action" in adapter.pending["C18"].detail
        assert (await run_fixture("C18", adapter)).status == "pending"
        assert adapter.transport.mutation_count == 0
        assert not wire.turns


@pytest.mark.parametrize("mode", ["rate_limited", "ceiling", "missing_runtime"])
async def test_actual_host_refusal_and_deadline_rows(
    native_host: Host, mode: str, record_property: Callable[[str, object], None]
) -> None:
    request, prepared, _, _, wire, _ = native_host
    reasons = {
        "rate_limited": TerminationReason.RATE_LIMITED,
        "ceiling": TerminationReason.CEILING,
        "missing_runtime": TerminationReason.ADMISSION_DENIED,
    }
    if mode == "rate_limited":
        assert isinstance(wire, FailureWire)
        wire.reject_input = True

    async def run():
        if mode == "missing_runtime":
            # The normal wrapper records the admitted account, without seeding
            # observation identity from the case's expected values.
            await bind_session(
                replace(request.deps, turn_runtimes={}),
                request.admission,
                tenant_id=request.tenant_id,
                platform="slack",
                thread_id="thread",
                external_user_id="caller",
                session_account_id=request.session_account_id,
                reuse_existing=True,
            )
            raise AssertionError("missing runtime was admitted")

        async def reseed() -> str:
            raise AssertionError("a refused input must not be retried")

        return await run_prepared_turn(
            request.deps,
            prepared,
            tenant_id=request.tenant_id,
            platform="slack",
            thread_id="thread",
            external_user_id="caller",
            user_message="offline refused input",
            lifecycle=RecordingLifecycle(),
            cancel=asyncio.Event(),
            reseed_user_message=reseed,
            recovery_lifecycle=lambda cancel: RecordingLifecycle(),
            deadline=datetime.now(UTC) - timedelta(seconds=1) if mode == "ceiling" else None,
            render_interval_s=0.01,
            operation_key="c18-" + mode,
        )

    before = len(wire.requests)
    facts = await OpenAITerminationEnvelope(
        request.deps.sessionmaker,
        request.scope,
        "slack",
        (
            TerminationCase(
                mode,
                reasons[mode],
                None if mode == "missing_runtime" else prepared.session_ref,
                run,
                expected_error_class="AdmissionDenied"
                if mode == "missing_runtime"
                else "TurnError",
            ),
        ),
    ).evidence()
    row = facts["terminations"][0]
    assert (
        row["expected_reason"] == row["actual_reason"] == row["persisted_reason"] == reasons[mode]
    )
    assert row["persisted_rows"] == 1
    assert row["expected_error_class"] == row["actual_error_class"] == row["persisted_error_class"]
    assert row["expected_session"] == row["actual_session"] == row["persisted_session"]
    assert row["persisted_account"] == request.scope.account_id
    assert row["persisted_tenant"] == request.scope.tenant_id
    assert row["persisted_platform"] == "slack"
    assert row["actual_recovered"] is row["persisted_recovered"] is False
    assert not wire.turns
    if mode == "missing_runtime":
        assert row["persisted_session"] is None
        assert len(wire.requests) == before
    record_property("termination_evidence", json.dumps(facts))


@pytest.mark.parametrize("field", ["provider", "kind", "tenant_id", "account_id"])
def test_foreign_native_identity_is_refused_before_callback(native_host: Host, field: str) -> None:
    request, prepared, _, _, wire, _ = native_host
    assert prepared.session_ref is not None

    async def never_run():
        raise AssertionError("foreign identity was dispatched")

    bad = prepared.session_ref.model_copy(update={field: "foreign"})
    before = len(wire.requests)
    with pytest.raises(ScopeViolation):
        OpenAITerminationEnvelope(
            request.deps.sessionmaker,
            request.scope,
            "slack",
            (TerminationCase("foreign", TerminationReason.COMPLETED, bad, never_run),),
        )
    assert not wire.turns
    assert len(wire.requests) == before


async def test_declared_cases_are_snapshotted_and_complete_coverage_is_explicit(
    native_host: Host,
) -> None:
    request, prepared, _, _, wire, _ = native_host

    async def never_run():
        raise AssertionError("coverage declarations do not execute native cases")

    cases = [
        TerminationCase(reason.value, reason, prepared.session_ref, never_run)
        for reason in REQUIRED_REASONS
    ]
    envelope = OpenAITerminationEnvelope(
        request.deps.sessionmaker,
        request.scope,
        "slack",
        cast(tuple[TerminationCase, ...], cases),
    )
    cases.clear()
    assert len(envelope.cases) == len(REQUIRED_REASONS)
    assert not envelope.missing_reasons
    registry = Registry()
    async with register(registry, sdk_factory=build_async_openai):
        adapter = compose_openai_host(registry.create("openai.offline"), termination=envelope)
        assert "C18" not in adapter.pending
        assert isinstance(adapter.transport, OpenAIHostTransport)
        assert adapter.transport.host_termination_evidence is not None
        # Registration alone does not execute a case or certify its result.
        assert adapter.transport.mutation_count == 0
        assert not wire.turns


def test_inventory_is_independent_and_reserved_reasons_are_excluded() -> None:
    assert set(REQUIRED_REASONS) | UNREACHABLE_REASONS == set(TerminationReason)
    assert not set(REQUIRED_REASONS) & UNREACHABLE_REASONS
    assert {
        TerminationReason.COMPLETED,
        TerminationReason.INTERRUPTED,
        TerminationReason.UPSTREAM,
        TerminationReason.SESSION_TERMINATED,
        TerminationReason.CEILING,
        TerminationReason.ADMISSION_DENIED,
    } <= set(REQUIRED_REASONS)


@pytest.mark.parametrize("routing", ["primary", "mutable_route"])
async def test_actual_host_callback_cannot_reroute_queued_writes_or_evidence_reads(
    native_host: Host, routing: str
) -> None:
    request, prepared, _, _, wire, _ = native_host
    session_ref = prepared.session_ref
    assert session_ref is not None
    sessions = request.deps.sessionmaker
    original = dict(sessions.kw)
    foreign = create_async_engine(
        "postgresql+asyncpg://unused:unused@127.0.0.1:1/daimon_test_foreign_probe"
    )
    crossings: list[bool] = []
    results: list[RunOutcome] = []
    routes: dict[object, object] = {}
    if routing == "mutable_route":
        sessions.configure(binds=routes)

    def trap(*args: object, **kwargs: object) -> None:
        crossings.append(True)
        raise RuntimeError("C18 callback changed routing reached a foreign connection")

    sql_event.listen(foreign.sync_engine, "do_connect", trap)

    async def run() -> RunOutcome:
        async def reseed() -> str:
            raise AssertionError("unexpected resend")

        outcome = await run_prepared_turn(
            request.deps,
            prepared,
            tenant_id=request.tenant_id,
            platform="slack",
            thread_id="thread",
            external_user_id="caller",
            user_message="callback routing admission proof",
            lifecycle=RecordingLifecycle(),
            cancel=asyncio.Event(),
            reseed_user_message=reseed,
            recovery_lifecycle=lambda cancel: RecordingLifecycle(),
            render_interval_s=0.01,
            operation_key="c18-callback-routing-" + routing,
        )
        results.append(outcome)
        if routing == "primary":
            sessions.configure(bind=foreign)
        else:
            routes[TurnOutcome] = foreign
        return outcome

    try:
        facts = await OpenAITerminationEnvelope(
            sessions,
            request.scope,
            "slack",
            (TerminationCase("actual-completed", TerminationReason.COMPLETED, session_ref, run),),
        ).evidence()
        assert len(wire.turns) == len(results) == 1
        assert results[0].state.termination is TerminationReason.COMPLETED
        assert not crossings
        row = facts["terminations"][0]
        assert row["actual_reason"] == row["persisted_reason"] == "completed"
        assert row["persisted_rows"] == 1
        assert row["actual_session"] == row["persisted_session"] == session_ref.id
    finally:
        sessions.kw.clear()
        sessions.kw.update(original)
        await outcomes.drain_outcomes()
        await foreign.dispose()


@pytest.mark.parametrize(
    "corruption",
    [
        "reason",
        "error_class",
        "session_id",
        "account_id",
        "tenant_id",
        "platform",
        "recovered",
        "omit",
    ],
)
async def test_actual_sdk_outcome_writer_corruption_remains_visible(
    native_host: Host, monkeypatch: pytest.MonkeyPatch, corruption: str
) -> None:
    request, prepared, _, _, wire, _ = native_host
    original = outcomes.record

    async def corrupt(db: AsyncSession, row: OutcomeRecord) -> None:
        if corruption == "omit":
            return
        changes = {
            "reason": TerminationReason.UPSTREAM,
            "error_class": "ForeignError",
            "session_id": "foreign-native",
            "account_id": None,
            "tenant_id": None,
            "platform": "discord",
            "recovered": True,
        }
        await original(db, replace(row, **{corruption: changes[corruption]}))

    monkeypatch.setattr(outcomes, "record", corrupt)

    async def run():
        async def reseed() -> str:
            raise AssertionError("unexpected resend")

        return await run_prepared_turn(
            request.deps,
            prepared,
            tenant_id=request.tenant_id,
            platform="slack",
            thread_id="thread",
            external_user_id="caller",
            user_message="writer corruption proof",
            lifecycle=RecordingLifecycle(),
            cancel=asyncio.Event(),
            reseed_user_message=reseed,
            recovery_lifecycle=lambda cancel: RecordingLifecycle(),
            render_interval_s=0.01,
            operation_key="c18-corrupt-" + corruption,
        )

    facts = await OpenAITerminationEnvelope(
        request.deps.sessionmaker,
        request.scope,
        "slack",
        (
            TerminationCase(
                "native-completed", TerminationReason.COMPLETED, prepared.session_ref, run
            ),
        ),
    ).evidence()
    row: Mapping[str, object] = facts["terminations"][0]
    assert row["actual_reason"] == row["expected_reason"] == "completed"
    assert wire.turns
    if corruption in {"omit", "tenant_id"}:
        assert row["persisted_rows"] == 0
        assert row["persisted_reason"] is None
    else:
        key = {"session_id": "session", "account_id": "account"}.get(corruption, corruption)
        assert row["persisted_rows"] == 1
        assert (
            row["actual_" + key] != row["persisted_" + key]
            if key in {"reason", "error_class", "session", "recovered"}
            else row["expected_" + key] != row["persisted_" + key]
        )
