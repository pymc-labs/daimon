"""Bounded, transport-neutral pre-turn context. Provider text is never logged."""

from __future__ import annotations

import asyncio
import math
import os
import time
import uuid
from asyncio import sleep as _keepwarm_sleep
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import cache
from http.cookiejar import CookieJar, DefaultCookiePolicy
from ssl import SSLContext
from time import monotonic as _keepwarm_now
from typing import Any, Literal, Protocol

import httpx
import structlog
from httpx import AsyncClient
from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter

log = structlog.get_logger(__name__)
MAX_RESPONSE_BYTES = 65_536
# Expire idle connections before the provider's 300-second keep-alive.
KEEPALIVE_EXPIRY_SECONDS = 240
KEEPWARM_IDLE_SECONDS = 200
KEEPWARM_TICK_SECONDS = 20
KEEPWARM_TIMEOUT_SECONDS = 5.0
KEEPWARM_REFRESH_TICKS = 10
KEEPWARM_MAX_RESPONSE_BYTES = 4096
TURN_CEILING_SECONDS = 0.2
FIRST_CALL_CEILING_SECONDS = 0.4
DETACHED_CALL_TIMEOUT_SECONDS = 5.0
MAX_DETACHED_CALLS = 8


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    transport: Literal["mcp"] = "mcp"
    endpoint: str
    # Resolve credentials on the host; never persist a bearer key in the settings JSON.
    bearer_env: str | None = None
    enabled: bool = False
    timeout_ms: int = Field(default=200, ge=1, le=200)
    first_call_timeout_ms: int = Field(default=400, ge=1, le=400)
    max_tokens: int = Field(default=2500, ge=1, le=2500)
    system_max_tokens: int = Field(default=1000, ge=1, le=2000)


class ContextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1] = 1
    tenant_id: uuid.UUID
    server_id: str
    platform: str
    channel_id: str | None
    parent_channel_id: str | None = None
    thread_id: str | None = None
    place_type: Literal["channel", "thread", "dm"]
    requester_platform_user_id: str
    agent_id: str
    # This is host-attested origin metadata, NOT evidence of internal placement.
    # The authenticated provider must verify the actual destination and caller.
    caller_identity: str = "daimon-os"
    trust_placement: Literal["provider_verifies"] = "provider_verifies"
    message_text: str = ""
    mention_ids: tuple[str, ...] = ()
    session_id: str = ""
    system_fragment_received: bool = False
    sent_hashes: dict[str, str] = Field(default_factory=dict)
    deadline_ms: int = 200
    max_tokens: int = 2500
    system_max_tokens: int = 1000


class EmittedCard(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source: str
    as_of: str


class ContextResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    system_fragment: str = ""
    turn_fragment: str = ""
    cards: tuple[EmittedCard, ...] = ()
    estimated_tokens: int = Field(default=0, ge=0)
    token_method: Literal["ceil_chars_div_3.5"] = "ceil_chars_div_3.5"
    provenance: dict[str, JsonValue] = Field(default_factory=dict)
    cache_hint: dict[str, JsonValue] = Field(default_factory=dict)
    status: Literal["ok", "empty", "refused"] = "empty"


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / 3.5)


class Provider(Protocol):
    async def __call__(
        self, config: ProviderConfig, request: ContextRequest
    ) -> ContextResponse: ...


@cache
def _ssl_context() -> SSLContext:
    # Warmed by provider construction outside turns; subsequent owners do no CA I/O.
    return httpx.create_ssl_context(trust_env=False)


def _origin(endpoint: str) -> httpx.URL:
    url = httpx.URL(endpoint)
    return httpx.URL(scheme=url.scheme, host=url.host, port=url.port)


@dataclass
class _CallTiming:
    starts: dict[str, float] = field(default_factory=dict[str, float])
    durations: dict[str, float] = field(default_factory=dict[str, float])
    new_connection: bool = False

    async def trace(self, event: str, info: dict[str, Any]) -> None:
        # Consume event names only: info can contain headers and credentials.
        phase, _, edge = event.rpartition(".")
        if phase not in {
            "connection.connect_tcp",
            "connection.start_tls",
            "http11.receive_response_headers",
            "http2.receive_response_headers",
        }:
            return
        now = time.monotonic()
        if edge == "started":
            self.starts[phase] = now
            self.new_connection |= phase == "connection.connect_tcp"
        elif edge in {"complete", "failed"} and phase in self.starts:
            self.durations[phase] = (now - self.starts.pop(phase)) * 1000

    def log(self) -> None:
        log.info(
            "context_provider.timing",
            # httpcore's connect_tcp includes resolution; its trace does not
            # expose DNS separately. Null avoids claiming unmeasured DNS is zero.
            dns_ms=None,
            connect_ms=round(self.durations.get("connection.connect_tcp", 0.0), 3),
            connect_includes_dns=True,
            tls_ms=round(self.durations.get("connection.start_tls", 0.0), 3),
            headers_ms=round(
                sum(v for k, v in self.durations.items() if k.endswith("receive_response_headers")),
                3,
            ),
            new_connection=self.new_connection,
        )


class McpProvider:
    """Stateless MCP tools/call with bounded JSON or SSE replies."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._last_traffic: dict[httpx.URL, float] = {}
        self._keepwarm_task: asyncio.Task[None] | None = None
        # The imported constructor isolates this owner from other transports' overrides.
        self.client = AsyncClient(
            transport=transport,
            limits=httpx.Limits(
                max_connections=100,
                max_keepalive_connections=20,
                keepalive_expiry=KEEPALIVE_EXPIRY_SECONDS,
            ),
            verify=_ssl_context(),
            follow_redirects=False,
            trust_env=False,
            cookies=CookieJar(policy=DefaultCookiePolicy(allowed_domains=[])),
        )

    def start(self, load: Callable[[], Awaitable[list[ProviderConfig]]]) -> None:
        """Start one best-effort owner; startup never waits on network or the DB."""
        if self._keepwarm_task is None and not self.client.is_closed:
            self._keepwarm_task = asyncio.create_task(self._keepwarm(load))

    async def _keepwarm(self, load: Callable[[], Awaitable[list[ProviderConfig]]]) -> None:
        origins: set[httpx.URL] = set()
        refresh_in = 0
        while True:
            if refresh_in == 0:
                refresh_in = KEEPWARM_REFRESH_TICKS
                try:
                    async with asyncio.timeout(KEEPWARM_TIMEOUT_SECONDS):
                        configs = await load()
                    refreshed: set[httpx.URL] = set()
                    for config in configs:
                        try:
                            config = ProviderConfig.model_validate(config)
                            if config.enabled and httpx.URL(config.endpoint).scheme == "https":
                                refreshed.add(_origin(config.endpoint))
                        except Exception as error:
                            log.warning(
                                "context_provider.keepwarm_skipped",
                                error_class=type(error).__name__,
                            )
                    origins = refreshed
                    self._last_traffic = {
                        origin: touched
                        for origin, touched in self._last_traffic.items()
                        if origin in origins
                    }
                except Exception as error:
                    log.warning(
                        "context_provider.keepwarm_failed", error_class=type(error).__name__
                    )
                    if not origins:
                        refresh_in = 0
            await asyncio.gather(
                *(
                    self._ping(origin)
                    for origin in origins
                    if _keepwarm_now() - self._last_traffic.get(origin, -math.inf)
                    >= KEEPWARM_IDLE_SECONDS
                )
            )
            refresh_in = max(0, refresh_in - 1)
            await _keepwarm_sleep(KEEPWARM_TICK_SECONDS)

    async def _ping(self, origin: httpx.URL) -> None:
        try:
            self._last_traffic[origin] = _keepwarm_now()
            async with asyncio.timeout(KEEPWARM_TIMEOUT_SECONDS):
                # No config/credential resolution or destination data on health requests.
                async with self.client.stream(
                    "GET",
                    origin.copy_with(path="/health"),
                    timeout=KEEPWARM_TIMEOUT_SECONDS,
                ) as reply:
                    read = 0
                    async for chunk in reply.aiter_bytes(chunk_size=KEEPWARM_MAX_RESPONSE_BYTES):
                        read += len(chunk)
                        if read >= KEEPWARM_MAX_RESPONSE_BYTES:
                            break
                    log.debug("context_provider.keepwarm", status=reply.status_code)
        except Exception as error:
            log.warning("context_provider.keepwarm_failed", error_class=type(error).__name__)
        finally:
            self._last_traffic[origin] = _keepwarm_now()

    async def aclose(self) -> None:
        if self._keepwarm_task is not None:
            self._keepwarm_task.cancel()
            await asyncio.gather(self._keepwarm_task, return_exceptions=True)
        await self.client.aclose()

    async def __call__(self, config: ProviderConfig, request: ContextRequest) -> ContextResponse:
        timing = _CallTiming()
        try:
            origin = _origin(config.endpoint)
            self._last_traffic[origin] = _keepwarm_now()
            try:
                return await self._call(config, request, timing)
            finally:
                self._last_traffic[origin] = _keepwarm_now()
        finally:
            timing.log()

    async def _call(
        self,
        config: ProviderConfig,
        request: ContextRequest,
        timing: _CallTiming,
    ) -> ContextResponse:
        if httpx.URL(config.endpoint).scheme != "https":
            raise ValueError("provider endpoint must use HTTPS")
        if config.bearer_env and not config.bearer_env.startswith(
            f"DAIMON_CONTEXT_{request.tenant_id.hex.upper()}_"
        ):
            raise ValueError("provider credential reference belongs to another scope")
        key = os.environ[config.bearer_env] if config.bearer_env else None
        headers = {"Accept": "application/json, text/event-stream"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        body = {
            "jsonrpc": "2.0",
            "id": "context",
            "method": "tools/call",
            "params": {
                "name": "context_cards_for_turn",
                "arguments": request.model_dump(mode="json"),
            },
        }
        async with self.client.stream(
            "POST",
            config.endpoint,
            headers=headers,
            json=body,
            timeout=DETACHED_CALL_TIMEOUT_SECONDS,
            extensions={"trace": timing.trace},
        ) as reply:
            reply.raise_for_status()
            raw = bytearray()
            async for chunk in reply.aiter_bytes():
                if len(raw) + len(chunk) > MAX_RESPONSE_BYTES:
                    raise ValueError("provider response exceeds byte cap")
                raw.extend(chunk)
            content_type = reply.headers.get("content-type", "").split(";", 1)[0]
        if content_type == "text/event-stream":
            # FastMCP stateless streams carry one JSON-RPC message. Ignore SSE
            # metadata/comments; join data lines according to the SSE format.
            messages: list[str] = []
            data: list[str] = []
            for line in raw.decode("utf-8").splitlines() + [""]:
                if not line:
                    if data:
                        messages.append("\n".join(data))
                        data = []
                elif line.startswith("data:"):
                    value = line[5:]
                    data.append(value[1:] if value.startswith(" ") else value)
            if len(messages) != 1:
                raise ValueError("expected one MCP message")
            payload = messages[0]
        else:
            payload = bytes(raw)
        envelope = TypeAdapter(dict[str, JsonValue]).validate_json(payload)
        result = envelope.get("result")
        if (
            envelope.get("jsonrpc") != "2.0"
            or envelope.get("id") != "context"
            or not isinstance(result, dict)
        ):
            raise ValueError("invalid MCP response")
        if result.get("isError") or "structuredContent" not in result:
            raise ValueError("MCP tool failed or has no structured result")
        return ContextResponse.model_validate(result["structuredContent"])


@dataclass
class SessionContext:
    hashes: dict[str, str] = field(default_factory=dict[str, str])
    system_received: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    touched: float = field(default_factory=time.monotonic)


@dataclass
class ContextInput:
    response: ContextResponse = field(default_factory=ContextResponse)
    state: SessionContext | None = None

    def accepted(self) -> None:
        """Only the successful input-send edge may advance acknowledgement state."""
        if self.state is not None:
            self.state.hashes.update({card.id: card.hash for card in self.response.cards})
            self.state.system_received |= bool(self.response.system_fragment)


@dataclass(frozen=True)
class LoadedProvider:
    config: ProviderConfig
    server_id: str


ConfigLoader = Callable[[ContextRequest], Awaitable[LoadedProvider | None]]


async def _bounded[T](call: Awaitable[T], seconds: float, detached: set[asyncio.Future[Any]]) -> T:
    async def _capped() -> T:
        # Bound config reads and the whole provider call, including late work.
        async with asyncio.timeout(DETACHED_CALL_TIMEOUT_SECONDS):
            return await call

    task = asyncio.ensure_future(_capped())
    try:
        done, _ = await asyncio.wait({task}, timeout=max(0, seconds))
        if not done:
            if len(detached) < MAX_DETACHED_CALLS:
                detached.add(task)
                task.add_done_callback(detached.discard)
                task.add_done_callback(_consume_task)
            raise TimeoutError
        if task.cancelled():
            raise RuntimeError("context operation cancelled itself")
        return task.result()
    finally:
        if not task.done() and task not in detached:
            task.cancel()
            task.add_done_callback(_consume_task)


class ContextProviders:
    """Host owner; tenant + binding + destination + session isolate acknowledgement.

    Entries expire after 24h; restart/eviction can repeat context after eviction.
    Providers are called every turn to recheck destination authorization; cached
    hashes are only acknowledgements, never cached authorization or card content.
    """

    def __init__(self, *, provider: Provider | None = None, max_sessions: int = 1024) -> None:
        self.provider = provider or McpProvider()
        self._detached: set[asyncio.Future[Any]] = set()
        self.max_sessions = max_sessions
        self.sessions: OrderedDict[tuple[uuid.UUID, str, str, str], SessionContext] = OrderedDict()

    def start(self, load: Callable[[], Awaitable[list[ProviderConfig]]]) -> None:
        if isinstance(self.provider, McpProvider):
            self.provider.start(load)

    async def aclose(self) -> None:
        for task in self._detached:
            task.cancel()
        await asyncio.gather(*self._detached, return_exceptions=True)
        if isinstance(self.provider, McpProvider):
            await self.provider.aclose()

    @asynccontextmanager
    async def turn(
        self,
        request: ContextRequest,
        load: ConfigLoader,
    ) -> AsyncIterator[ContextInput]:
        started = time.monotonic()
        state: SessionContext | None = None
        acquired = False
        injection = ContextInput()
        try:
            # Config reads have the same 200ms host ceiling, including pool waits.
            loaded = await _bounded(load(request), TURN_CEILING_SECONDS, self._detached)
            if loaded is not None and loaded.config.enabled:
                config = loaded.config
                destination = repr(
                    (
                        request.platform,
                        loaded.server_id,
                        request.channel_id,
                        request.parent_channel_id,
                        request.thread_id,
                        request.place_type,
                        request.agent_id,
                    )
                )
                key = (request.tenant_id, config.model_dump_json(), destination, request.session_id)
                for old_key, old in list(self.sessions.items()):
                    if not old.lock.locked() and time.monotonic() - old.touched > 86400:
                        del self.sessions[old_key]
                if key not in self.sessions:
                    if len(self.sessions) >= self.max_sessions:
                        victim = next(
                            (k for k, v in self.sessions.items() if not v.lock.locked()), None
                        )
                        if victim is None:
                            raise ValueError("context session cache is full")
                        del self.sessions[victim]
                    self.sessions[key] = SessionContext()
                state = self.sessions[key]
                self.sessions.move_to_end(key)
                # Acknowledgement only advances, so an already acknowledged session
                # can use its shorter lock deadline before acquiring the lock.
                expires = started + (
                    min(config.timeout_ms / 1000, TURN_CEILING_SECONDS)
                    if state.system_received
                    else min(config.first_call_timeout_ms / 1000, FIRST_CALL_CEILING_SECONDS)
                )
                # asyncio.Lock is cancellable, with no user provider code involved.
                async with asyncio.timeout(max(0, expires - time.monotonic())):
                    await state.lock.acquire()
                    acquired = True
                # A concurrent send may have acknowledged the system while we waited.
                # Both budgets include config and lock time from this hook's start.
                if state.system_received:
                    expires = started + min(config.timeout_ms / 1000, TURN_CEILING_SECONDS)
                else:
                    expires = started + min(
                        config.first_call_timeout_ms / 1000, FIRST_CALL_CEILING_SECONDS
                    )
                state.touched = time.monotonic()
                remaining = expires - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                effective = request.model_copy(
                    update={
                        "server_id": loaded.server_id,
                        "system_fragment_received": state.system_received,
                        "sent_hashes": dict(state.hashes),
                        "deadline_ms": max(1, int(remaining * 1000)),
                        "max_tokens": config.max_tokens,
                        "system_max_tokens": config.system_max_tokens,
                    }
                )
                result = await _bounded(self.provider(config, effective), remaining, self._detached)
                if (
                    len(result.model_dump_json().encode()) > MAX_RESPONSE_BYTES
                    or estimate_tokens(result.turn_fragment) > config.max_tokens
                    or estimate_tokens(result.system_fragment) > config.system_max_tokens
                    or len(state.hashes.keys() | {c.id for c in result.cards}) > 512
                ):
                    raise ValueError("provider response exceeds budget")
                if result.status == "ok":
                    if state.system_received and result.system_fragment:
                        raise ValueError("provider repeated system fragment")
                    if any(state.hashes.get(c.id) == c.hash for c in result.cards):
                        raise ValueError("provider repeated acknowledged card")
                    injection = ContextInput(result, state)
                elif result.status == "refused":
                    log.info("context_provider.refused", tenant_id=str(request.tenant_id))
        except Exception as error:
            log.warning(
                "context_provider.dropped",
                tenant_id=str(request.tenant_id),
                error_class=type(error).__name__,
            )
        except BaseException:
            if acquired and state is not None:
                state.lock.release()
            raise
        try:
            yield injection
        finally:
            if acquired and state is not None:
                state.lock.release()


def _consume_task[T](task: asyncio.Future[T]) -> None:
    if not task.cancelled():
        task.exception()


@dataclass(frozen=True)
class ContextHook:
    owner: ContextProviders
    request: ContextRequest
    load: ConfigLoader
    system_message_supported: bool = True

    def turn(self, session_id: str):
        return self.owner.turn(
            self.request.model_copy(update={"session_id": session_id}),
            self.load,
        )
