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
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaEnvironment, BetaManagedAgentsAgent, BetaManagedAgentsSession
from anthropic.types.beta.beta_managed_agents_model_config import BetaManagedAgentsModelConfig
from daimon.testing.ma_models import (
    DEFAULT_MODEL_ID,
    ma_agent,
    ma_environment,
    ma_session,
)
from daimon.testing.ma_models import EMPTY_CLOUD_CONFIG as EMPTY_CLOUD_CONFIG
from daimon.testing.ma_models import EMPTY_SESSION_STATS as EMPTY_SESSION_STATS
from daimon.testing.ma_models import EMPTY_SESSION_USAGE as EMPTY_SESSION_USAGE
from daimon.testing.ma_models import SessionStatus as SessionStatus

# ---------------------------------------------------------------------------
# Sentinel
# ---------------------------------------------------------------------------


class NotHandled(Exception):
    """Raise from a handler passed to combine_handlers to indicate this handler
    does not match the request. combine_handlers will try the next handler."""


# The shared `EMPTY_*` constants and `SessionStatus` live in
# `daimon.testing.ma_models` and are re-exported above for existing importers.

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

    def add_agent(self, agent: BetaManagedAgentsAgent) -> None:
        """Serve `GET /v1/agents/{agent.id}` (the exact id only)."""
        body = agent.model_dump(mode="json")
        self.add("GET", rf"/v1/agents/{re.escape(agent.id)}", lambda _r, _m: _json_200(body))

    def add_environment(self, environment: BetaEnvironment) -> None:
        """Serve `GET /v1/environments/{environment.id}` (the exact id only)."""
        body = environment.model_dump(mode="json")
        self.add(
            "GET",
            rf"/v1/environments/{re.escape(environment.id)}",
            lambda _r, _m: _json_200(body),
        )

    def add_session(self, session: BetaManagedAgentsSession) -> None:
        """Serve `GET /v1/sessions/{session.id}` (the exact id only)."""
        body = session.model_dump(mode="json")
        self.add("GET", rf"/v1/sessions/{re.escape(session.id)}", lambda _r, _m: _json_200(body))

    def add_agent_list(self, *agents: BetaManagedAgentsAgent) -> None:
        """Serve `GET /v1/agents` with exactly these agents (the resolver's tag lookup)."""
        items = [agent.model_dump(mode="json") for agent in agents]
        self.add("GET", r"/v1/agents", lambda _r, _m: list_response(items))

    def add_environment_list(self, *environments: BetaEnvironment) -> None:
        """Serve `GET /v1/environments` with exactly these environments."""
        items = [environment.model_dump(mode="json") for environment in environments]
        self.add("GET", r"/v1/environments", lambda _r, _m: list_response(items))

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


def _json_200(body: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json=body)


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


def session_response(
    *,
    session_id: str,
    status: SessionStatus = "idle",
    agent_id: str | None = None,
    environment_id: str = "env_test",
    metadata: dict[str, str] | None = None,
) -> httpx.Response:
    """Response for GET /v1/sessions/{id} (the SDK's `beta.sessions.retrieve`).

    After plan 19-02, a driver-driven SSE script that ends without a terminal
    event makes the driver check session status, so every transport-level
    test that drives the driver needs this route registered.

    Built on `ma_session`; a fresh `agent_...` id is minted when `agent_id`
    is omitted, as a live create would.
    """
    session = ma_session(
        id=session_id,
        status=status,
        agent_id=agent_id or _ma_id("agent"),
        environment_id=environment_id,
        metadata=metadata,
        created_at=datetime.now(UTC),
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


def build_no_retry_anthropic(
    handler: Callable[[httpx.Request], httpx.Response] | MARouter,
) -> AsyncAnthropic:
    """`build_fake_anthropic` with the SDK's own retries disabled.

    The SDK auto-retries 409/429/5xx (max_retries=2), which would consume a
    scripted conflict before the code under test can see it. A `MARouter`
    is accepted directly so callers need not spell `.dispatch`.
    """
    dispatch = handler.dispatch if isinstance(handler, MARouter) else handler
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(dispatch), base_url="https://api.anthropic.com"
    )
    return AsyncAnthropic(api_key="test", http_client=http_client, max_retries=0)


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


def require_api_key() -> str:
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
    model: str = DEFAULT_MODEL_ID,
    system: str | None = None,
    metadata: dict[str, str] | None = None,
    mcp_servers: list[dict[str, object]] | None = None,
    tools: list[dict[str, object]] | None = None,
    skills: list[dict[str, object]] | None = None,
    version: int = 1,
) -> dict[str, object]:
    """Build a payload shaped like MA's BetaManagedAgentsAgent.

    The frame (ids, timestamps, model, metadata) comes from `ma_agent`. The
    `tools` / `mcp_servers` / `skills` lists are echoed back verbatim, the
    way `make_fake_ma_handler` returns whatever a create/update sent: those
    arrive in the SDK's *request* shape (`default_config` optional, no
    resolved `enabled` flags), which the response models would reject.
    """
    now = datetime.now(UTC)
    payload: dict[str, object] = ma_agent(
        id=agent_id or _ma_id("agent"),
        name=name,
        model=BetaManagedAgentsModelConfig(id=model, speed="standard"),
        system=system,
        metadata=metadata,
        version=version,
        created_at=now,
    ).model_dump(mode="json")
    payload["mcp_servers"] = mcp_servers or []
    payload["tools"] = tools or []
    payload["skills"] = skills or []
    return payload


def _environment_response(
    *,
    environment_id: str,
    name: str = "test-env",
    description: str = "",
    metadata: dict[str, str] | None = None,
) -> BetaEnvironment:
    """A validated BetaEnvironment with fresh timestamps (see `ma_environment`)."""
    now = datetime.now(UTC).isoformat()
    return ma_environment(
        id=environment_id,
        name=name,
        description=description,
        metadata=metadata,
        created_at=now,
    )


def make_archive_agent_handler(
    *, name: str = "doomed", model: str = DEFAULT_MODEL_ID
) -> Callable[[httpx.Request], httpx.Response]:
    """Handle `POST /v1/agents/{id}/archive`, which `make_fake_ma_handler`
    does not implement. Raises `NotHandled` otherwise, so it composes via
    `combine_handlers` in front of the agent fake."""

    def handler(request: httpx.Request) -> httpx.Response:
        m = re.fullmatch(r"/v1/agents/(?P<id>[^/]+)/archive", request.url.path)
        if request.method != "POST" or not m:
            raise NotHandled
        now = datetime.now(UTC)
        archived = ma_agent(
            id=m.group("id"),
            name=name,
            model=BetaManagedAgentsModelConfig(id=model, speed="standard"),
            version=2,
            created_at=now,
            archived_at=now,
        )
        return httpx.Response(200, json=archived.model_dump(mode="json"))

    return handler


def make_agent_env_echo_handler(
    *, tenant_id: uuid.UUID | str | None = None, with_session: bool = True
) -> Callable[[httpx.Request], httpx.Response]:
    """Answer agent / environment (and, by default, session) retrieves for
    whatever id was asked, built on the canonical `ma_*` shapes.

    Covers the endpoints a thread turn hits when it creates an MA session
    (`GET /v1/agents/{id}`, `GET /v1/environments/{id}`) and the session
    read a mapping row without a recorded configuration triggers
    (`GET /v1/sessions/{id}`, unless `with_session=False`). Any other
    request fails loudly with an AssertionError naming it.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        m = re.fullmatch(r"/v1/agents/(?P<id>[^/]+)", path)
        if m and request.method == "GET":
            agent = ma_agent(id=m.group("id"), tenant_id=tenant_id)
            return httpx.Response(200, json=agent.model_dump(mode="json"))
        m = re.fullmatch(r"/v1/environments/(?P<id>[^/]+)", path)
        if m and request.method == "GET":
            environment = ma_environment(id=m.group("id"), tenant_id=tenant_id)
            return httpx.Response(200, json=environment.model_dump(mode="json"))
        m = re.fullmatch(r"/v1/sessions/(?P<id>[^/]+)", path)
        if with_session and m and request.method == "GET":
            session = ma_session(id=m.group("id"))
            return httpx.Response(200, json=session.model_dump(mode="json"))
        raise AssertionError(f"make_agent_env_echo_handler: unhandled {request.method} {path}")

    return handler


def resolved_agent_env_router(
    agent: BetaManagedAgentsAgent | None = None,
    environment: BetaEnvironment | None = None,
    *,
    tenant_id: uuid.UUID | str | None = None,
    router: MARouter | None = None,
) -> MARouter:
    """A router whose list + retrieve routes resolve one agent and one
    environment: what admission needs to pick a responder for `tenant_id`.

    Defaults to `ma_agent(tenant_id=...)` / `ma_environment(tenant_id=...)`
    so the resolver's tag lookup (`daimon_tenant` + `daimon_name`) finds
    them. Retrieve routes are exact-id (`MARouter.add_agent` /
    `add_environment`): a retrieve of any other id fails loudly. Pass
    `router=` to add these routes to an existing router.
    """
    resolved_agent = agent if agent is not None else ma_agent(tenant_id=tenant_id)
    resolved_environment = (
        environment if environment is not None else ma_environment(tenant_id=tenant_id)
    )
    target = router if router is not None else MARouter()
    target.add_agent_list(resolved_agent)
    target.add_agent(resolved_agent)
    target.add_environment_list(resolved_environment)
    target.add_environment(resolved_environment)
    return target


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


@dataclass
class FakeMAState:
    """In-memory agent store shared between the MA agent-CRUD fake and other
    fakes that need to read agent specs (e.g. the sessions fake freezing an
    agent snapshot at session-create time).

    Extracted from `make_fake_ma_handler`'s local closure so it can be
    injected and shared — `make_fake_ma_handler(state=None)` keeps today's
    behaviour (a private, handler-local store) unchanged.

    `agent_versions` is a full history keyed by (agent_id, version number):
    `agents` alone only ever holds the LATEST version (each `agents.update`
    overwrites it in place), so a caller pinning an explicit prior version
    (e.g. `make_fake_sessions_handler`'s session-create, which accepts
    `{"type": "agent", "id": ..., "version": N}`) needs the historical
    snapshot, not whatever is current.
    """

    agents: dict[str, dict[str, object]] = field(default_factory=dict[str, dict[str, object]])
    agent_versions: dict[str, dict[int, dict[str, object]]] = field(
        default_factory=dict[str, dict[int, dict[str, object]]]
    )


def make_fake_ma_handler(
    state: FakeMAState | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """Stateful fake handler for MA agent CRUD.

    Tracks created agents in memory so PATCH can update them. Validates the
    mcp_servers <-> mcp_toolset cross-reference on POST and PATCH.

    Pass a `FakeMAState` to share the agent store with another fake (e.g.
    `make_fake_sessions_handler`, which reads agent specs at session-create
    time to freeze a session's agent snapshot). Omit it for today's
    behaviour: a private store scoped to this handler.

    Returns a plain callable (not decorated) — wrap with build_fake_anthropic
    to get an AsyncAnthropic client.
    """
    st = state if state is not None else FakeMAState()
    store = st.agents

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
            # A create may spell `model` as the config dict form
            # (`{"id": ..., "speed": ...}`, what a fork copies off a live
            # agent) or as the bare id string; the response carries the id.
            match body.get("model", DEFAULT_MODEL_ID):
                case {"id": str(model_id)} | str(model_id):
                    pass
                case other:
                    raise AssertionError(f"make_fake_ma_handler: unexpected model {other!r}")
            agent = _agent_response(
                name=body.get("name", "unnamed"),
                model=model_id,
                system=body.get("system"),
                metadata=body.get("metadata", {}),
                mcp_servers=body.get("mcp_servers", []),
                tools=body.get("tools", []),
                skills=body.get("skills", []),
                version=1,
            )
            store[agent["id"]] = agent  # pyright: ignore[reportArgumentType]
            st.agent_versions.setdefault(str(agent["id"]), {})[1] = dict(agent)
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
            st.agent_versions.setdefault(agent_id, {})[merged["version"]] = dict(merged)  # pyright: ignore[reportArgumentType]
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

    stores: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    memories: dict[str, list[dict[str, Any]]] = field(
        default_factory=dict[str, list[dict[str, Any]]]
    )


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
