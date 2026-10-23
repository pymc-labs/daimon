"""Explicit one-attempt C06/C04 runner on a separately signed five-dollar line.

No import-time credentials, provider selection or network. The lead pins both
the financial envelope and the separate GO bytes; admission and reservation
precede key access. Offline execution requires an actual MockTransport.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, model_validator

from mux.contracts.actions import InputEvent, UserMessage
from mux.contracts.events import Event, TextPart
from mux.contracts.ids import PageRequest, ResourceRef, Scope
from mux.contracts.receipts import SendReceipt
from mux.drivers.gemini import GeminiManagedAgents
from mux.drivers.gemini.core import BASE_AGENT
from mux.drivers.gemini.deployment import (
    _require_clean_environment,  # pyright: ignore[reportPrivateUsage]
)
from mux.drivers.gemini.transport import (
    API_REVISION,
    NO_TOOLS_TOOL,
    Object,
    SDKTransport,
    provider_call,
)
from mux.testing.conformance.budget import (
    BudgetConfig,
    BudgetGuard,
    BudgetRefused,
    ProbePlan,
    TokenLimits,
)
from mux.testing.conformance.recording import Recorder, RequestMetadata
from mux.testing.conformance.runner import Result
from mux.testing.drivers.gemini.live_cert import EvidenceAliases, read_key, write_report
from mux.testing.drivers.gemini.round_cases import (
    INPUT_MAX,
    MODEL,
    OUTPUT_MAX,
    PRICE_SOURCE,
    case_ceiling,
    run_c04,
    run_c06,
)
from mux.testing.drivers.gemini.round_usage import (
    COUNTERS,
    NativeRoute,
    UsageBook,
    counter_metadata,
    timestamp,
)

type Case = Literal["C06", "C04"]
COUNTS: dict[Case, int] = {"C06": 1, "C04": 3}
ROUND_LEDGER = Path(
    "/home/clsandoval/cs/daimon-neutral-core-20261009/lanes/N9-qa/"
    "spend-gemini-certg-round-20261012.md"
)
REPO = Path(__file__).resolve().parents[5]


class Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    case: Case
    model: Literal["gemini-3.8-flash"]
    source_head: str = Field(pattern=r"^[a-f0-9]{40}$")
    config_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    ledger_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    scope: Scope
    interactions: int = Field(strict=True, ge=1, le=3)
    input_tokens_per_interaction: Literal[1_048_576]
    output_tokens_per_interaction: Literal[65_536]
    tools: tuple[()] = ()
    runtime_max_usd: Decimal = Decimal(0)
    maximum_usd: Decimal
    deadline_seconds: float = Field(ge=30, le=120)
    expires_at: datetime

    @model_validator(mode="after")
    def bounded(self) -> Envelope:
        if (
            self.interactions != COUNTS[self.case]
            or self.runtime_max_usd != 0
            or self.maximum_usd != case_ceiling(self.interactions)
            or self.scope.is_platform
            or self.scope.is_legacy_host_authorized
            or self.expires_at.tzinfo is None
        ):
            raise ValueError("case differs from the reviewed model-limit/tool-free recipe")
        return self

    @property
    def limits(self) -> TokenLimits:
        return TokenLimits(
            input_tokens=INPUT_MAX * self.interactions,
            output_tokens=OUTPUT_MAX * self.interactions,
        )


@dataclass(frozen=True)
class Admission:
    envelope: Envelope
    config: Path
    output: Path
    envelope_path: Path | None = None
    go_path: Path | None = None
    envelope_sha256: str | None = None
    go_sha256: str | None = None


def admit(
    envelope_path: Path,
    go_path: Path,
    *,
    envelope_sha256: str,
    go_sha256: str,
    config: Path,
    output: Path,
) -> Admission:
    """Trusted launch pins are supplied by the lead, never read from the GO itself."""
    _require_clean_environment()  # Names only, before reads or allocation.
    envelope_bytes, go_bytes = envelope_path.read_bytes(), go_path.read_bytes()
    if (
        hashlib.sha256(envelope_bytes).hexdigest() != envelope_sha256
        or hashlib.sha256(go_bytes).hexdigest() != go_sha256
    ):
        raise BudgetRefused("separate envelope or GO launch pin changed")
    envelope = Envelope.model_validate_json(envelope_bytes)
    go = TypeAdapter[Object](Object).validate_json(go_bytes)
    if (
        go
        != {
            "live_go": True,
            "case": envelope.case,
            "envelope_sha256": envelope_sha256,
            "source_head": envelope.source_head,
            "config_sha256": envelope.config_sha256,
            "ledger_id": envelope.ledger_id,
            "scope": envelope.scope.model_dump(mode="json"),
            "output": str(output),
        }
        or type(go.get("live_go")) is not bool
    ):
        raise BudgetRefused("GO differs from the exact case/binding/financial envelope")
    if datetime.now(UTC) >= envelope.expires_at:
        raise BudgetRefused("case envelope expired")
    current = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip()
    dirty = subprocess.check_output(["git", "status", "--porcelain"], cwd=REPO, text=True)
    merged = subprocess.run(
        ["git", "merge-base", "--is-ancestor", current, "origin/integration/neutral-core"],
        cwd=REPO,
        capture_output=True,
        check=False,
    )
    if current != envelope.source_head or dirty or merged.returncode:
        raise BudgetRefused("live runner requires the exact clean merged source")
    validate_config(config, envelope, live=True)
    if output.exists() or not output.is_absolute() or output.resolve() != output:
        raise BudgetRefused("evidence output must be fresh and absolute")
    return Admission(envelope, config, output, envelope_path, go_path, envelope_sha256, go_sha256)


def validate_config(path: Path, envelope: Envelope, *, live: bool) -> BudgetConfig:
    if live and datetime.now(UTC).date().year >= 2027:
        raise BudgetRefused("introductory prices/runtime need review before a later live round")
    if hashlib.sha256(path.read_bytes()).hexdigest() != envelope.config_sha256:
        raise BudgetRefused("round host configuration pin changed")
    config = BudgetConfig.load(path)
    if set(config.providers) != {"gemini"} or config.total_cap_usd != 5:
        raise BudgetRefused("round must use its separately allocated five-dollar line")
    budget = config.providers["gemini"]
    if budget.cap_usd != 5 or set(budget.models) != {MODEL} or budget.session_prices:
        raise BudgetRefused("round allows only the exact primary and reviewed zero runtime")
    price = budget.models[MODEL]
    day = datetime.now(UTC).date()
    if (
        price.input != Decimal("0.75")
        or price.output != Decimal("3.75")
        or price.cached_input != Decimal("0.075")
        or price.cache_write_input != Decimal("0.75")
        or price.source != PRICE_SOURCE
        or price.pricing_basis != "standard-global"
        or price.effective_from is None
        or price.effective_until is None
        or not price.effective_from <= day < price.effective_until
    ):
        raise BudgetRefused("current dated standard primary prices are required")
    if live and config.ledger_path.resolve() != ROUND_LEDGER:
        raise BudgetRefused("live runner must use the signed separate round ledger")
    return config


class MeterFrames:
    """Observe raw SSE data without changing bytes, framing or SDK delivery."""

    def __init__(self, receive: Callable[[Object], None], taint: Callable[[], None]) -> None:
        self.receive, self.taint = receive, taint
        self.pending = b""
        self.data: list[bytes] = []
        self.error: Exception | None = None
        self.skip_lf = False

    def feed(self, chunk: bytes) -> None:
        if self.skip_lf and chunk:
            chunk = chunk.removeprefix(b"\n")
            self.skip_lf = False
        self.pending += chunk
        while True:
            separators = [
                index for value in (b"\n", b"\r") if (index := self.pending.find(value)) >= 0
            ]
            if not separators:
                break
            index = min(separators)
            if self.pending[index : index + 1] == b"\r" and index + 1 == len(self.pending):
                self.skip_lf = True  # Process CR now; absorb a later split LF.
            size = 2 if self.pending[index : index + 2] == b"\r\n" else 1
            line, self.pending = self.pending[:index], self.pending[index + size :]
            self.line(line)
        if len(self.pending) + sum(map(len, self.data)) > 4_194_304:
            self.taint()
            raise BudgetRefused("native meter frame exceeded the bounded decoder")
        if self.error is not None:
            error, self.error = self.error, None
            raise error

    def line(self, line: bytes) -> None:
        if not line:
            if self.data:
                data, self.data = b"\n".join(self.data), []
                if data != b"[DONE]":
                    try:
                        raw = TypeAdapter[Object](Object).validate_json(data)
                    except ValueError:
                        self.taint()
                        self.error = self.error or BudgetRefused("native meter frame is malformed")
                        return
                    try:
                        self.receive(raw)
                    except Exception as error:
                        self.taint()
                        self.error = self.error or error
        elif line.startswith(b"data:"):
            self.data.append(line[5:].removeprefix(b" "))

    def finish(self) -> None:
        self.feed(b"")
        if self.pending:
            self.line(self.pending.removesuffix(b"\r"))
            self.pending = b""
        self.line(b"")
        if self.error is not None:
            error, self.error = self.error, None
            raise error


class MeterStream(httpx.AsyncByteStream):
    def __init__(self, source: httpx.AsyncByteStream, frames: MeterFrames) -> None:
        self.source, self.frames = source, frames

    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            async for chunk in self.source:
                self.frames.feed(chunk)
                yield chunk
            self.frames.finish()
        except BaseException:
            self.frames.taint()
            raise

    async def aclose(self) -> None:
        try:
            self.frames.finish()
        finally:
            await self.source.aclose()


class RoundWire:
    """One scope, bounded actual request bytes; GET/cancel are not send retries."""

    def __init__(
        self, sdk: SDKTransport, book: UsageBook, recorder: Recorder, scope: Scope, maximum: int
    ) -> None:
        self.sdk, self.book, self.recorder, self.scope, self.maximum = (
            sdk,
            book,
            recorder,
            scope,
            maximum,
        )
        self.session: ResourceRef | None = None
        self.posts = 0
        self.cancels = 0
        self.reads = 0
        self.attempts = 0
        self.final = False
        self.request_sha256: str | None = None
        self.deleted: set[str] = set()
        self._recorded: set[str] = set()
        self.input_hashes: dict[str, str] = {}
        self.current_input_sha256: str | None = None
        self.request_model: str | None = None
        self.drivers: list[tuple[GeminiManagedAgents, ResourceRef]] = []
        self.http_snapshots: dict[str, Object] = {}
        self.routes: dict[httpx.Request, NativeRoute] = {}
        self.responses: list[JsonValue] = []

    def bind_session(self, session: ResourceRef) -> None:
        if (
            session.provider,
            session.kind,
            session.tenant_id,
            session.account_id,
            session.account_scope_id,
        ) != (
            "gemini",
            "session",
            self.scope.tenant_id,
            self.scope.account_id,
            self.scope.account_id,
        ):
            raise BudgetRefused("round native session differs from the admitted scope")
        self.session = session

    def attach_driver(self, driver: GeminiManagedAgents, session: ResourceRef) -> None:
        if self.session != session:
            raise BudgetRefused("driver journal differs from the bound session")
        self.drivers.append((driver, session))

    async def record_journals(self) -> None:
        for driver, session in self.drivers:
            page = await driver.events.list(self.scope, session, page=PageRequest())
            self.record(tuple(page.data))

    @property
    def native_posts(self) -> int:
        return self.posts

    @property
    def cancel_posts(self) -> int:
        return self.cancels

    async def capture_request(self, request: httpx.Request) -> None:
        path = request.url.path
        if (
            request.url.scheme != "https"
            or request.url.host != "generativelanguage.googleapis.com"
            or not path.startswith("/v1beta/interactions")
            or set(request.url.params) - {"stream", "last_event_id"}
        ):
            raise BudgetRefused("round SDK destination changed")
        fields: tuple[str, ...] = ()
        if request.method == "POST" and path.rstrip("/").endswith("/interactions"):
            parsed = json.loads(request.content)
            config = parsed.get("agent_config", {})
            if (
                self.session is None
                or self.posts >= self.maximum
                or self.book.exceeds_interaction_limit()
                or self.book.model_drift
                # The reviewed set: the inert no-tools sentinel alone (an empty list
                # would enable Gemini's billable built-ins).
                or parsed.get("tools") != [NO_TOOLS_TOOL]
                or parsed.get("agent") != BASE_AGENT
                or parsed.get("background") is not True
                or parsed.get("store") is not True
                or config != {"type": "antigravity", "model": MODEL, "max_total_tokens": 4096}
                or "previous_interaction_id" in parsed
                or set(parsed)
                - {"agent", "agent_config", "input", "tools", "environment", "background", "store"}
                or parsed.get("environment") not in ("remote", {"type": "remote", "sources": []})
            ):
                raise BudgetRefused(
                    "unreviewed tools/model/environment or extra native interaction"
                )
            self.posts += 1
            fields = tuple(sorted(parsed))
            self.request_sha256 = hashlib.sha256(request.content).hexdigest()
            self.request_model = config["model"]
            self.current_input_sha256 = hashlib.sha256(
                json.dumps(parsed["input"], sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            route = NativeRoute(
                "create", self.session, None, self.request_sha256, self.request_model
            )
        elif request.method == "POST":
            native_id = (
                path.split("/")[-2]
                if path.endswith("/cancel")
                else path.rsplit("/", 1)[-1].removesuffix(":cancel")
            )
            if native_id not in self.book.native or self.cancels:
                raise BudgetRefused("round cancellation must target one accepted owned interaction")
            if path not in (
                f"/v1beta/interactions/{native_id}:cancel",
                f"/v1beta/interactions/{native_id}/cancel",
            ):
                raise BudgetRefused("unreviewed native mutation route")
            self.cancels += 1
            accepted = self.book.native[native_id]
            route = NativeRoute(
                "cancel",
                accepted.session,
                native_id,
                accepted.request_sha256,
                accepted.request_model,
            )
        elif request.method in ("GET", "DELETE"):
            native_id = path.rsplit("/", 1)[-1]
            if native_id not in self.book.native:
                raise BudgetRefused("round read/delete must target an accepted owned interaction")
            if path != f"/v1beta/interactions/{native_id}":
                raise BudgetRefused("unreviewed native read/delete route")
            accepted = self.book.native[native_id]
            kind = (
                "cleanup"
                if request.method == "DELETE"
                else "stream"
                if request.url.params.get("stream") == "true"
                else "readback"
            )
            route = NativeRoute(
                kind,
                accepted.session,
                native_id,
                accepted.request_sha256,
                accepted.request_model,
                final=kind == "readback" and self.final,
            )
        else:
            raise BudgetRefused("unreviewed SDK request method")
        self.routes[request] = route
        self.recorder.record(
            RequestMetadata(
                method=request.method,
                path=self.book.aliases.path(path),
                body_fields=fields,
            ),
            (),
        )

    def receive_native(self, response: httpx.Response, raw: Object) -> None:
        """All JSON bodies and SSE frames converge here, before SDK decoding."""
        request = response.request
        route = self.routes.get(request)
        if route is None:
            raise BudgetRefused("native response lacks an admitted request route")
        snapshots = self.book.receive(raw, route, success=response.status_code < 300)
        self.http_snapshots[request.url.path] = {"id": raw.get("id")}
        for snapshot in snapshots:
            native_id = snapshot.get("id")
            counts = counter_metadata(snapshot)
            projected_counts: Object = {}
            for name in COUNTERS:
                count = counts.get(name)
                projected_counts[name] = count if type(count) is int and count >= 0 else None
            updated = timestamp(snapshot.get("updated"))
            self.responses.append(
                {
                    "method": request.method,
                    "path": self.book.aliases.path(request.url.path),
                    "response_kind": route.kind,
                    "http_status": response.status_code,
                    "interaction": self.book.aliases.identity(native_id)
                    if isinstance(native_id, str)
                    else None,
                    "interaction_status": snapshot.get("status"),
                    "updated": updated.isoformat() if updated else None,
                    "usageMetadata": projected_counts,
                    "meter_origin": "usageMetadata"
                    if snapshot.get("usageMetadata") is not None
                    else "usage",
                }
            )

    async def capture_response(self, response: httpx.Response) -> None:
        route = self.routes.get(response.request)
        if route is None:
            raise BudgetRefused("native response lacks an admitted request route")
        content_type = response.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            self.book.receive({}, route, success=response.status_code < 300)
            frames = MeterFrames(
                lambda raw: self.receive_native(response, raw),
                lambda: self.book.taint(route.native_id),
            )
            if response.is_stream_consumed:
                frames.feed(response.content)
                frames.finish()
            else:
                if not isinstance(response.stream, httpx.AsyncByteStream):
                    self.book.taint(route.native_id)
                    raise BudgetRefused("native meter stream is not asynchronously owned")
                response.stream = MeterStream(response.stream, frames)
            return
        await response.aread()
        try:
            raw = (
                TypeAdapter[Object](Object).validate_json(response.content)
                if response.content
                else {}
            )
        except ValueError:
            self.book.taint(route.native_id)
            raise BudgetRefused("native response is malformed") from None
        self.receive_native(response, raw)

    async def create(self, request: Mapping[str, JsonValue]) -> Object:
        if self.session is None or self.attempts >= self.maximum:
            raise BudgetRefused("round has no admitted send slot")
        self.attempts += 1
        bounded = dict(request)
        config = bounded.get("agent_config")
        if not isinstance(config, dict) or config.get("model") != MODEL:
            raise BudgetRefused("round request model is not the primary")
        bounded["agent_config"] = {**config, "max_total_tokens": 4096}
        raw = await self.sdk.create(bounded)
        if self.request_sha256 is None:
            raise BudgetRefused("actual SDK POST bytes were not observed")
        native = self.http_snapshots.pop("/v1beta/interactions", None)
        if native is None or native.get("id") != raw.get("id"):
            raise BudgetRefused("native acceptance/SDK identity lacks independent HTTP evidence")
        native_id = native.get("id")
        if not isinstance(native_id, str) or native_id not in self.book.native:
            raise BudgetRefused("native acceptance was not retained before SDK decoding")
        if self.current_input_sha256 is not None and isinstance(raw.get("id"), str):
            native_id = raw["id"]
            if isinstance(native_id, str):
                self.input_hashes[native_id] = self.current_input_sha256
        return raw

    async def get(self, interaction_id: str) -> Object:
        if interaction_id not in self.book.native or self.reads >= 80:
            raise BudgetRefused("round native read count/scope refused")
        self.reads += 1
        raw = await self.sdk.get(interaction_id)
        native = self.http_snapshots.pop(f"/v1beta/interactions/{interaction_id}", None)
        if native is None or native.get("id") != raw.get("id"):
            self.book.native[interaction_id].uncertain = True
            self.book.native[interaction_id].final_complete = False
            if self.final and not self.book.native[interaction_id].final_get:
                self.book.failed_final(interaction_id)
        return raw

    async def cancel(self, interaction_id: str) -> Object:
        if interaction_id not in self.book.native:
            raise BudgetRefused("round cancel scope refused")
        return await self.sdk.cancel(interaction_id)

    async def download_snapshot(self, environment_id: str) -> bytes:
        raise BudgetRefused("round cannot download snapshots")

    async def open_stream(
        self, interaction_id: str, *, after: str | None = None
    ) -> AsyncIterator[Object]:
        if interaction_id not in self.book.native:
            raise BudgetRefused("round stream scope refused")
        return await self.sdk.open_stream(interaction_id, after=after)

    def record(self, events: tuple[Event, ...]) -> None:
        fresh = tuple(self.book.aliases.event(e) for e in events if e.id not in self._recorded)
        self.recorder.record(RequestMetadata(method="GET", path="/host/round/journal"), fresh)
        self._recorded.update(e.id for e in events)

    def verify_receipt(
        self, session: ResourceRef, receipt: SendReceipt, inputs: Sequence[InputEvent]
    ) -> bool:
        if len(receipt.input_ids) != 1 or receipt.turn_id != receipt.input_ids[0]:
            return False
        native_id = receipt.input_ids[0]
        evidence = self.book.native.get(native_id)
        if evidence is None or evidence.session != session:
            return False
        body: list[Object] = []
        for input_ in inputs:
            if not isinstance(input_, UserMessage) or any(
                not isinstance(p, TextPart) for p in input_.content
            ):
                return False
            body.append(
                {
                    "type": "text",
                    "text": "".join(p.text for p in input_.content if isinstance(p, TextPart)),
                }
            )
        digest = hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return self.input_hashes.get(native_id) == digest


@asynccontextmanager
async def owned_sdk(
    key: str, mock: httpx.MockTransport | None
) -> AsyncIterator[tuple[SDKTransport, httpx.AsyncClient]]:
    """Explicit auth and owned sync/async HTTP; no ambient TLS/proxy routes."""
    _require_clean_environment()
    from google import genai
    from google.genai.types import HttpOptions, HttpRetryOptions

    context = httpx.create_ssl_context(trust_env=False)
    with httpx.Client(
        transport=mock, trust_env=False, follow_redirects=False, verify=context, proxy=None
    ) as sync:
        async with httpx.AsyncClient(
            transport=mock, trust_env=False, follow_redirects=False, verify=context, proxy=None
        ) as http:
            client = genai.Client(
                api_key=key,
                enterprise=False,
                vertexai=False,
                credentials=None,
                project=None,
                location=None,
                debug_config=genai.client.DebugConfig(
                    client_mode=None, replays_directory=None, replay_id=None
                ),
                http_options=HttpOptions(
                    base_url="https://generativelanguage.googleapis.com",
                    api_version="v1beta",
                    httpx_client=sync,
                    httpx_async_client=http,
                    headers={"x-goog-api-key": key},
                    retry_options=HttpRetryOptions(attempts=1),
                ),
            )
            try:
                client._nextgen_client.max_retries = 0  # pyright: ignore[reportPrivateUsage]
                client.aio._nextgen_client.max_retries = 0  # pyright: ignore[reportPrivateUsage]
                yield SDKTransport(client), http
            finally:
                try:
                    await client.aio.aclose()
                finally:
                    client.close()


async def execute(
    admission: Admission, *, mock: httpx.MockTransport | None = None, live: bool = False
) -> Object:
    """One case execution, one aggregate reservation, terminal GETs BEFORE cleanup."""
    _require_clean_environment()
    if mock is None and not live or mock is not None and live:
        raise BudgetRefused("select an explicit mock or separately admitted live execution")
    if mock is not None and not isinstance(mock, httpx.MockTransport):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise BudgetRefused("offline execution requires a real MockTransport")
    if live:
        if (
            admission.envelope_path is None
            or admission.go_path is None
            or admission.envelope_sha256 is None
            or admission.go_sha256 is None
        ):
            raise BudgetRefused("live execution requires separately pinned admission documents")
        admission = admit(
            admission.envelope_path,
            admission.go_path,
            envelope_sha256=admission.envelope_sha256,
            go_sha256=admission.go_sha256,
            config=admission.config,
            output=admission.output,
        )
    envelope = admission.envelope
    # Revalidate even an explicit offline caller's model_copy/constructor input.
    envelope = Envelope.model_validate_json(envelope.model_dump_json())
    config = validate_config(admission.config, envelope, live=live)
    output = admission.output
    output.mkdir(mode=0o700)  # Existing evidence is never rerun or overwritten.
    fd = os.open(output / ".attempt", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    guard = BudgetGuard(admission.config, config.ledger_path)
    reservation = guard.reserve_with_hold(
        ProbePlan(provider="gemini", model=MODEL, fixture_id=envelope.case, limits=envelope.limits),
        minimum_hold_usd=envelope.maximum_usd,
        maximum_hold_usd=envelope.maximum_usd,
        expected_config_sha256=envelope.config_sha256,
        expected_ledger_id=envelope.ledger_id,
    )
    key = read_key() if live else "offline-round-only"
    aliases, recorder = EvidenceAliases((key,)), Recorder(secrets=(key,))
    book = UsageBook(output, aliases)
    result = Result(envelope.case, "fail", ("case did not reach complete evidence",))
    status: Literal["completed", "failed", "cancelled"] = "failed"
    start = asyncio.get_running_loop().time()
    cancelled = False
    wire: RoundWire | None = None
    try:
        async with owned_sdk(key, mock) as (sdk, client_http):
            http = client_http
            wire = RoundWire(sdk, book, recorder, envelope.scope, envelope.interactions)
            http.event_hooks["request"].append(wire.capture_request)
            http.event_hooks["response"].append(wire.capture_response)
            try:
                async with asyncio.timeout(max(1, envelope.deadline_seconds - 15)):
                    result = await (
                        run_c06(wire, envelope.scope)
                        if envelope.case == "C06"
                        else run_c04(wire, envelope.scope)
                    )
                status = "completed"
            except asyncio.CancelledError:
                cancelled, status = True, "cancelled"
            except Exception:
                result = Result(
                    envelope.case, "fail", ("actual case assertions or provider execution failed",)
                )
            finally:
                for native_id in book.native:
                    wire.final = True
                    try:
                        async with asyncio.timeout(
                            max(
                                0.001,
                                min(
                                    3,
                                    envelope.deadline_seconds
                                    - (asyncio.get_running_loop().time() - start),
                                ),
                            )
                        ):
                            await wire.get(native_id)
                    except Exception:
                        if not book.native[native_id].final_get:
                            book.failed_final(native_id)
                    except asyncio.CancelledError:
                        cancelled, status = True, "cancelled"
                        if not book.native[native_id].final_get:
                            book.failed_final(native_id)
                    wire.final = False
                    try:
                        async with asyncio.timeout(
                            max(
                                0.001,
                                min(
                                    2,
                                    envelope.deadline_seconds
                                    - (asyncio.get_running_loop().time() - start),
                                ),
                            )
                        ):
                            # Delete only this accepted private native identity.
                            owned = sdk._interactions  # pyright: ignore[reportPrivateUsage]
                            await provider_call(
                                owned.delete(
                                    id=native_id, extra_headers={"Api-Revision": API_REVISION}
                                )
                            )
                            wire.deleted.add(native_id)
                    except Exception:
                        result = Result(envelope.case, "fail", ("owned provider cleanup failed",))
                    except asyncio.CancelledError:
                        cancelled, status = True, "cancelled"
    except asyncio.CancelledError:
        cancelled, status = True, "cancelled"
    except Exception:
        result = Result(envelope.case, "fail", ("owned SDK setup or finalization failed",))
    finally:
        if wire is not None:
            try:
                await (
                    wire.record_journals()
                )  # Local driver facts, even after failed/cancelled runs.
            except Exception:
                result = Result(envelope.case, "fail", ("actual driver journal collection failed",))
        if cancelled:
            result = Result(envelope.case, "fail", ("case execution was cancelled",))
        actual, bounds = book.amounts(attempts=wire.attempts if wire else 1)
        with localcontext() as decimal_context:
            decimal_context.prec = 80
            minimum_charge = sum(
                (
                    reservation.price.minimum_charge(r.tokens, r.observed_at)
                    for r in bounds.requests
                ),
                Decimal(0),
            )
        exceeds = (
            sum(r.tokens.minimum_input_tokens for r in bounds.requests)
            > envelope.limits.input_tokens
            or sum(r.tokens.output_tokens or 0 for r in bounds.requests)
            > envelope.limits.output_tokens
            or minimum_charge > envelope.maximum_usd
        )
        if exceeds:
            receipt = guard.settle_with_overrun(
                reservation,
                status=status,
                limits=envelope.limits,
                actual=actual,
                overrun_evidence=bounds,
            )
            result = Result(
                envelope.case, "fail", ("known usage exceeded the signed case ceiling",)
            )
        else:
            receipt = guard.settle(
                reservation,
                status=status,
                limits=envelope.limits,
                actual=actual,
            )
        if book.exceeds_interaction_limit():
            result = Result(
                envelope.case,
                "fail",
                ("native interaction exceeded its individual model-limit premise",),
            )
        if book.model_drift:
            result = Result(
                envelope.case, "fail", ("native response reported an unreviewed model",)
            )
        recorder.save(
            output / f"{envelope.case}.json",
            fixture_id=envelope.case,
            provider="gemini",
            model=MODEL,
            complete=result.status == "pass",
        )
        report: Object = {
            "case": envelope.case,
            "model": MODEL,
            "status": result.status,
            "origin": "live-provider" if live else "sdk-mock",
            "evidence": list(result.evidence),
            "Imax": INPUT_MAX,
            "Omax": OUTPUT_MAX,
            "N": envelope.interactions,
            "maximum_usd": str(envelope.maximum_usd),
            "runtime_max_usd": "0",
            "tools": [NO_TOOLS_TOOL],
            "native_creates": wire.posts if wire else 0,
            "actual_usd": str(receipt.actual_usd) if receipt.actual_usd is not None else None,
            "held_usd": str(receipt.held_usd),
            "receipt": receipt.model_dump(mode="json"),
            "interactions": TypeAdapter(list[JsonValue]).validate_python(book.public_facts()),
            "http_usage": wire.responses if wire else [],
            "native_model_drift": book.model_drift,
            "cleanup": [aliases.identity(i) for i in sorted(wire.deleted)] if wire else [],
        }
        write_report(output / "result.json", report)
    if cancelled:
        raise asyncio.CancelledError
    return report


def main() -> int:
    """Separate signed-envelope AND GO pins; never a key-only live switch."""
    import argparse

    parser = argparse.ArgumentParser(description="Signed one-shot Gemini C06/C04 live round")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--envelope", type=Path, required=True)
    parser.add_argument("--envelope-sha256", required=True)
    parser.add_argument("--go", type=Path, required=True)
    parser.add_argument("--go-sha256", required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.live:
        parser.error("live execution requires explicit selection and the lead's two document pins")
    try:
        admitted = admit(
            args.envelope,
            args.go,
            envelope_sha256=args.envelope_sha256,
            go_sha256=args.go_sha256,
            config=args.config,
            output=args.output,
        )
        result = asyncio.run(execute(admitted, live=True))
    except asyncio.CancelledError:
        print("Gemini case cancelled; retained its financial evidence")
        return 1
    except Exception:
        print("Gemini case refused or failed; no retry authorized")
        return 1
    # Closed status + financial fields only; no native identity/key/raw body.
    print(json.dumps({name: result[name] for name in ("case", "status", "actual_usd", "held_usd")}))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
