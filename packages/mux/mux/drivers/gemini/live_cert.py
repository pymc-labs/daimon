"""Explicit Gemini probe preparation; importing this module never opens a client."""

from __future__ import annotations

import asyncio
import json
import os
import stat
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from pydantic import JsonValue, TypeAdapter

from mux.conformance.budget import (
    BudgetConfig,
    BudgetGuard,
    BudgetRefused,
    ProbePlan,
    TokenLimits,
    TokenUsage,
)
from mux.conformance.gemini import PENDING_REASONS, GeminiScript
from mux.conformance.live_probe import ProbeOutcome, ProbeRun, run_probe
from mux.conformance.recording import Recorder, RequestMetadata
from mux.conformance.runner import Adapter, PendingKind, PendingReason, Result, run_fixture
from mux.contracts.actions import UserMessage
from mux.contracts.events import AgentMessagePayload, Event, TextPart
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ChannelRef, ModelRef, PageRequest, Scope, ThreadRef
from mux.contracts.resources import AgentSpec, EnvironmentSpec, SessionFilter, SessionSpec
from mux.drivers.gemini import GeminiManagedAgents
from mux.drivers.gemini.fake import MemoryStorage
from mux.drivers.gemini.transport import Object, Transport, object_value
from mux.errors import ProviderError
from mux.state.memory import MemoryStateStore

GEMINI_CAP = Decimal("30")
GEMINI_STOP = Decimal("24")
# Official offline runtime model table, source-G-runtime.txt:735-749.
# Live probes pin the cost-focused Flash-Lite tier; no default/fallback model.
LIVE_MODEL = "gemini-3.5-flash-lite"
SHARED_SPEND_PATH = Path("/home/clsandoval/cs/daimon-neutral-core-20261009/lanes/N9-qa/spend.md")
KEY_PATH = Path.home() / ".config/daimon-nc/gemini.env"
SCOPE = Scope(
    tenant_id="gemini-cert",
    account_id="gemini-cert",
    principal_id="probe",
    authorization_id="explicit-live-probe",
)


@dataclass(frozen=True)
class SmokeSettings:
    model: str
    # This is a financial reservation, not a claimed provider-enforced cap.
    limits: TokenLimits = TokenLimits(input_tokens=32768, output_tokens=32768)
    max_total_tokens: int = 4096
    timeout_s: float = 120.0
    poll_s: float = 0.5

    def __post_init__(self) -> None:
        if not self.model or not 1 <= self.max_total_tokens <= 4096:
            raise ValueError("an explicit model and a small provider token budget are required")
        if not 0 < self.timeout_s <= 120 or not 0 <= self.poll_s <= 5:
            raise ValueError("invalid probe deadline or polling interval")
        if min(self.limits.input_tokens, self.limits.output_tokens) < self.max_total_tokens:
            raise ValueError("reservation must cover the requested provider token budget")


def validate_budget(path: Path, settings: SmokeSettings) -> BudgetConfig:
    config = BudgetConfig.load(path)
    budget = config.providers.get("gemini")
    if budget is None or budget.cap_usd != GEMINI_CAP or config.total_cap_usd != Decimal("150"):
        raise BudgetRefused("Gemini probe requires the approved $30 line and $24 stop")
    if settings.model not in budget.models:
        raise BudgetRefused("explicit Gemini model needs reviewed prices before live admission")
    return config


def validate_shared_budget(path: Path, settings: SmokeSettings) -> BudgetConfig:
    if settings.model != LIVE_MODEL:
        raise BudgetRefused("live Gemini probes require gemini-3.5-flash-lite only")
    config = validate_budget(path, settings)
    if config.ledger_path.resolve() != SHARED_SPEND_PATH:
        raise BudgetRefused("live Gemini probes require the approved N9 shared spend ledger")
    return config


def read_key(path: Path = KEY_PATH) -> str:
    """Closed key-file schema; no environment interpolation or ambient fallback."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "r", encoding="utf-8") as file:
            info = os.fstat(file.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.getuid()
            ):
                raise ValueError("unsafe key file")
            if info.st_size > 4096:
                raise ValueError("oversized key file")
            lines = [
                line.strip() for line in file if line.strip() and not line.lstrip().startswith("#")
            ]
        if len(lines) != 1:
            raise ValueError("unexpected key file schema")
        name, sep, key = lines[0].removeprefix("export ").partition("=")
        if name != "GEMINI_API_KEY" or sep != "=":
            raise ValueError("unexpected key file schema")
        key = key.strip()
        if len(key) >= 2 and key[0] == key[-1] and key[0] in "\"'":
            key = key[1:-1]
        if not key or any(c.isspace() for c in key) or any(c in key for c in "$`\\"):
            raise ValueError("invalid key")
        return key
    except (OSError, ValueError, UnicodeError):
        raise BudgetRefused("Gemini key file is absent or invalid") from None


class LimitedTransport:
    """One native POST only; no retries or continuation hidden in the probe."""

    def __init__(self, source: Transport, max_total_tokens: int) -> None:
        self.source = source
        self.max_total_tokens = max_total_tokens
        self.sent = False
        self.accepted_id: str | None = None

    async def create(self, request: Mapping[str, JsonValue]) -> Object:
        if self.sent:
            raise ProviderError("invalid_request", retryable=False, native_code="probe_send_limit")
        self.sent = True
        bounded = dict(request)
        agent = dict(object_value(bounded["agent_config"]))
        agent["max_total_tokens"] = self.max_total_tokens
        bounded["agent_config"] = agent
        response = await self.source.create(bounded)
        id_ = response.get("id")
        self.accepted_id = id_ if isinstance(id_, str) else None
        return response

    async def get(self, interaction_id: str) -> Object:
        return await self.source.get(interaction_id)

    async def cancel(self, interaction_id: str) -> Object:
        return await self.source.cancel(interaction_id)

    async def download_snapshot(self, environment_id: str) -> bytes:
        return await self.source.download_snapshot(environment_id)

    async def open_stream(
        self, interaction_id: str, *, after: str | None = None
    ) -> AsyncIterator[Object]:
        return await self.source.open_stream(interaction_id, after=after)


async def smoke(
    source: Transport,
    guard: BudgetGuard,
    output: Path,
    settings: SmokeSettings,
    *,
    secrets: tuple[str, ...] = (),
    request_metadata: list[RequestMetadata] | None = None,
) -> ProbeRun:
    """C07 is a budget/evidence tag. This narrow smoke does not pass live C07."""
    validate_budget(guard.config_path, settings)
    bounded = LimitedTransport(source, settings.max_total_tokens)

    def record(recorder: Recorder, events: tuple[Event, ...]) -> None:
        if request_metadata:
            for metadata in request_metadata[:-1]:
                recorder.record(metadata, ())
            recorder.record(request_metadata[-1], events)
        else:
            recorder.record(RequestMetadata(method="GET", path="/host/smoke/events"), events)

    async def invoke(recorder: Recorder) -> ProbeOutcome:
        ma = GeminiManagedAgents(
            bounded,
            storage=MemoryStorage(),
            state_store=MemoryStateStore(),
            account_scope_id="gemini-cert",
        )
        terminal = False
        try:
            async with asyncio.timeout(settings.timeout_s):
                a = await ma.agents.create(
                    SCOPE,
                    AgentSpec(name="smoke", model=ModelRef(provider="gemini", id=settings.model)),
                    key="smoke-agent",
                )
                env = await ma.environments.create(
                    SCOPE, EnvironmentSpec(name="smoke"), key="smoke-env"
                )
                thread = ThreadRef(
                    channel=ChannelRef(
                        tenant_id=SCOPE.tenant_id, platform="test", channel_id="smoke"
                    ),
                    thread_id="smoke",
                )
                s = await ma.sessions.create(
                    SCOPE,
                    SessionSpec(
                        agent=a.ref,
                        agent_revision=a.revision,
                        environment=env.ref,
                        config_revision=1,
                        extensions={
                            "gemini.session": ExtensionConfig(
                                namespace="gemini.session",
                                version=1,
                                value={
                                    "binding_id": "smoke",
                                    "thread": thread.model_dump(mode="json"),
                                },
                            )
                        },
                    ),
                    key="smoke-session",
                )
                sent = await ma.events.send(
                    SCOPE,
                    s.ref,
                    (
                        UserMessage(
                            content=(
                                TextPart(
                                    text="Reply with exactly: GEMINI_SMOKE_OK. Do not use tools."
                                ),
                            )
                        ),
                    ),
                    key="smoke-send",
                )
                if sent.status not in ("queued", "processed"):
                    raise ProviderError(
                        "upstream", retryable=False, native_code="unknown_smoke_send"
                    )
                while True:
                    projection = await ma.events.reconcile(SCOPE, s.ref)
                    measured = await ma.usage.reconcile(SCOPE, s.ref)
                    if len(measured) != 1:
                        raise ProviderError(
                            "upstream", retryable=False, native_code="smoke_meter_count"
                        )
                    meter = measured[0]
                    tokens = TokenUsage(
                        input_tokens=meter.input_tokens,
                        output_tokens=meter.output_tokens,
                        input_cached_tokens=meter.input_cached_tokens,
                    )
                    if (
                        tokens.minimum_input_tokens > settings.limits.input_tokens
                        or (tokens.output_tokens or 0) > settings.limits.output_tokens
                    ):
                        journal = await ma.events.list(SCOPE, s.ref, page=PageRequest(limit=100))
                        record(recorder, journal.data)
                        return ProbeOutcome(
                            usage=tokens,
                            result=Result(
                                "C07",
                                "fail",
                                (
                                    "Provider exceeded the financial reservation; "
                                    "subsequent admission blocked.",
                                ),
                            ),
                        )
                    if projection.state == "idle":
                        terminal = True
                        break
                    if projection.state in ("requires_action", "terminated"):
                        raise ProviderError(
                            "upstream", retryable=False, native_code="smoke_not_completed"
                        )
                    await asyncio.sleep(settings.poll_s)
                journal = await ma.events.list(SCOPE, s.ref, page=PageRequest(limit=100))
                if journal.has_more:
                    raise ProviderError(
                        "upstream", retryable=False, native_code="smoke_journal_limit"
                    )
                ends = [e for e in journal.data if e.type == "session.turn_ended"]
                messages = [e for e in journal.data if e.type == "agent.message"]
                if len(ends) != 1 or ends[0].payload.get("outcome") != "completed" or not messages:
                    raise ProviderError(
                        "upstream", retryable=False, native_code="smoke_incomplete_evidence"
                    )
                texts = [
                    part.text
                    for e in messages
                    if isinstance(payload := e.typed_payload(), AgentMessagePayload)
                    for part in payload.content
                    if isinstance(part, TextPart)
                ]
                if texts != ["GEMINI_SMOKE_OK"]:
                    raise ProviderError(
                        "upstream", retryable=False, native_code="smoke_reply_mismatch"
                    )
                record(recorder, journal.data)
                result = Result(
                    "C07",
                    "pending",
                    ("Narrow SDK smoke completed; no live C07 certificate.",),
                    PendingReason(
                        PendingKind.ADAPTER_DEPENDENCY,
                        "Live usage revision/correction fixture has not been executed.",
                    ),
                )
                return ProbeOutcome(
                    usage=TokenUsage(
                        input_tokens=meter.input_tokens,
                        output_tokens=meter.output_tokens,
                        input_cached_tokens=meter.input_cached_tokens,
                    ),
                    result=result,
                )
        finally:
            if bounded.accepted_id is not None and not terminal:
                # Best effort, bounded; an unobserved acceptance remains charged
                # and cannot be claimed cancelled when its ID was never returned.
                try:
                    async with asyncio.timeout(5):
                        await bounded.cancel(bounded.accepted_id)
                except (ProviderError, TimeoutError):
                    pass

    plan = ProbePlan(
        provider="gemini", model=settings.model, fixture_id="C07", limits=settings.limits
    )
    return await run_probe(guard, plan, output, invoke, secrets=secrets)


async def record_offline_matrix(
    output: Path, *, model: str = "fixture"
) -> list[dict[str, JsonValue]]:
    """Record actual driver journal facts; offline evidence never becomes live evidence."""
    rows: list[dict[str, JsonValue]] = []
    for index in range(1, 19):
        fid = f"C{index:02}"
        script = GeminiScript()
        result = await run_fixture(
            fid, Adapter(script.ma, script.store, script, pending=PENDING_REASONS)
        )
        tape: str | None = None
        if result.status == "pass":
            recorder = Recorder()
            sessions = await script.ma.sessions.list(
                script.scope, filters=SessionFilter(), page=PageRequest()
            )
            for session in sessions.data:
                events: list[Event] = []
                cursor = None
                while True:
                    page = await script.ma.events.list(
                        script.scope, session.ref, page=PageRequest(cursor=cursor, limit=100)
                    )
                    events.extend(page.data)
                    cursor = page.next_cursor
                    if cursor is None:
                        break
                recorder.record(
                    RequestMetadata(method="GET", path="/host/conformance/events"), events
                )
            tape = f"offline-{fid}.json"
            recorder.save(
                output / tape, fixture_id=fid, provider="gemini", model=model, complete=True
            )
        rows.append(
            {
                "fixture_id": fid,
                "status": result.status,
                "origin": "offline_driver",
                "evidence": list(result.evidence),
                "recording": tape,
                "pending_kind": result.pending_reason.kind.value if result.pending_reason else None,
                "pending_detail": result.pending_reason.detail if result.pending_reason else None,
            }
        )
    return rows


def write_report(path: Path, value: Mapping[str, JsonValue]) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        json.dump(value, file, indent=2)
        file.write("\n")


async def run_sdk_smoke(
    guard: BudgetGuard,
    output: Path,
    settings: SmokeSettings,
    *,
    key: str,
    mock: bool,
) -> ProbeRun:
    if not mock:
        validate_shared_budget(guard.config_path, settings)
        if guard.spend_path.resolve() != SHARED_SPEND_PATH:
            raise BudgetRefused("live Gemini probes require the approved N9 shared spend ledger")
    # The SDK stays private to this driver. Vertex/environment routing is
    # explicitly disabled; a mock command never reads a provider credential.
    import httpx
    from google import genai
    from google.genai.types import HttpOptions, HttpRetryOptions

    from mux.drivers.gemini.transport import API_REVISION, SDKTransport

    metadata: list[RequestMetadata] = []

    async def capture(request: httpx.Request) -> None:
        body: dict[str, object] | None = None
        if request.method == "POST":
            body = dict(TypeAdapter(dict[str, JsonValue]).validate_json(request.content))
        metadata.append(
            RequestMetadata.from_request(
                request.method, str(request.url), headers=request.headers, body=body
            )
        )

    def respond(request: httpx.Request) -> httpx.Response:
        from datetime import UTC, datetime

        if request.url.host != "generativelanguage.googleapis.com":
            raise ValueError("mock probe used an unexpected provider endpoint")
        if request.headers.get("Api-Revision") != API_REVISION:
            raise ValueError("mock probe omitted the pinned API revision")
        stamp = datetime.now(UTC).isoformat()
        if request.method == "POST":
            body = json.loads(request.content)
            if body["agent_config"].get("model") != settings.model:
                raise ValueError("explicit probe model missing")
            if body["agent_config"].get("max_total_tokens") != settings.max_total_tokens:
                raise ValueError("provider token budget missing")
        return httpx.Response(
            200,
            json={
                "id": "smoke-root",
                "status": "completed",
                "created": stamp,
                "updated": stamp,
                "environment_id": "smoke-workspace",
                "steps": [
                    {
                        "type": "model_output",
                        "content": [{"type": "text", "text": "GEMINI_SMOKE_OK"}],
                    }
                ],
                "usage": {
                    "total_input_tokens": 64,
                    "total_cached_tokens": 0,
                    "total_output_tokens": 8,
                    "total_thought_tokens": 0,
                },
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond) if mock else None,
        trust_env=False,
        timeout=settings.timeout_s,
        event_hooks={"request": [capture]},
    ) as http:
        client = genai.Client(
            api_key=key,
            vertexai=False,
            http_options=HttpOptions(
                base_url="https://generativelanguage.googleapis.com",
                httpx_async_client=http,
                retry_options=HttpRetryOptions(attempts=1),
            ),
        )
        try:
            return await smoke(
                SDKTransport(client),
                guard,
                output,
                settings,
                secrets=(key,),
                request_metadata=metadata,
            )
        finally:
            await client.aio.aclose()
            client.close()


async def run_prepared(
    output: Path,
    settings: SmokeSettings,
    *,
    live: bool = False,
    budget_path: Path | None = None,
) -> Path:
    """Explicit live switch only. No watcher, credentials discovery or host activation."""
    if not output.is_dir() or any(output.iterdir()):
        raise ValueError("probe output must be an existing empty directory")
    if live:
        if budget_path is None:
            raise BudgetRefused("live probes require the reviewed budget config")
        config = validate_shared_budget(budget_path, settings)
        guard = BudgetGuard(budget_path, config.ledger_path)
        key = read_key()
    else:
        # Synthetic prices and a private ledger; never touches the shared ledger.
        budget_path = output / "mock-budget.json"
        ledger = output / "mock-spend.md"
        write_report(
            budget_path,
            {
                "version": 1,
                "total_cap_usd": "150",
                "ledger_path": str(ledger),
                "providers": {
                    "gemini": {
                        "cap_usd": "30",
                        "models": {
                            settings.model: {
                                "input": "1",
                                "cached_input": "1",
                                "cache_write_input": "1",
                                "output": "4",
                            }
                        },
                    }
                },
            },
        )
        BudgetGuard.initialize(budget_path, ledger)
        guard = BudgetGuard(budget_path, ledger)
        key = "offline-fixture"
    probe = await run_sdk_smoke(guard, output / "smoke-C07.json", settings, key=key, mock=not live)
    rows = await record_offline_matrix(output)
    report = output / "report.json"
    write_report(
        report,
        {
            "version": 1,
            "mode": "live_smoke" if live else "mock_transport",
            "model": settings.model,
            "gemini_cap_usd": "30",
            "gemini_stop_usd": "24",
            "smoke_status": "completed",
            "smoke_recording": probe.recording.name,
            "smoke_receipt": probe.receipt.model_dump(mode="json"),
            "live_c07_status": probe.result.status,
            "matrix_origin": "offline_driver",
            "matrix": [row for row in rows],
            "certified": False,
        },
    )
    return report


def main() -> int:
    import argparse

    from mux.conformance.budget import BudgetLedgerError
    from mux.conformance.live_probe import ProbeRunError
    from mux.conformance.recording import RecordingError

    parser = argparse.ArgumentParser(description="Gemini smoke and recorded offline C-matrix")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument(
        "--live", action="store_true", help="requires lead authorization and key file"
    )
    parser.add_argument(
        "--budget", type=Path, help="reviewed shared-ledger config, required for --live"
    )
    args = parser.parse_args()
    try:
        report = asyncio.run(
            run_prepared(
                args.output.resolve(),
                SmokeSettings(model=args.model),
                live=args.live,
                budget_path=args.budget,
            )
        )
    except (
        BudgetRefused,
        BudgetLedgerError,
        ProbeRunError,
        RecordingError,
        ProviderError,
        OSError,
        ValueError,
    ) as error:
        print(f"Gemini probe refused or failed ({type(error).__name__}); no certificate.")
        return 1
    print(f"Gemini probe report: {report}; no full live certificate.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
