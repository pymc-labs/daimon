"""MA transport fakes for Daimon test suites.

Provides transport-level fake helpers (handler functions, MARouter, combine_handlers)
and shared constants for building AsyncAnthropic instances backed by httpx.MockTransport.

Usage pattern:
    from daimon.testing.ma import build_fake_anthropic, make_fake_ma_handler
    client = build_fake_anthropic(make_fake_ma_handler())

Custom handler composition:
    from daimon.testing.ma import combine_handlers, NotHandled

    def my_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/custom":
            return httpx.Response(200, json={...})
        raise NotHandled

    client = build_fake_anthropic(combine_handlers(my_handler, make_fake_ma_handler()))
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

import httpx
import pytest
from anthropic import AsyncAnthropic
from anthropic.types.beta import (
    BetaCloudConfig,
    BetaEnvironment,
    BetaManagedAgentsSession,
    BetaPackages,
    BetaUnrestrictedNetwork,
)
from anthropic.types.beta.beta_managed_agents_model_config import BetaManagedAgentsModelConfig
from anthropic.types.beta.beta_managed_agents_session_agent import BetaManagedAgentsSessionAgent
from anthropic.types.beta.beta_managed_agents_session_stats import BetaManagedAgentsSessionStats
from anthropic.types.beta.beta_managed_agents_session_usage import BetaManagedAgentsSessionUsage

# ---------------------------------------------------------------------------
# Sentinel
# ---------------------------------------------------------------------------


class NotHandled(Exception):
    """Raise from a handler passed to combine_handlers to indicate this handler
    does not match the request. combine_handlers will try the next handler."""


# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

EMPTY_CLOUD_CONFIG = BetaCloudConfig(
    type="cloud",
    networking=BetaUnrestrictedNetwork(type="unrestricted"),
    packages=BetaPackages(apt=[], cargo=[], gem=[], go=[], npm=[], pip=[]),
)

EMPTY_SESSION_STATS = BetaManagedAgentsSessionStats()

EMPTY_SESSION_USAGE = BetaManagedAgentsSessionUsage()

# ---------------------------------------------------------------------------
# Handler type + MARouter
# ---------------------------------------------------------------------------

Handler = Callable[[httpx.Request, re.Match[str]], httpx.Response]


@dataclass
class MARouter:
    """Minimal path-regex router for an httpx.MockTransport handler.

    Build routes with .add(), then pass .dispatch as the handler to
    build_fake_anthropic or httpx.MockTransport directly.
    """

    routes: list[tuple[str, re.Pattern[str], Handler]] = field(
        default_factory=list[tuple[str, re.Pattern[str], Handler]]
    )

    def add(self, method: str, path_re: str, handler: Handler) -> None:
        self.routes.append((method.upper(), re.compile(path_re), handler))

    def dispatch(self, request: httpx.Request) -> httpx.Response:
        for method, pattern, handler in self.routes:
            if request.method != method:
                continue
            match = pattern.fullmatch(request.url.path)
            if match is None:
                continue
            return handler(request, match)
        raise AssertionError(
            f"MARouter: no route for {request.method} {request.url.path} "
            f"(registered: {[(m, p.pattern) for m, p, _ in self.routes]})"
        )


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------


def json_body(request: httpx.Request) -> dict[str, Any]:
    """Decode the JSON body of an httpx.Request for inline assertions."""
    return json.loads(request.content.decode("utf-8")) if request.content else {}  # type: ignore[return-value]


def list_response(data: list[dict[str, Any]]) -> httpx.Response:
    """MA LIST shape: {data: [...], next_page: null}."""
    return httpx.Response(200, json={"data": data, "next_page": None})


def not_found_response(message: str) -> httpx.Response:
    """MA 404 error shape."""
    return httpx.Response(
        404,
        json={"type": "error", "error": {"type": "not_found_error", "message": message}},
    )


def sse_response(events: list[dict[str, Any]]) -> httpx.Response:
    """Build an httpx.Response that emits SSE events for the SDK's stream parser.

    Each event dict must have a 'type' key (used as the SSE event name)
    and is serialized as the SSE data line.
    """
    chunks: list[str] = []
    for event in events:
        event_type = event["type"]
        data = json.dumps(event)
        chunks.append(f"event: {event_type}\ndata: {data}\n\n")
    body = "".join(chunks)
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=body.encode(),
    )


def send_events_response(data: list[dict[str, Any]] | None = None) -> httpx.Response:
    """Response for POST /v1/sessions/{id}/events."""
    return httpx.Response(200, json={"data": data})


SessionStatus = Literal["rescheduling", "running", "idle", "terminated"]
"""The SDK's own status enum (`BetaManagedAgentsSession.status`) is inlined on
the model rather than exported as a standalone type, so this is a local
alias of the same four literals."""


def session_response(
    *,
    session_id: str,
    status: SessionStatus = "idle",
    agent_id: str | None = None,
    environment_id: str = "env_test",
) -> httpx.Response:
    """Response for GET /v1/sessions/{id} (the SDK's `beta.sessions.retrieve`).

    After plan 19-02, a driver-driven SSE script that ends without a terminal
    event makes the driver check session status, so every transport-level
    test that drives the driver needs this route registered.

    Builds a real `BetaManagedAgentsSession` and serializes with
    `.model_dump(mode="json")`, exactly as `_agent_response` /
    `_environment_response` do.
    """
    now = datetime.now(UTC).isoformat()
    session = BetaManagedAgentsSession(
        id=session_id,
        type="session",
        status=status,
        agent=BetaManagedAgentsSessionAgent(
            id=agent_id or _ma_id("agent"),
            type="agent",
            name="test-agent",
            model=BetaManagedAgentsModelConfig(id="claude-sonnet-4-6"),  # type: ignore[arg-type]
            mcp_servers=[],
            skills=[],
            tools=[],
            version=1,
        ),
        environment_id=environment_id,
        created_at=now,
        updated_at=now,
        metadata={},
        outcome_evaluations=[],
        resources=[],
        stats=EMPTY_SESSION_STATS,
        usage=EMPTY_SESSION_USAGE,
        vault_ids=[],
    )
    return httpx.Response(200, json=session.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# combine_handlers
# ---------------------------------------------------------------------------


def combine_handlers(
    *handlers: Callable[[httpx.Request], httpx.Response],
) -> Callable[[httpx.Request], httpx.Response]:
    """Combine multiple handlers into one.

    Each handler either returns an httpx.Response or raises NotHandled.
    Handlers are tried in order; the first matching handler wins.
    If no handler matches, raises AssertionError with a descriptive message.
    """

    def combined(request: httpx.Request) -> httpx.Response:
        for handler in handlers:
            try:
                return handler(request)
            except NotHandled:
                continue
        raise AssertionError(f"No handler matched {request.method} {request.url.path}")

    return combined


# ---------------------------------------------------------------------------
# AsyncAnthropic builders
# ---------------------------------------------------------------------------


def build_fake_anthropic(
    handler: Callable[[httpx.Request], httpx.Response],
) -> AsyncAnthropic:
    """Return a real AsyncAnthropic whose HTTP transport is a MockTransport.

    `handler` is required. Tests compose their own handler (or use MARouter /
    combine_handlers / make_fake_ma_handler) and pass it here. The real SDK
    code path runs in full (parameter validation, response parsing).
    """
    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport, base_url="https://api.anthropic.com")
    return AsyncAnthropic(api_key="test", http_client=http_client)


def build_stub_anthropic(
    handler: Callable[[httpx.Request], httpx.Response] | None = None,
) -> AsyncAnthropic:
    """Return a real AsyncAnthropic with an optional handler.

    Default handler returns 200 with an empty JSON body — enough for tests
    that only need `client` to type-check as `AsyncAnthropic` and never
    actually invoke a `beta.*` method. Pass a real handler for tests that
    need specific responses.
    """

    def _noop(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    return build_fake_anthropic(handler or _noop)


@pytest.fixture
def stub_anthropic() -> AsyncAnthropic:
    """AsyncAnthropic with a no-op 200 handler. Decorative; for tests that
    never call through to `beta.*`.

    Import into a package's `conftest.py` (`from daimon.testing.ma import
    stub_anthropic  # noqa: F401`) to make it discoverable by pytest.
    """
    return build_stub_anthropic()


@pytest.fixture
def make_stub_anthropic() -> Callable[
    [Callable[[httpx.Request], httpx.Response] | None], AsyncAnthropic
]:
    """Factory fixture: tests that need a custom handler call
    `make_stub_anthropic(handler)` to build an AsyncAnthropic that routes
    to it. Returned type matches `build_stub_anthropic`'s signature.

    Import into a package's `conftest.py` (`from daimon.testing.ma import
    make_stub_anthropic  # noqa: F401`) to make it discoverable by pytest.
    """
    return build_stub_anthropic


# ---------------------------------------------------------------------------
# Live-API contract-test helper
# ---------------------------------------------------------------------------


def _require_api_key() -> str:
    """Read DAIMON_TEST_ANTHROPIC_API_KEY from env, skip if missing.

    Shared by contract-test conftests (`-m contract`, env-gated, never gate
    CI) that need a real AsyncAnthropic client backed by a live API key.
    """
    key = os.environ.get("DAIMON_TEST_ANTHROPIC_API_KEY")
    if not key:
        pytest.skip("DAIMON_TEST_ANTHROPIC_API_KEY not set — contract tests skipped")
    return key


# ---------------------------------------------------------------------------
# Stateful agent CRUD handler
# ---------------------------------------------------------------------------


def _ma_id(prefix: str) -> str:
    """Mimic MA's prefixed-ID shape: e.g. ``agent_017vXaNG5P7Fu1g4orggSwEY``."""
    return f"{prefix}_{secrets.token_urlsafe(18).replace('-', '').replace('_', '')[:24]}"


def _agent_response(
    *,
    agent_id: str | None = None,
    name: str = "uat-agent",
    model: str = "claude-sonnet-4-6",
    system: str | None = None,
    metadata: dict[str, str] | None = None,
    mcp_servers: list[dict[str, object]] | None = None,
    tools: list[dict[str, object]] | None = None,
    skills: list[dict[str, object]] | None = None,
    version: int = 1,
) -> dict[str, object]:
    """Build a payload shaped like MA's BetaManagedAgentsAgent."""
    now = datetime.now(UTC).isoformat()
    return {
        "id": agent_id or _ma_id("agent"),
        "type": "agent",
        "name": name,
        "version": version,
        "model": {"id": model, "speed": "standard"},
        "system": system,
        "metadata": metadata or {},
        "mcp_servers": mcp_servers or [],
        "tools": tools or [],
        "skills": skills or [],
        "created_at": now,
        "updated_at": now,
        "archived_at": None,
        "description": None,
    }


def _environment_response(
    *,
    environment_id: str,
    name: str = "test-env",
    description: str = "",
    metadata: dict[str, str] | None = None,
) -> BetaEnvironment:
    """Build a validated BetaEnvironment using real SDK construction.

    Uses EMPTY_CLOUD_CONFIG for the config field. Serializes with
    `.model_dump(mode="json")` -- see `guideline:testing`'s validated-
    construction rule for why unvalidated shortcuts are banned here.
    """
    now = datetime.now(UTC).isoformat()
    return BetaEnvironment(
        id=environment_id,
        name=name,
        type="environment",
        config=EMPTY_CLOUD_CONFIG,
        created_at=now,
        updated_at=now,
        description=description,
        metadata=metadata or {},
    )


def _validate_mcp_toolset_crossref(payload: dict[str, Any]) -> str | None:
    """Real MA rule: every name in mcp_servers must be referenced by a
    mcp_toolset entry in tools. Return error message if violated, else None.

    Payload is raw parsed JSON from an httpx request body, so its values are
    genuinely `Any` (per guideline:typing — explicit `Any` for SDK payloads).
    """
    servers: list[dict[str, Any]] = payload.get("mcp_servers") or []
    tools: list[dict[str, Any]] = payload.get("tools") or []
    server_names = {s.get("name") for s in servers}
    referenced = {t.get("mcp_server_name") for t in tools if t.get("type") == "mcp_toolset"}
    missing = sorted(n for n in server_names if n not in referenced and isinstance(n, str))
    if missing:
        return (
            f"Agent has invalid configuration: failed to update agent: "
            f"mcp_servers {missing} declared but no mcp_toolset in tools "
            f"references them"
        )
    return None


def make_fake_ma_handler() -> Callable[[httpx.Request], httpx.Response]:
    """Stateful fake handler for MA agent CRUD.

    Tracks created agents in memory so PATCH can update them. Validates the
    mcp_servers <-> mcp_toolset cross-reference on POST and PATCH.

    Returns a plain callable (not decorated) — wrap with build_fake_anthropic
    to get an AsyncAnthropic client.
    """
    store: dict[str, dict[str, object]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method

        # GET /v1/agents — list
        if method == "GET" and path == "/v1/agents":
            return httpx.Response(200, json={"data": list(store.values()), "has_more": False})

        # GET /v1/skills — empty list (the mount-name guards list skills before
        # create/attach; tests that need real rows layer their own handler on top)
        if method == "GET" and path == "/v1/skills":
            return httpx.Response(200, json={"data": [], "next_page": None})

        # POST /v1/agents — create
        if method == "POST" and path == "/v1/agents":
            body: dict[str, Any] = json.loads(request.content)
            err = _validate_mcp_toolset_crossref(body)
            if err:
                return httpx.Response(
                    400,
                    json={
                        "type": "error",
                        "error": {"type": "invalid_request_error", "message": err},
                    },
                )
            agent = _agent_response(
                name=body.get("name", "unnamed"),
                model=body.get("model", "claude-sonnet-4-6"),
                system=body.get("system"),
                metadata=body.get("metadata", {}),
                mcp_servers=body.get("mcp_servers", []),
                tools=body.get("tools", []),
                skills=body.get("skills", []),
                version=1,
            )
            store[agent["id"]] = agent  # pyright: ignore[reportArgumentType]
            return httpx.Response(200, json=agent)

        # GET /v1/environments/{id} — retrieve single environment
        m = re.match(r"^/v1/environments/(?P<id>[^/]+)$", path)
        if m and method == "GET":
            environment_id = m.group("id")
            env = _environment_response(environment_id=environment_id)
            return httpx.Response(200, json=env.model_dump(mode="json"))

        # GET /v1/agents/{id} — retrieve single agent
        m = re.match(r"^/v1/agents/(?P<id>[^/]+)$", path)
        if m and method == "GET":
            agent_id = m.group("id")
            if agent_id not in store:
                return httpx.Response(
                    404,
                    json={
                        "type": "error",
                        "error": {"type": "not_found_error", "message": "no such agent"},
                    },
                )
            return httpx.Response(200, json=store[agent_id])

        # PATCH/POST /v1/agents/{id} — update (MA uses POST for updates)
        m = re.match(r"^/v1/agents/(?P<id>[^/]+)$", path)
        if m and method in {"PATCH", "POST"}:
            agent_id = m.group("id")
            if agent_id not in store:
                return httpx.Response(
                    404,
                    json={
                        "type": "error",
                        "error": {"type": "not_found_error", "message": "no such agent"},
                    },
                )
            body = json.loads(request.content)
            existing = store[agent_id]
            merged: dict[str, object] = {**existing, **body}
            err = _validate_mcp_toolset_crossref(merged)
            if err:
                return httpx.Response(
                    400,
                    json={
                        "type": "error",
                        "error": {"type": "invalid_request_error", "message": err},
                    },
                )
            merged["version"] = existing.get("version", 1) + 1  # pyright: ignore[reportOperatorIssue]
            store[agent_id] = merged
            return httpx.Response(200, json=merged)

        return httpx.Response(404, json={"error": f"unhandled {method} {path}"})

    return handler


# ---------------------------------------------------------------------------
# Stateful memory-store fake (agent memory feature)
# ---------------------------------------------------------------------------


def _prefix_match(path: str, prefix: str) -> bool:
    """Match path against a segment-aware prefix.

    Matches whole path segments: /notes/ matches /notes/todo.md but NOT /notes-archive/todo.md.
    """
    if prefix == "/":
        return True
    norm = prefix.rstrip("/") + "/"
    return path == prefix or path.startswith(norm)


@dataclass
class FakeMemoryStoreState:
    """In-memory state shared between a memory-store fake and test assertions."""

    stores: dict[str, dict[str, Any]] = field(default_factory=dict)
    memories: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


def _memory_store_response(
    *,
    store_id: str,
    name: str,
    description: str | None,
    metadata: dict[str, str] | None,
    archived_at: str | None = None,
) -> dict[str, Any]:
    """Payload shaped like BetaManagedAgentsMemoryStore."""
    now = datetime.now(UTC).isoformat()
    return {
        "id": store_id,
        "type": "memory_store",
        "name": name,
        "description": description,
        "metadata": metadata or {},
        "created_at": now,
        "updated_at": now,
        "archived_at": archived_at,
    }


def _memory_response(*, store_id: str, path: str, content: str) -> dict[str, Any]:
    """Payload shaped like BetaManagedAgentsMemory (memory_stores/ namespace)."""
    now = datetime.now(UTC).isoformat()
    return {
        "id": _ma_id("mem"),
        "type": "memory",
        "memory_store_id": store_id,
        "memory_version_id": _ma_id("memver"),
        "path": path,
        "content": content,
        "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
        "content_size_bytes": len(content.encode()),
        "created_at": now,
        "updated_at": now,
    }


def _memory_view(mem: dict[str, Any], view: str) -> dict[str, Any]:
    """Apply the API's view semantics: `content` is populated only for
    `view=full`; the default `basic` view nulls it (sha/size stay populated)."""
    if view == "full":
        return mem
    redacted = dict(mem)
    redacted["content"] = None
    return redacted


def make_fake_memory_store_handler(
    state: FakeMemoryStoreState | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """Stateful fake for /v1/memory_stores endpoints.

    Raises NotHandled for non-memory paths — compose with other handlers via
    combine_handlers. Covers: store create/retrieve/archive/delete, memory
    create/list/retrieve. (update/versions endpoints are out of v1 scope.)

    When combining with make_fake_ma_handler (which returns a 404 catch-all),
    pass this handler first to combine_handlers() so requests are tried here before fallthrough.
    """
    st = state if state is not None else FakeMemoryStoreState()

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method

        if method == "POST" and path == "/v1/memory_stores":
            body = json_body(request)
            store_id = _ma_id("memstore")
            store = _memory_store_response(
                store_id=store_id,
                name=str(body.get("name", "")),
                description=body.get("description"),
                metadata=body.get("metadata"),
            )
            st.stores[store_id] = store
            st.memories.setdefault(store_id, [])
            return httpx.Response(200, json=store)

        m = re.fullmatch(r"/v1/memory_stores/(?P<sid>[^/]+)/memories", path)
        if m and method == "POST":
            sid = m.group("sid")
            if sid not in st.stores:
                return not_found_response("no such store")
            body = json_body(request)
            mem = _memory_response(
                store_id=sid, path=str(body["path"]), content=str(body.get("content", ""))
            )
            st.memories[sid].append(mem)
            return httpx.Response(200, json=mem)

        if m and method == "GET":
            sid = m.group("sid")
            if sid not in st.stores:
                return not_found_response("no such store")
            prefix = request.url.params.get("path_prefix", "/")
            view = request.url.params.get("view", "basic")
            data = [
                _memory_view(x, view)
                for x in st.memories.get(sid, [])
                if _prefix_match(x["path"], prefix)
            ]
            return list_response(data)

        m = re.fullmatch(r"/v1/memory_stores/(?P<sid>[^/]+)/memories/(?P<mid>[^/]+)", path)
        if m and method == "GET":
            sid, mid = m.group("sid"), m.group("mid")
            view = request.url.params.get("view", "basic")
            for x in st.memories.get(sid, []):
                if x["id"] == mid:
                    return httpx.Response(200, json=_memory_view(x, view))
            return not_found_response("no such memory")

        m = re.fullmatch(r"/v1/memory_stores/(?P<sid>[^/]+)/archive", path)
        if m and method == "POST":
            sid = m.group("sid")
            store = st.stores.get(sid)
            if store is None:
                return not_found_response("no such store")
            store["archived_at"] = datetime.now(UTC).isoformat()
            return httpx.Response(200, json=store)

        m = re.fullmatch(r"/v1/memory_stores/(?P<sid>[^/]+)", path)
        if m and method == "DELETE":
            sid = m.group("sid")
            if sid not in st.stores:
                return not_found_response("no such store")
            del st.stores[sid]
            st.memories.pop(sid, None)
            return httpx.Response(200, json={"id": sid, "type": "memory_store_deleted"})

        if m and method == "GET":
            sid = m.group("sid")
            store = st.stores.get(sid)
            if store is None:
                return not_found_response("no such store")
            return httpx.Response(200, json=store)

        raise NotHandled

    return handler
