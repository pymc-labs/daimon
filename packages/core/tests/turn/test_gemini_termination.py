"""Owned SDK bytes -> public Gemini host wrappers -> exact committed C18 facts."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

import httpx
import pytest
from daimon.core._models import TurnOutcome
from daimon.core.channel_backend import check_backend
from daimon.core.config import McpSettings
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.mux_backend import TurnRuntime
from daimon.core.pricing import ProviderPrice
from daimon.core.scope import DeploymentDefault, ResolvedConfig
from daimon.core.stores import turn_outcomes
from daimon.core.stores.mux_state import PostgresStateStore
from daimon.core.stores.turn_outcomes import OutcomeRecord
from daimon.core.turn import outcomes
from daimon.core.turn.admission import Admission
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.gemini import (
    PROFILE,
    GeminiDeployment,
    GeminiJournal,
    GeminiTransportRuntime,
    GeminiUsageRuntime,
)
from daimon.core.turn.prepare import PreparedTurn, bind_session
from daimon.core.turn.run import RunOutcome, run_prepared_turn
from daimon.core.turn.termination import TerminationReason
from daimon.testing import pytest_plugin as database_plugin
from daimon.testing.db import (
    DatabasePreflight,
    build_test_engine,
    preflight_test_database,
    require_owned_sessionmaker,
)
from daimon.testing.factories import make_tenant
from daimon.testing.gemini_termination import (
    REQUIRED_REASONS,
    UNREACHABLE_REASONS,
    GeminiTerminationEnvelope,
    GeminiTerminationTransport,
    compose_gemini_termination,
)
from daimon.testing.ma_models import ma_agent, ma_environment
from daimon.testing.turn_conformance import (
    TerminationCase,
    TerminationEvidence,
    TerminationSnapshot,
)
from daimon.testing.turn_fakes import RecordingLifecycle
from mux.contracts.ids import ModelRef, Scope
from mux.contracts.resources import AgentSpec, EnvironmentSpec, ToolSpec
from mux.drivers.gemini.flash import FLASH_PRIMARY
from mux.drivers.gemini.transport import Object, SDKTransport
from mux.errors import ScopeViolation
from mux.testing.conformance.gemini import adapter
from mux.testing.conformance.runner import PendingKind, run_fixture
from mux.testing.drivers.anthropic.ma_transport import ScriptedTransport
from mux.testing.drivers.gemini.fake import MemoryStorage
from mux.testing.isolated_sdk import SDK_ENV_VARS, build_gemini_client
from sqlalchemy import event as sql_event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .test_gemini_codec import interaction
from .test_gemini_preparation import ACCOUNT, CHANNEL, CONFIG, TENANT, Accounting

SCOPE = Scope(
    tenant_id=str(TENANT), account_id=str(ACCOUNT), principal_id="host", authorization_id="offline"
)


db_preflight = database_plugin.db_preflight


@pytest.fixture(scope="session", autouse=True)
def owned_database_preflight(db_preflight: DatabasePreflight) -> None:
    """Use shared URL/schema admission before any engine fixture."""


@pytest.fixture(autouse=True)
def clean_sdk_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in SDK_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


async def _silence() -> AsyncIterator[bytes]:
    await asyncio.Event().wait()
    yield b""


class NativeWire:
    """Docs-derived native replies; no normalized events/outcomes injected."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.status = "completed"
        self.get_status: int | None = None
        self.repeat_get_error = False
        self.usage: Object | None = None
        self.final_get_status: str | None = None
        self.retry_after_s: int | None = None
        self.saved: dict[str, Object] = {}
        # Accepted but never started: the stream stays silent, as under Gemini load.
        self.silent_stream = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "POST" and request.url.path.endswith("/interactions"):
            body = json.loads(request.content)
            assert body["agent_config"]["model"] == FLASH_PRIMARY
            assert body["tools"] == [
                {
                    "type": "function",
                    "name": "weather",
                    "description": "",
                    "parameters": {"type": "object", "properties": {}},
                }
            ]
            assert body["input"] == [{"type": "text", "text": "offline question"}]
            identity = f"offline-interaction-{len(self.saved) + 1}"
            raw = interaction(identity, status=self.status)
            if self.usage is not None:
                raw["usage"] = self.usage
            if self.silent_stream:
                # As Gemini reads back a run it accepted and never began (live probe).
                raw["steps"] = [{"type": "user_input"}]
                del raw["usage"]
            if self.status == "requires_action":
                raw["steps"] = [
                    {"type": "function_call", "id": "call-1", "name": "weather", "arguments": {}}
                ]
            self.saved[identity] = raw
            return httpx.Response(200, json=raw)
        if request.method == "POST" and request.url.path.endswith("/cancel"):
            identity = request.url.path.rsplit("/", 2)[-2]
            self.saved[identity]["status"] = "cancelled"
            return httpx.Response(200, json=self.saved[identity])
        if request.method == "GET":
            identity = request.url.path.rsplit("/", 1)[-1]
            assert identity in self.saved
            if self.get_status is not None:
                status = self.get_status
                if not self.repeat_get_error:
                    self.get_status = None
                return httpx.Response(
                    status,
                    headers={"retry-after": str(self.retry_after_s)}
                    if self.retry_after_s is not None
                    else {},
                    json={
                        "error": {
                            "message": "offline provider refusal",
                            "status": "RESOURCE_EXHAUSTED",
                        }
                    },
                )
            if request.url.params.get("stream") == "true":
                return httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    content=_silence() if self.silent_stream else b"",
                )
            if self.final_get_status is not None:
                self.saved[identity]["status"] = self.final_get_status
                self.saved[identity]["updated"] = datetime.now(UTC).isoformat()
            return httpx.Response(200, json=self.saved[identity])
        raise AssertionError("unexpected native Gemini C18 request")


@dataclass
class NativeHost:
    deps: TurnDeps
    admission: Admission
    journal: GeminiJournal
    ancillary: ScriptedTransport
    wire: NativeWire


@pytest.fixture
async def native_host(
    db_session_factory: async_sessionmaker[AsyncSession], db_clean: None
) -> AsyncIterator[NativeHost]:
    require_owned_sessionmaker(db_session_factory)
    async with db_session_factory.begin() as db:
        await make_tenant(db, id=TENANT)
    store = PostgresStateStore(db_session_factory)
    journal = GeminiJournal(
        MemoryStorage(),
        store,
        GeminiDeployment(
            config_digest=CONFIG.digest,
            config_revision=CONFIG.local,
            account_scope_id="offline-project",
            agent=AgentSpec(
                name="explicit",
                model=ModelRef(provider="gemini", id=FLASH_PRIMARY),
                tools=(
                    ToolSpec(
                        name="weather",
                        kind="custom",
                        permission="auto",
                        input_schema={"type": "object", "properties": {}},
                    ),
                ),
            ),
            environment=EnvironmentSpec(name="inline"),
        ),
    )
    wire = NativeWire()
    client = build_gemini_client(transport=httpx.MockTransport(wire.handle))
    transport = SDKTransport(client)
    ancillary = ScriptedTransport()
    try:
        async with ancillary.client() as anthropic:
            runtime = TurnRuntime(
                transport_factory=lambda config, scope: GeminiTransportRuntime(transport),
                journal=journal,
                usage_store=GeminiUsageRuntime(
                    Accounting(),
                    {
                        FLASH_PRIMARY: ProviderPrice(
                            provider="gemini",
                            model=FLASH_PRIMARY,
                            checked_on=date(2026, 10, 10),
                            input=Decimal("0.75"),
                            output=Decimal("3.75"),
                            cache_read=Decimal("0.075"),
                        )
                    },
                    Decimal(0),
                ),
            )
            deps = TurnDeps(
                anthropic=anthropic,
                sessionmaker=db_session_factory,
                deployment_default=DeploymentDefault(),
                resolver_cache=new_resolver_cache(),
                defaults_root=Path("/unused"),
                mcp=McpSettings(),
                billing_config=None,
                markup=Decimal(1),
                fernet=None,
                github_fallback_pat=None,
                github_app_id=None,
                github_app_private_key=None,
                public_url=None,
                turn_path="mux",
                channel_backends=True,
                turn_runtimes={PROFILE: runtime},
                state_store=store,
            )
            admission = Admission(
                account_id=ACCOUNT,
                agent=ma_agent(),
                environment=ma_environment(),
                config=ResolvedConfig(agent_name="configured", environment_name="configured"),
                backend_revision=CONFIG,
                backend_revision_origin="channel",
                backend=check_backend(CONFIG),
                origin_channel_id=CHANNEL.channel_id,
            )
            yield NativeHost(deps, admission, journal, ancillary, wire)
    finally:
        await client.aio.aclose()
        client.close()


async def prepare(native: NativeHost, *, thread: str = "thread") -> PreparedTurn:
    return await bind_session(
        native.deps,
        native.admission,
        tenant_id=TENANT,
        platform=CHANNEL.platform,
        external_user_id="user",
        thread_id=thread,
        session_account_id=ACCOUNT,
        reuse_existing=True,
    )


async def run(
    native: NativeHost,
    *,
    deadline: datetime | None = None,
    thread: str = "thread",
    operation_key: str = "offline-c18",
    lifecycle: RecordingLifecycle | None = None,
) -> RunOutcome:
    prepared = await prepare(native, thread=thread)

    async def reseed() -> str:
        raise AssertionError("Gemini native replay must not invoke Anthropic replacement")

    return await run_prepared_turn(
        native.deps,
        prepared,
        tenant_id=TENANT,
        platform=CHANNEL.platform,
        thread_id=thread,
        external_user_id="user",
        user_message="offline question",
        lifecycle=lifecycle or RecordingLifecycle(),
        cancel=asyncio.Event(),
        reseed_user_message=reseed,
        recovery_lifecycle=lambda cancel: RecordingLifecycle(),
        render_interval_s=3600,
        deadline=deadline,
        operation_key=operation_key,
    )


def assert_snapshot(row: TerminationSnapshot, expected: TerminationReason) -> None:
    assert len(row) == 21 and row["persisted_rows"] == 1
    assert row["expected_reason"] == row["actual_reason"] == row["persisted_reason"] == expected
    assert row["expected_session"] == row["actual_session"] == row["persisted_session"]
    assert row["expected_account"] == row["persisted_account"] == str(ACCOUNT)
    assert row["expected_tenant"] == row["persisted_tenant"] == str(TENANT)
    assert row["expected_platform"] == row["persisted_platform"] == CHANNEL.platform
    assert row["expected_error_class"] == row["actual_error_class"] == row["persisted_error_class"]
    assert (
        row["expected_recovered"] is row["actual_recovered"] is row["persisted_recovered"] is False
    )


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("completed", TerminationReason.COMPLETED),
        ("cancelled", TerminationReason.INTERRUPTED),
        ("failed", TerminationReason.UPSTREAM),
        ("incomplete", TerminationReason.SESSION_TERMINATED),
        ("budget_exceeded", TerminationReason.SESSION_TERMINATED),
        ("requires_action", TerminationReason.REQUIRES_ACTION),
    ],
)
async def test_native_final_outcome_has_exact_committed_snapshot(
    native_host: NativeHost,
    status: str,
    expected: TerminationReason,
    record_property: Callable[[str, object], None],
) -> None:
    prepared = await prepare(native_host)
    assert prepared.session_ref is not None
    native_host.wire.status = status
    envelope = GeminiTerminationEnvelope(
        native_host.deps.sessionmaker,
        SCOPE,
        CHANNEL.platform,
        (
            TerminationCase(
                status,
                expected,
                prepared.session_ref,
                lambda: run(native_host),
                expected_error_class="TurnError" if expected.is_failure else None,
            ),
        ),
    )
    composed = compose_gemini_termination(adapter(), termination=envelope)
    assert isinstance(composed.transport, GeminiTerminationTransport)
    hook = composed.transport.host_termination_evidence
    assert hook is not None
    facts = cast(TerminationEvidence, await hook())
    assert facts["required_reasons"] == tuple(reason.value for reason in REQUIRED_REASONS)
    assert len(envelope.missing_reasons) == 20
    assert_snapshot(facts["terminations"][0], expected)
    record_property("termination_evidence", json.dumps(facts))
    assert facts["terminations"][0]["persisted_session"]
    requests = native_host.wire.requests
    assert sum(r.method == "POST" for r in requests) == 1
    assert any(r.method == "GET" for r in requests)
    assert not native_host.ancillary.requests
    assert not {"pass", "verdict", "coverage_complete"}.intersection(facts)
    events = await native_host.journal.state_store.read_events(prepared.session_ref.id)
    assert any(e.type == "user.message" for e in events)
    assert any(e.type in ("session.turn_ended", "session.requires_action") for e in events)
    assert (await run_fixture("C18", composed)).status == "pending"
    assert composed.pending["C18"].kind == PendingKind.ADAPTER_DEPENDENCY
    assert isinstance(composed.transport, GeminiTerminationTransport)
    assert callable(composed.transport.host_termination_evidence)


async def test_a_turn_gemini_never_starts_frees_its_hold_at_the_deadline(
    native_host: NativeHost,
) -> None:
    native_host.wire.status = "in_progress"
    native_host.wire.silent_stream = True
    deadline = datetime.now(UTC) + timedelta(seconds=3)
    outcome = await asyncio.wait_for(run(native_host, deadline=deadline), timeout=60)
    assert outcome.state.termination is not None and outcome.state.termination.is_failure
    # The host gave up: the interaction is cancelled, as before.
    cancels = [r for r in native_host.wire.requests if r.url.path.endswith("/cancel")]
    assert len(cancels) == 1
    identity = cancels[0].url.path.rsplit("/", 2)[-2]
    assert native_host.wire.saved[identity]["status"] == "cancelled"
    # It never ran or billed: its hold is released now, not after the hold TTL (the
    # cancel's read-back and any later read both ask; the ledger release is idempotent).
    usage = native_host.deps.turn_runtimes[PROFILE].usage_store
    assert set(cast(Accounting, usage.accounting).released) == {identity}


async def test_public_binding_refusal_is_pre_native_and_committed(native_host: NativeHost) -> None:
    native_host.deps = replace(native_host.deps, turn_runtimes={})
    facts = await GeminiTerminationEnvelope(
        native_host.deps.sessionmaker,
        SCOPE,
        CHANNEL.platform,
        (
            TerminationCase(
                "no-runtime",
                TerminationReason.ADMISSION_DENIED,
                None,
                lambda: run(native_host),
                expected_error_class="AdmissionDenied",
            ),
        ),
    ).evidence()
    assert_snapshot(facts["terminations"][0], TerminationReason.ADMISSION_DENIED)
    assert facts["terminations"][0]["persisted_session"] is None
    assert not native_host.wire.requests and not native_host.ancillary.requests


async def test_get_rate_limit_uses_native_sdk_error_and_committed_host_reason(
    native_host: NativeHost,
) -> None:
    prepared = await prepare(native_host)
    assert prepared.session_ref is not None
    native_host.wire.status = "in_progress"
    native_host.wire.get_status = 429
    facts = await GeminiTerminationEnvelope(
        native_host.deps.sessionmaker,
        SCOPE,
        CHANNEL.platform,
        (
            TerminationCase(
                "rate-limited",
                TerminationReason.RATE_LIMITED,
                prepared.session_ref,
                lambda: run(native_host),
                expected_error_class="TurnError",
            ),
        ),
    ).evidence()
    assert_snapshot(facts["terminations"][0], TerminationReason.RATE_LIMITED)
    assert sum(r.method == "POST" for r in native_host.wire.requests) == 1


async def test_expired_public_deadline_keeps_session_and_exact_failure(
    native_host: NativeHost,
) -> None:
    prepared = await prepare(native_host)
    assert prepared.session_ref is not None
    facts = await GeminiTerminationEnvelope(
        native_host.deps.sessionmaker,
        SCOPE,
        CHANNEL.platform,
        (
            TerminationCase(
                "ceiling",
                TerminationReason.CEILING,
                prepared.session_ref,
                lambda: run(native_host, deadline=datetime.now(UTC) - timedelta(seconds=1)),
                expected_error_class="TurnError",
            ),
        ),
    ).evidence()
    assert_snapshot(facts["terminations"][0], TerminationReason.CEILING)
    assert not native_host.wire.requests


async def test_reused_host_session_has_distinct_native_roots_and_outcome_rows(
    native_host: NativeHost,
) -> None:
    prepared = await prepare(native_host)
    assert prepared.session_ref is not None
    first = TerminationCase(
        "first",
        TerminationReason.COMPLETED,
        prepared.session_ref,
        lambda: run(native_host, operation_key="c18:first"),
    )
    second = TerminationCase(
        "second",
        TerminationReason.COMPLETED,
        prepared.session_ref,
        lambda: run(native_host, operation_key="c18:second"),
    )
    envelope = GeminiTerminationEnvelope(
        native_host.deps.sessionmaker, SCOPE, CHANNEL.platform, (first, second)
    )
    facts = await envelope.evidence()
    assert len(facts["terminations"]) == 2
    assert len({row["outcome_id"] for row in facts["terminations"]}) == 2
    for row in facts["terminations"]:
        assert_snapshot(row, TerminationReason.COMPLETED)
    posts = [json.loads(req.content) for req in native_host.wire.requests if req.method == "POST"]
    assert len(posts) == 2 and "previous_interaction_id" not in posts[0]
    assert posts[1]["previous_interaction_id"] == "offline-interaction-1"
    events = await native_host.journal.state_store.read_events(prepared.session_ref.id)
    roots = [e for e in events if e.type == "session.turn_ended"]
    assert len(roots) == 2 and len({e.turn_id for e in roots}) == 2
    assert len({e.native.event_id for e in roots}) == 2
    assert len([e for e in events if e.type == "agent.message"]) == 2
    assert len([e for e in events if e.type == "usage.observed"]) == 2
    assert len(envelope.missing_reasons) == 20


async def test_repeated_usage_get_error_preserves_terminal_failure_and_pending(
    native_host: NativeHost,
) -> None:
    # A failed final meter fetch must not replace the original failure. The
    # incomplete reason inventory still keeps the composed C18 verdict pending.
    prepared = await prepare(native_host)
    assert prepared.session_ref is not None
    native_host.wire.status = "in_progress"
    native_host.wire.get_status = 429
    native_host.wire.repeat_get_error = True
    envelope = GeminiTerminationEnvelope(
        native_host.deps.sessionmaker,
        SCOPE,
        CHANNEL.platform,
        (
            TerminationCase(
                "repeated-429",
                TerminationReason.RATE_LIMITED,
                prepared.session_ref,
                lambda: run(native_host),
                expected_error_class="TurnError",
            ),
        ),
    )
    facts = await envelope.evidence()
    row = facts["terminations"][0]
    assert row["expected_reason"] == "rate_limited"
    assert row["actual_reason"] == row["persisted_reason"] == "rate_limited"
    assert row["actual_error_class"] == row["persisted_error_class"] == "TurnError"
    assert row["persisted_rows"] == 1 and row["persisted_session"] == prepared.ma_session_id
    assert (
        await run_fixture("C18", compose_gemini_termination(adapter(), termination=envelope))
    ).status == "pending"


@pytest.mark.parametrize("corruption", ["missing", "session", "error", "recovery"])
async def test_missing_or_corrupted_committed_native_facts_are_never_repaired(
    native_host: NativeHost,
    monkeypatch: pytest.MonkeyPatch,
    corruption: str,
) -> None:
    original = turn_outcomes.record

    async def faulty_write(db: AsyncSession, row: OutcomeRecord) -> None:
        if corruption == "missing":
            return
        if corruption == "session":
            row = replace(row, session_id="wrong-session")
        elif corruption == "error":
            row = replace(row, error_class="WrongError")
        else:
            row = replace(row, recovered=True)
        await original(db, row)

    monkeypatch.setattr(outcomes, "record", faulty_write)
    prepared = await prepare(native_host)
    assert prepared.session_ref is not None
    envelope = GeminiTerminationEnvelope(
        native_host.deps.sessionmaker,
        SCOPE,
        CHANNEL.platform,
        (
            TerminationCase(
                "corrupt-row",
                TerminationReason.COMPLETED,
                prepared.session_ref,
                lambda: run(native_host),
            ),
        ),
    )
    facts = await envelope.evidence()
    row = facts["terminations"][0]
    assert row["actual_reason"] == row["expected_reason"] == "completed"
    if corruption == "missing":
        assert row["persisted_rows"] == 0 and row["persisted_reason"] is None
        assert row["persisted_session"] is row["persisted_account"] is None
    elif corruption == "session":
        assert row["persisted_session"] == "wrong-session" != row["actual_session"]
    elif corruption == "error":
        assert row["actual_error_class"] is None and row["persisted_error_class"] == "WrongError"
    else:
        assert row["actual_recovered"] is False and row["persisted_recovered"] is True
    assert (
        await run_fixture("C18", compose_gemini_termination(adapter(), termination=envelope))
    ).status == "pending"


def test_inventory_is_complete_independent_and_default_registration_unchanged() -> None:
    assert len(REQUIRED_REASONS) == len(set(REQUIRED_REASONS)) == 21
    assert set(REQUIRED_REASONS).isdisjoint(UNREACHABLE_REASONS)
    assert set(REQUIRED_REASONS) | UNREACHABLE_REASONS == set(TerminationReason)
    original = adapter()
    composed = compose_gemini_termination(original)
    assert composed.driver is original.driver and composed.store is original.store
    assert composed.pending == original.pending
    assert isinstance(composed.transport, GeminiTerminationTransport)
    assert composed.transport.host_termination_evidence is None


@pytest.mark.parametrize("change", ["provider", "tenant_id", "account_id", "kind"])
async def test_foreign_expected_identity_refused_before_callback_or_io(
    native_host: NativeHost, change: str
) -> None:
    prepared = await prepare(native_host)
    assert prepared.session_ref is not None
    ref = prepared.session_ref.model_copy(update={change: "foreign"})
    with pytest.raises(ScopeViolation):
        GeminiTerminationEnvelope(
            native_host.deps.sessionmaker,
            SCOPE,
            CHANNEL.platform,
            (
                TerminationCase(
                    "foreign", TerminationReason.COMPLETED, ref, lambda: run(native_host)
                ),
            ),
        )
    assert not native_host.wire.requests


@pytest.mark.parametrize(
    "reason", [TerminationReason.COMPLETED, TerminationReason.SESSION_TERMINATED]
)
def test_post_native_reason_cannot_claim_null_session(
    native_host: NativeHost, reason: TerminationReason
) -> None:
    with pytest.raises(ValueError, match="must retain"):
        GeminiTerminationEnvelope(
            native_host.deps.sessionmaker,
            SCOPE,
            CHANNEL.platform,
            (TerminationCase("null", reason, None, lambda: run(native_host)),),
        )


@pytest.mark.parametrize(
    "url,skipped",
    [
        ("postgresql+asyncpg://offline:fixture@foreign.invalid/daimon_test_nc_n12", False),
        ("postgresql+asyncpg://offline:fixture@localhost/daimon_test", True),
    ],
)
def test_database_preflight_precedes_connections(tmp_path: Path, url: str, skipped: bool) -> None:
    trap = tmp_path / "connected"
    plugin = tmp_path / "gemini_c18_connection_trap.py"
    plugin.write_text(
        "from pathlib import Path\nfrom sqlalchemy import event\nfrom sqlalchemy.engine import Engine\n"
        "def pytest_sessionstart(session):\n"
        "    def refuse(*args, **kwargs):\n"
        f"        Path({str(trap)!r}).touch()\n"
        "        raise RuntimeError('connection before ownership')\n"
        "    event.listen(Engine, 'do_connect', refuse)\n"
    )
    env = dict(os.environ)
    env.pop("GITHUB_ACTIONS", None)
    env.pop("CI", None)
    env["DAIMON_DATABASE__TEST_URL"] = url
    env["PYTHONPATH"] = os.pathsep.join((str(tmp_path), env.get("PYTHONPATH", "")))
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-o",
            "addopts=",
            "-n",
            "0",
            "-p",
            "daimon_db_preflight",
            "-p",
            "gemini_c18_connection_trap",
            f"{Path(__file__).resolve()}::test_public_binding_refusal_is_pre_native_and_committed",
            "-q",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert not trap.exists(), result.stdout + result.stderr
    assert result.returncode == (0 if skipped else 1), result.stdout + result.stderr
    assert ("1 skipped" if skipped else "owned local lane engines") in result.stdout


@pytest.mark.parametrize("registered", [True, False])
def test_registered_isolated_ci_is_the_only_shared_name_exception(
    monkeypatch: pytest.MonkeyPatch, registered: bool
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("CI", "true")
    url = "postgresql+asyncpg://offline:fixture@localhost/daimon_test"
    monkeypatch.setenv("DAIMON_DATABASE__TEST_URL", url)
    schema = f"test_w{os.getpid()}_1234abcd" if registered else "unowned_schema"
    if registered:
        preflight_test_database(schema)
        require_owned_sessionmaker(build_test_engine(url, schema))
    else:
        with pytest.raises(ValueError, match="owned local lane engines"):
            preflight_test_database(schema)


@pytest.mark.parametrize("routing", ["primary", "mutable_route"])
async def test_actual_host_callback_cannot_reroute_queued_writes_or_evidence_reads(
    native_host: NativeHost, routing: str
) -> None:
    prepared = await prepare(native_host)
    session_ref = prepared.session_ref
    assert session_ref is not None
    sessions = native_host.deps.sessionmaker
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

    async def execute() -> RunOutcome:
        result = await run(native_host, operation_key="c18-callback-routing-" + routing)
        results.append(result)
        if routing == "primary":
            sessions.configure(bind=foreign)
        else:
            routes[TurnOutcome] = foreign
        return result

    try:
        facts = await GeminiTerminationEnvelope(
            sessions,
            SCOPE,
            CHANNEL.platform,
            (
                TerminationCase(
                    "actual-completed", TerminationReason.COMPLETED, session_ref, execute
                ),
            ),
        ).evidence()
        assert len(native_host.wire.saved) == len(results) == 1
        assert results[0].state.termination is TerminationReason.COMPLETED
        assert not crossings
        row = facts["terminations"][0]
        assert_snapshot(row, TerminationReason.COMPLETED)
        assert row["actual_session"] == row["persisted_session"] == session_ref.id
        assert sum(r.method == "POST" for r in native_host.wire.requests) == 1
        assert any(r.method == "GET" for r in native_host.wire.requests)
    finally:
        sessions.kw.clear()
        sessions.kw.update(original)
        await outcomes.drain_outcomes()
        await foreign.dispose()


@pytest.mark.parametrize("routing", ["primary", "mutable_route"])
async def test_reconfigured_source_is_readmitted_before_any_callback(
    native_host: NativeHost, routing: str
) -> None:
    prepared = await prepare(native_host)
    assert prepared.session_ref is not None
    sessions = native_host.deps.sessionmaker
    original = dict(sessions.kw)
    foreign = create_async_engine(
        "postgresql+asyncpg://unused:unused@127.0.0.1:1/daimon_test_foreign_probe"
    )
    crossings: list[bool] = []
    executions: list[bool] = []

    def trap(*args: object, **kwargs: object) -> None:
        crossings.append(True)
        raise RuntimeError("reconfigured C18 source reached a foreign connection")

    sql_event.listen(foreign.sync_engine, "do_connect", trap)

    async def never_run() -> RunOutcome:
        executions.append(True)
        raise AssertionError("unowned factory admitted callback")

    envelope = GeminiTerminationEnvelope(
        sessions,
        SCOPE,
        CHANNEL.platform,
        (TerminationCase("refused", TerminationReason.COMPLETED, prepared.session_ref, never_run),),
    )
    try:
        if routing == "primary":
            sessions.configure(bind=foreign)
        else:
            sessions.configure(binds={TurnOutcome: foreign})
        with pytest.raises(ValueError, match="owned local lane engines"):
            await envelope.evidence()
        assert not executions and not crossings and not native_host.wire.requests
    finally:
        sessions.kw.clear()
        sessions.kw.update(original)
        await foreign.dispose()
