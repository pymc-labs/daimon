"""Opt-in A7 preparation: isolated SDK replay with real host persistence.

The explicit live mode delegates to the guarded one-attempt entry; other modes
never read credentials or access a canonical ledger. The caller owns its
migrated test DB, tenant and quiet-window GO.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import hashlib
import importlib.metadata
import json
import math
import os
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from unittest.mock import patch

import httpx
from anthropic import AsyncAnthropic
from anthropic.types import RawMessageStreamEvent
from anthropic.types.beta.session_create_params import SessionCreateParams
from anthropic.types.beta.sessions import BetaManagedAgentsSpanModelRequestEndEvent
from daimon.core.config import load_turn_settings
from daimon.core.constants import MA_MAX_RETRIES
from daimon.core.stores.mux_state import PostgresStateStore
from daimon.core.turn import driver
from daimon.core.turn.io import LegacyTurnIO, MuxTurnIO, TurnIO
from daimon.core.turn.persistence import TurnPersistence
from daimon.core.turn.posture import BillingExempt
from daimon.testing.db import require_owned_sessionmaker
from daimon.testing.turn_fakes import RecordingLifecycle
from mux.contracts.ids import ChannelRef, ResourceRef, Scope, ThreadRef
from mux.contracts.resources import ProviderBinding
from mux.state.store import binding_slot
from mux.testing.drivers.anthropic.ma import send_events_response
from mux.testing.drivers.anthropic.ma_models import ma_model_usage
from mux.testing.drivers.anthropic.ma_transport import ScriptedReply, ScriptedTransport
from mux.testing.isolated_sdk import AmbientSDKConfigurationError, build_async_anthropic
from sqlalchemy import event as sql_event
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker

HAIKU = "claude-haiku-5-5"
PathName = Literal["legacy", "mux"]
OFFLINE_PROBES = (
    "packages/core/tests/turn/test_a7_anthropic.py",
    "packages/core/tests/turn/test_persistence.py::test_production_retries_cannot_duplicate_a_claimed_native_mutation",
    "packages/core/tests/turn/test_persistence.py::test_a_claim_keeps_the_default_retry_policy_for_reads_and_legacy_mutations",
    "packages/core/tests/turn/test_persistence.py::test_uncertain_sdk_delivery_restarts_by_replay_without_another_post",
    "packages/core/tests/turn/test_persistence.py::test_crash_boundaries_restart_without_duplicate_provider_delivery",
    "packages/core/tests/turn/test_persistence.py::test_replay_overlap_preserves_journal_sequence_and_preview_authority",
    "packages/core/tests/turn/test_persistence.py::test_live_foreign_holder_is_refused_before_provider_io",
    "packages/core/tests/turn/test_persistence.py::test_journal_owner_must_be_persisted_and_scope_must_match",
    "packages/core/tests/turn/test_persistence.py::test_cancel_ack_cannot_complete_the_journal_but_independent_stop_can",
    "packages/core/tests/turn/test_persistence.py::test_main_confirmation_recovery_keeps_legacy_wire_and_the_mux_fence",
    "packages/core/tests/turn/test_driver_cancel.py",
    "packages/core/tests/turn/test_driver.py::test_double_connection_error_surfaces_connection_lost",
)


def refuse_ambient() -> None:
    # Protect an already allocated SDK transport against late configuration.
    # Construction and copies use the shared factory's independent guard.
    if any(
        name.upper().startswith(("ANTHROPIC_", "OPENAI_", "GOOGLE_", "GEMINI_"))
        for name in os.environ
    ):
        raise AmbientSDKConfigurationError("A7 offline SDK refuses ambient provider configuration")


def offline_client(transport: ScriptedTransport) -> AsyncAnthropic:
    """A real shared isolated SDK, including guarded single-attempt copies."""

    def dispatch(request: httpx.Request) -> httpx.Response:
        refuse_ambient()
        return transport.dispatch(request)

    return build_async_anthropic(
        api_key="a7-offline-only",
        base_url="https://anthropic.invalid",
        headers={},
        max_retries=MA_MAX_RETRIES,
        timeout=5,
        transport=httpx.MockTransport(dispatch),
    )


def isolated_engine(sessions: async_sessionmaker[AsyncSession]) -> AsyncEngine:
    """Delegate every route to the owner's shared guard before touching SQL."""
    require_owned_sessionmaker(sessions)
    bind = sessions.kw.get("bind")
    engine = bind.engine if isinstance(bind, AsyncConnection) else bind
    if not isinstance(engine, AsyncEngine):
        raise ValueError("A7 requires a primary async engine to count timed SQL")
    return engine


@dataclass(frozen=True)
class QuietPolicy:
    max_load_per_cpu: float = 0.5
    max_cpu_pressure_percent: float = 10.0

    def accepts(self, samples: Sequence[Mapping[str, float]]) -> bool:
        return len(samples) >= 2 and all(
            all(math.isfinite(value) for value in sample.values())
            and sample["cpu_count"] >= 1
            and 0 <= sample["load1"] <= self.max_load_per_cpu * sample["cpu_count"]
            and 0 <= sample["cpu_some_avg10_percent"] <= self.max_cpu_pressure_percent
            for sample in samples
        )


def host_sample() -> Mapping[str, float]:
    fields = Path("/proc/pressure/cpu").read_text().splitlines()[0].split()[1:]
    pressure = dict(field.split("=", 1) for field in fields)
    return {
        "monotonic_s": time.monotonic(),
        "load1": os.getloadavg()[0],
        "cpu_count": float(os.cpu_count() or 0),
        "cpu_some_avg10_percent": float(pressure["avg10"]),
    }


def percentile(values: Sequence[float], quantile: float) -> float:
    ordered = sorted(values)
    index = (len(ordered) - 1) * quantile
    low, high = math.floor(index), math.ceil(index)
    return ordered[low] + (ordered[high] - ordered[low]) * (index - low)


class _Lifecycle(RecordingLifecycle):
    first_event_ns: int | None = None

    async def on_sse_event(self, event: RawMessageStreamEvent) -> None:
        if self.first_event_ns is None:
            self.first_event_ns = time.perf_counter_ns()
        await super().on_sse_event(event)


@dataclass(frozen=True)
class TurnEvidence:
    selected_path: PathName
    io_class: str
    duration_ms: float
    first_event_ms: float
    digest: str
    http_calls: int
    sql_statements: int
    journal_records: int
    claim_accepted: bool | None
    lease_released: bool | None


class StateReplay:
    """Seed/verify outside timing; measure real driver/SDK/journal/claim/lease work.

    Warm mode reuses one SDK client. Cold mode includes only SDK allocation and
    close in each turn's timing, and remains a separately labelled diagnostic.
    Fresh pair slots prevent journal deduplication from hiding persistence cost.
    """

    def __init__(self, sessions: async_sessionmaker[AsyncSession], scope: Scope) -> None:
        self.engine = isolated_engine(sessions)
        if scope.is_platform or scope.is_legacy_host_authorized:
            raise ValueError("A7 requires an authorized tenant/account scope")
        self.scope = scope
        self.store = PostgresStateStore(sessions)
        self.trace = ScriptedTransport()
        self.client: AsyncAnthropic | None = None

    async def prepare(self, pair: str) -> ProviderBinding:
        binding = ProviderBinding(
            id=f"a7:{pair}",
            thread=ThreadRef(
                channel=ChannelRef(
                    tenant_id=self.scope.tenant_id, platform="slack", channel_id="a7"
                ),
                thread_id=pair,
            ),
            provider="anthropic",
            profile="anthropic.managed_agents",
            generation=1,
            config_revision=0,
            native_refs={"session": f"a7-session-{pair}"},
            legacy_account_id=self.scope.account_id,
        )
        await self.store.put_binding(binding, expected_generation=0)
        return binding

    async def turn(
        self, binding: ProviderBinding, path: PathName, *, cold_client: bool
    ) -> TurnEvidence:
        refuse_ambient()
        session_id = binding.native_refs["session"]
        stamp = datetime(2026, 1, 1, tzinfo=UTC)
        usage = BetaManagedAgentsSpanModelRequestEndEvent(
            id="usage",
            type="span.model_request_end",
            model_request_start_id="request",
            model_usage=ma_model_usage(input_tokens=100, output_tokens=50),
            processed_at=stamp,
        ).model_dump(mode="json")
        self.trace.queue(
            ScriptedReply.stream(
                f"/v1/sessions/{session_id}/events/stream",
                [
                    {
                        "id": "message",
                        "type": "agent.message",
                        "content": [{"type": "text", "text": "offline answer"}],
                        "processed_at": stamp.isoformat(),
                    },
                    usage,
                    {
                        "id": "ended",
                        "type": "session.status_idle",
                        "stop_reason": {"type": "end_turn"},
                        "processed_at": stamp.isoformat(),
                    },
                ],
            ),
            ScriptedReply("POST", f"/v1/sessions/{session_id}/events", send_events_response()),
        )
        lifecycle = _Lifecycle()
        persistence = TurnPersistence(
            self.store, binding, self.scope, operation_key="a7:" + session_id
        )
        statements = 0

        def counted(*args: object) -> None:
            nonlocal statements
            statements += 1

        # Warm allocation is outside the clock and occurs once per replay.
        if not cold_client and self.client is None:
            self.client = offline_client(self.trace)
        prior_calls = len(self.trace.requests)
        previous = os.environ.get("DAIMON_TURN__PATH")
        os.environ["DAIMON_TURN__PATH"] = path
        sql_event.listen(self.engine.sync_engine, "after_cursor_execute", counted)
        client = self.client
        observed: list[str] = []
        factory = driver.turn_io  # pyright: ignore[reportPrivateImportUsage] - observe the driver's actual factory alias.

        def observe(*args: Any, **kwargs: Any) -> TurnIO:  # noqa: ANN401 - native factory seam.
            io = factory(*args, **kwargs)
            expected = LegacyTurnIO if path == "legacy" else MuxTurnIO
            if not isinstance(io, expected):
                raise RuntimeError("A7 requested path did not construct its actual TurnIO")
            observed.append(type(io).__name__)
            return io

        start = time.perf_counter_ns()
        try:
            if cold_client:
                client = offline_client(self.trace)
            if client is None:
                raise RuntimeError("A7 client was not initialized")
            selected = load_turn_settings(_env_file=None).path
            with (
                patch.object(driver, "turn_io", observe),
                persistence.activate() if path == "mux" else nullcontext(),
            ):
                state = await driver.run_turn(
                    anthropic=client,
                    session_id=session_id,
                    session_ref=ResourceRef(
                        id=session_id,
                        kind="session",
                        provider="anthropic",
                        account_scope_id="a7-offline",
                        tenant_id=self.scope.tenant_id,
                        account_id=self.scope.account_id,
                    ),
                    user_message="offline question",
                    lifecycle=lifecycle,
                    cancel=asyncio.Event(),
                    billing=BillingExempt(reason="headless-unrecorded"),
                    scope=self.scope,
                    path=selected,
                    now=lambda: stamp,
                    render_interval_s=3600,
                )
        finally:
            if cold_client and client is not None:
                await client.close()
            end = time.perf_counter_ns()
            sql_event.remove(self.engine.sync_engine, "after_cursor_execute", counted)
            if previous is None:
                os.environ.pop("DAIMON_TURN__PATH", None)
            else:
                os.environ["DAIMON_TURN__PATH"] = previous
        if self.trace.violations or self.trace.replies:
            raise RuntimeError("A7 script was violated or incompletely consumed")
        if len(observed) != 1:
            raise RuntimeError("A7 did not observe exactly one driver factory call")
        if lifecycle.first_event_ns is None or not start <= lifecycle.first_event_ns <= end:
            raise RuntimeError("A7 did not observe an event inside the measured turn")
        requests = self.trace.requests[prior_calls:]
        effects = {
            "requests": [
                [
                    r.method,
                    r.path.replace(session_id, "session"),
                    r.query,
                    r.body.decode(),
                    r.protocol_headers,
                ]
                for r in requests
            ],
            "events": [e.model_dump(mode="json") for e in lifecycle.sse_events],
            "content": [dataclasses.asdict(block) for block in state.content],
            "usage": dataclasses.asdict(state.usage_totals),
            "termination": state.termination,
            "successes": len(lifecycle.terminal_success),
            "failures": len(lifecycle.terminal_failures),
        }
        digest = hashlib.sha256(json.dumps(effects, sort_keys=True).encode()).hexdigest()
        records = await self.store.read_events(session_id)
        operation = await self.store.get_operation(self.scope, "a7:" + session_id + ":send:0")
        accepted = operation is not None and operation.operation.status == "accepted"
        if path == "legacy" and statements != 0:
            raise RuntimeError("default legacy added persistence I/O")
        if path == "mux" and (
            statements == 0
            or not accepted
            or [e.native.event_id for e in records] != ["message", "usage", "ended"]
        ):
            raise RuntimeError("mux replay did not persist every record and its claimed send")
        released: bool | None = None
        if path == "mux":
            lease = await self.store.acquire_lease(
                binding_slot(binding),
                holder="a7-successor",
                turn_id="verify",
                now=datetime.now(UTC),
                ttl=timedelta(minutes=1),
            )
            released = not lease.took_over
            await self.store.release_lease(lease)
            if not released:
                raise RuntimeError("mux lease had to be taken over rather than released")
        return TurnEvidence(
            selected,
            observed[0],
            (end - start) / 1e6,
            (lifecycle.first_event_ns - start) / 1e6,
            digest,
            len(requests),
            statements,
            len(records) if path == "mux" else 0,
            accepted if path == "mux" else None,
            released,
        )

    async def close(self) -> None:
        if self.client is not None:
            await self.client.close()


async def benchmark(
    replay: StateReplay,
    *,
    namespace: str,
    iterations: int = 200,
    warmups: int = 20,
    cold_client: bool = False,
    diagnostic: bool = False,
    quiet_window_go: bool = False,
    sample: Callable[[], Mapping[str, float]] = host_sample,
) -> Mapping[str, object]:
    if iterations < 2 or warmups < 0 or not namespace.strip():
        raise ValueError("A7 needs a namespace, two pairs and nonnegative warmups")
    if not diagnostic and (not quiet_window_go or iterations < 200 or warmups < 20):
        raise ValueError("A7 certification requires quiet-window GO and 200+20 pairs")
    pin = source_pin()
    if not diagnostic and pin["clean"] is not True:
        raise ValueError("A7 quiet benchmark requires a clean pinned checkout")
    samples = [sample()]
    pairs: list[dict[str, object]] = []
    added: list[float] = []
    first_added: list[float] = []
    stop = asyncio.Event()

    async def monitor() -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=1)
            except TimeoutError:
                samples.append(sample())

    task = asyncio.create_task(monitor())
    try:
        for index in range(iterations + warmups):
            binding = await replay.prepare(f"{namespace}-{index}")
            paths: tuple[PathName, PathName] = (
                ("legacy", "mux") if index % 2 == 0 else ("mux", "legacy")
            )
            turns = {
                path: await replay.turn(binding, path, cold_client=cold_client) for path in paths
            }
            if turns["legacy"].digest != turns["mux"].digest or any(
                t.http_calls != 2 for t in turns.values()
            ):
                raise RuntimeError("A7 legacy/mux effects or native HTTP work differ")
            if index >= warmups:
                added.append(turns["mux"].duration_ms - turns["legacy"].duration_ms)
                first_added.append(turns["mux"].first_event_ms - turns["legacy"].first_event_ms)
            pairs.append(
                {
                    "index": index,
                    "warmup": index < warmups,
                    "order": paths,
                    "turns": {path: dataclasses.asdict(t) for path, t in turns.items()},
                }
            )
    finally:
        stop.set()
        await task
        samples.append(sample())
        await replay.close()
    quiet = QuietPolicy().accepts(samples)
    p50, p95 = percentile(added, 0.5), percentile(added, 0.95)
    return {
        "status": "diagnostic" if diagnostic else "measured" if quiet else "blocked",
        "client_mode": "cold-diagnostic" if cold_client else "warm",
        "state_store": "postgres",
        "pairs": pairs,
        "host_samples": samples,
        "quiet_policy": dataclasses.asdict(QuietPolicy()),
        "quiet_host": quiet,
        "p50_added_ms": p50,
        "p95_added_ms": p95,
        "first_event_p50_added_ms": percentile(first_added, 0.5),
        "first_event_p95_added_ms": percentile(first_added, 0.95),
        "limits_ms": {"p50": 5, "p95": 20},
        "certification_gate_passed": not diagnostic
        and not cold_client
        and pin == source_pin()
        and quiet
        and p50 <= 5
        and p95 <= 20,
        "sdk_versions": {n: importlib.metadata.version(n) for n in ("anthropic", "httpx")},
        "created_at": datetime.now(UTC).isoformat(),
        "external_provider_calls": 0,
        "source": pin,
        "live_model_policy": HAIKU,
        "limitations": [
            "One billed-exempt scripted turn; no live-token/$ equivalence or full M0 certificate.",
            "Binding setup, assertions and successor-lease check excluded from timed turn.",
        ],
    }


def source_pin() -> Mapping[str, object]:
    root = Path(__file__).resolve().parents[4]

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True, check=True
        ).stdout.strip()

    return {"head": git("rev-parse", "HEAD"), "clean": not git("status", "--porcelain")}


def live_readiness() -> Mapping[str, object]:
    """Describe unmet native admission facts; never load credentials or ledger."""
    fields = sorted(SessionCreateParams.__annotations__)
    return {
        "status": "PENDING",
        "model": HAIKU,
        "external_provider_calls": 0,
        "sdk_session_create_fields": fields,
        "sdk_session_create_has_native_budget_field": any(
            name in fields for name in ("spend_control", "budget", "max_tokens")
        ),
        "required_owner_evidence": [
            "Lead live GO, disposable scope, reviewed limits and cleanup ownership.",
            "N9 canonical BudgetGuard reservation before any key read or SDK allocation.",
            "N9/N6 native executable case callbacks; recipe BOUND is not callback availability.",
            "Guard-only: no native cap; total300s and cumulative $.28 streamed stop.",
            "Actual normalized usage plus container/session time, uncertain reservations retained.",
            "Fresh offline replay of normalized receipts; all required cases executable.",
        ],
        "source": source_pin(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        default="offline-probes",
        choices=("offline-probes", "quiet-benchmark", "live-plan", "live"),
    )
    parser.add_argument("--output", required=True, type=Path, help="fresh evidence directory")
    parser.add_argument("--quiet-window-go", action="store_true")
    from daimon.testing.anthropic_live import add_live_arguments, live_command

    add_live_arguments(parser)
    args = parser.parse_args()
    if args.mode == "live":
        live_command("A7", args)
        return
    refuse_ambient()
    if args.output.exists():
        parser.error("evidence directory must be new; preserve earlier receipts")
    if args.mode == "quiet-benchmark" and not args.quiet_window_go:
        parser.error("quiet benchmark requires explicit lead quiet-window GO")
    args.output.mkdir(parents=True)
    root = Path(__file__).resolve().parents[4]
    receipt: dict[str, object] = {
        "mode": args.mode,
        "source": source_pin(),
        "external_provider_calls": 0,
        "full_m0_certificate": False,
    }
    if args.mode == "live-plan":
        receipt["native_admission"] = live_readiness()
        recipes = subprocess.run(
            [sys.executable, str(root / "tests/golden/live/bindings.py")],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        receipt["recipe_returncode"] = recipes.returncode
        receipt["recipes"] = json.loads(recipes.stdout) if recipes.returncode == 0 else None
        receipt["status"] = "PENDING" if recipes.returncode == 0 else "FAILED"
    else:
        benchmark_path = args.output.resolve() / "benchmark.json"
        previous = {
            name: os.environ.get(name) for name in ("DAIMON_A7_QUIET_GO", "DAIMON_A7_OUTPUT")
        }
        try:
            if args.mode == "quiet-benchmark":
                os.environ["DAIMON_A7_QUIET_GO"] = "yes"
                os.environ["DAIMON_A7_OUTPUT"] = str(benchmark_path)
                probes = (
                    "packages/core/tests/turn/test_a7_anthropic.py::test_a7_quiet_host_benchmark",
                )
            else:
                os.environ.pop("DAIMON_A7_QUIET_GO", None)
                probes = OFFLINE_PROBES
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    *probes,
                    "-n",
                    "2",
                    "-q",
                    "--junitxml",
                    str(args.output.resolve() / "tests.xml"),
                    "--basetemp",
                    str(args.output.resolve() / "pytest-tmp"),
                ],
                cwd=root,
                check=False,
            )
            receipt.update(
                status="offline-pass" if result.returncode == 0 else "FAILED",
                pytest_returncode=result.returncode,
                probes=probes,
            )
            if args.mode == "quiet-benchmark":
                receipt["benchmark_artifact_exists"] = benchmark_path.is_file()
                if not benchmark_path.is_file():
                    receipt["status"] = "FAILED"
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
    (args.output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    if receipt["status"] == "FAILED":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
