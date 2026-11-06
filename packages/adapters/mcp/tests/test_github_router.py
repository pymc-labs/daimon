"""The GitHub MCP router forwards each request with the one App token that serves it."""

from __future__ import annotations

import datetime as dt
import json
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

import httpx
import pytest
import structlog
from cryptography.fernet import Fernet, MultiFernet
from daimon.adapters.mcp.github_router import (
    AUDIT_TOOL_NAME,
    BATCH_NOT_SENT,
    GITHUB_ROUTER_METHODS,
    GITHUB_ROUTER_ROUTE,
    MAX_BODY_BYTES,
    CoreRouterBackend,
    Refusal,
    RoutedToken,
    RouterCaller,
    ToolCall,
    build_github_router_route,
)
from daimon.adapters.mcp.middleware.session_header import StripSessionIdMiddleware
from daimon.adapters.mcp.server import create_mcp_app
from daimon.core import github_router_tokens as router_tokens
from daimon.core.config import (
    AnthropicSettings,
    CryptoSettings,
    DatabaseSettings,
    GithubAppSettings,
    McpSettings,
    Settings,
)
from daimon.core.github_app_auth import PERMISSION_PROFILES
from daimon.core.github_router_tokens import GrantedRepo, GrantView
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_server_url import GITHUB_COPILOT_MCP_URL
from daimon.core.stores import github_issued_tokens as issued_tokens
from daimon.core.stores import github_router_sessions as router_sessions
from daimon.core.stores.security_audit import SecurityAuditRow, list_events
from daimon.core.stores.thread_sessions import create_thread_session, mark_dead
from daimon.testing.factories import make_platform_principal, make_tenant
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route

BEARER = "router-bearer-" + "b" * 32
TOKEN_ACME = "ghs_acme" + "a" * 32
TOKEN_ACME_NEW = "ghs_acmenew" + "n" * 29
TOKEN_GLOBEX = "ghs_globex" + "g" * 30
EXPIRES = dt.datetime(2026, 11, 6, 12, tzinfo=dt.UTC)
ACME, GLOBEX = 101, 202


def _token(token: str, installation_id: int, repo_ids: tuple[int, ...]) -> RoutedToken:
    return RoutedToken(
        token=token,
        token_id=uuid.uuid4(),
        installation_id=installation_id,
        repo_ids=repo_ids,
        permissions={"contents": "write"},
        expires_at=EXPIRES,
    )


@dataclass
class _Backend:
    """Two installations: owner `acme` and owner `globex`; the owner-free token is acme's."""

    caller: RouterCaller
    tokens: dict[str, RoutedToken] = field(
        default_factory=lambda: {
            "acme": _token(TOKEN_ACME, ACME, (1,)),
            "globex": _token(TOKEN_GLOBEX, GLOBEX, (2,)),
        }
    )
    choices: list[list[str]] = field(default_factory=list[list[str]])
    bearers: list[str] = field(default_factory=list[str])

    async def authenticate(self, bearer: str) -> RouterCaller | None:
        self.bearers.append(bearer)
        return self.caller if bearer == BEARER else None

    async def choose(
        self, caller: RouterCaller, calls: Sequence[ToolCall]
    ) -> RoutedToken | Refusal:
        self.choices.append([call.name for call in calls])
        if not calls:
            return self.tokens["acme"]
        owners: set[str] = set()
        for call in calls:
            owner = str(call.arguments.get("owner"))
            if owner not in self.tokens:
                # The fake has no token for this owner yet, like a mid-rotation session.
                return Refusal(router_tokens.REFRESHING_MESSAGE, "not_usable")
            owners.add(owner)
        if len(owners) > 1:
            return Refusal("These calls need two GitHub tokens. Ask for one repo at a time.",
                           "needs_one_scope")  # fmt: skip
        return self.tokens[owners.pop()]


class _Upstream:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.reject: set[str] = set()
        self.sse = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers["authorization"].removeprefix("Bearer ") in self.reject:
            return httpx.Response(401, headers={"www-authenticate": 'Bearer realm="github"'})
        if self.sse:
            return httpx.Response(
                200,
                content=b'event: message\ndata: {"jsonrpc":"2.0","method":"ping"}\n\n',
                headers={"content-type": "text/event-stream", "mcp-session-id": "gh-1"},
            )
        return httpx.Response(
            200,
            content=b'{"jsonrpc":"2.0","id":1,"result":{}}',
            headers={
                "content-type": "application/json",
                "mcp-session-id": "gh-1",
                "set-cookie": "upstream=1",
            },
        )


@dataclass
class _Router:
    client: httpx.AsyncClient
    backend: _Backend
    upstream: _Upstream
    factory: async_sessionmaker[AsyncSession]
    tenant_id: uuid.UUID

    async def post(self, body: object, **headers: str) -> httpx.Response:
        return await self.client.post(
            GITHUB_ROUTER_ROUTE,
            content=json.dumps(body),
            headers={
                "authorization": f"Bearer {BEARER}",
                "content-type": "application/json",
                "accept": "application/json, text/event-stream",
                **headers,
            },
        )

    def sent_tokens(self) -> list[str]:
        return [r.headers["authorization"].removeprefix("Bearer ") for r in self.upstream.requests]

    async def audit(self) -> list[SecurityAuditRow]:
        async with self.factory() as db:
            rows = await list_events(db, tenant_id=self.tenant_id, tool_name=AUDIT_TOOL_NAME)
        return rows


@pytest.fixture
async def router(db_session_factory: async_sessionmaker[AsyncSession]) -> _Router:
    async with db_session_factory() as db, db.begin():
        tenant = await make_tenant(db, platform="discord", workspace_id="guild-1")
    caller = RouterCaller(
        router_session_id=uuid.uuid4(),
        tenant_id=tenant.id,
        agent_id=uuid.uuid4(),
        account_id=None,
        ma_session_id="sesn_router",
    )
    backend, upstream = _Backend(caller), _Upstream()
    app = Starlette(middleware=[Middleware(StripSessionIdMiddleware)])
    app.add_route(
        GITHUB_ROUTER_ROUTE,
        build_github_router_route(
            backend=backend,
            sessionmaker=db_session_factory,
            client=httpx.AsyncClient(transport=httpx.MockTransport(upstream.handle)),
        ),
        methods=GITHUB_ROUTER_METHODS,
    )
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://d.test")
    return _Router(client, backend, upstream, db_session_factory, tenant.id)


def _call(
    message_id: int, owner: str, repo: str, tool: str = "get_file_contents"
) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "method": "tools/call",
        "params": {"name": tool, "arguments": {"owner": owner, "repo": repo, "path": "README.md"}},
    }


@pytest.mark.parametrize(
    "authorization",
    [None, "", "Bearer", "Bearer ", "Basic " + BEARER, "Bearer wrong", "Token " + BEARER],
)
async def test_missing_or_bad_bearer_is_401_without_detail(
    router: _Router, authorization: str | None
) -> None:
    headers = {} if authorization is None else {"authorization": authorization}
    response = await router.client.post(
        GITHUB_ROUTER_ROUTE, content=json.dumps(_call(1, "acme", "app")), headers=headers
    )
    assert response.status_code == 401
    assert response.content == b"", "no detail about why"
    assert "www-authenticate" not in response.headers
    assert router.upstream.requests == [], "nothing goes upstream"
    assert await router.audit() == [], "an unauthenticated request names no tenant"


async def test_a_bearer_the_store_does_not_accept_is_401(router: _Router) -> None:
    """Closed, expired and dead-session records all verify to None; see the core tests."""
    response = await router.client.get(
        GITHUB_ROUTER_ROUTE, headers={"authorization": "Bearer closed-or-expired"}
    )
    assert response.status_code == 401
    assert router.backend.bearers == ["closed-or-expired"]


async def test_each_call_goes_with_its_owner_installation_token(router: _Router) -> None:
    acme = await router.post(_call(1, "acme", "app"))
    globex = await router.post(_call(2, "globex", "site"))
    assert (acme.status_code, globex.status_code) == (200, 200)
    assert router.sent_tokens() == [TOKEN_ACME, TOKEN_GLOBEX]
    assert all(str(r.url) == GITHUB_COPILOT_MCP_URL for r in router.upstream.requests)
    rows = await router.audit()
    assert [(r.operation, r.outcome, r.reason) for r in rows] == [
        ("get_file_contents", "allowed", "routed"),
        ("get_file_contents", "allowed", "routed"),
    ]
    assert [r.github_installation_id for r in rows] == [ACME, GLOBEX]
    assert [r.github_repo_ids for r in rows] == [[1], [2]]
    assert rows[0].github_token_id == router.backend.tokens["acme"].token_id
    assert all(r.github_session_id == "sesn_router" for r in rows)


async def test_a_refusal_is_a_jsonrpc_error_and_never_goes_upstream(router: _Router) -> None:
    response = await router.post(_call(7, "initech", "secret"))
    assert response.status_code == 200
    assert response.json() == {
        "jsonrpc": "2.0",
        "id": 7,
        "error": {"code": -32001, "message": router_tokens.REFRESHING_MESSAGE},
    }
    assert router.upstream.requests == []
    (row,) = await router.audit()
    assert (row.operation, row.outcome, row.reason) == ("get_file_contents", "denied", "not_usable")
    assert row.github_token_id is None and row.github_installation_id is None


async def test_needs_one_scope_is_a_jsonrpc_error_and_never_goes_upstream(router: _Router) -> None:
    response = await router.post([_call(1, "acme", "app"), _call(2, "globex", "site")])
    assert response.status_code == 200
    assert response.json() == [
        {
            "jsonrpc": "2.0",
            "id": message_id,
            "error": {
                "code": -32001,
                "message": "These calls need two GitHub tokens. Ask for one repo at a time.",
            },
        }
        for message_id in (1, 2)
    ]
    assert router.upstream.requests == []
    rows = await router.audit()
    assert [(r.outcome, r.reason) for r in rows] == [("denied", "needs_one_scope")] * 2


async def test_a_batch_one_token_serves_goes_up_once_with_that_token(router: _Router) -> None:
    batch = [
        _call(1, "globex", "site"),
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        _call(2, "globex", "docs", tool="list_commits"),
    ]
    response = await router.post(batch)
    assert response.status_code == 200
    (sent,) = router.upstream.requests
    assert sent.headers["authorization"] == f"Bearer {TOKEN_GLOBEX}"
    assert json.loads(sent.content) == batch, "the body goes up unchanged"
    rows = await router.audit()
    assert [(r.operation, r.outcome) for r in rows] == [
        ("get_file_contents", "allowed"),
        ("list_commits", "allowed"),
    ]


async def test_a_mixed_batch_with_a_refused_call_is_not_sent(router: _Router) -> None:
    batch = [
        _call(1, "acme", "app"),
        _call(2, "initech", "secret"),
        {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
    ]
    response = await router.post(batch)
    assert response.status_code == 200
    assert router.upstream.requests == [], "no silent partial result"
    assert {m["id"]: m["error"]["message"] for m in response.json()} == {
        1: BATCH_NOT_SENT,
        2: router_tokens.REFRESHING_MESSAGE,
        3: BATCH_NOT_SENT,
    }, "every request is answered, the notification is not"
    rows = await router.audit()
    assert [(r.outcome, r.reason) for r in rows] == [
        ("denied", "batch_refused"),
        ("denied", "not_usable"),
    ]


async def test_a_malformed_tools_call_is_refused_here(router: _Router) -> None:
    message: dict[str, object] = {
        "jsonrpc": "2.0",
        "id": 4,
        "method": "tools/call",
        "params": {"arguments": {}},
    }
    response = await router.post(message)
    assert response.json()["error"]["code"] == -32602
    assert router.upstream.requests == []
    (row,) = await router.audit()
    assert (row.operation, row.outcome, row.reason) == ("<invalid>", "denied", "malformed_call")


async def test_a_body_that_is_not_jsonrpc_is_400(router: _Router) -> None:
    response = await router.client.post(
        GITHUB_ROUTER_ROUTE, content=b"{not json", headers={"authorization": f"Bearer {BEARER}"}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32700
    assert router.upstream.requests == []


@pytest.mark.parametrize("method", ["initialize", "tools/list", "ping"])
async def test_non_call_methods_use_the_owner_free_token(router: _Router, method: str) -> None:
    response = await router.post({"jsonrpc": "2.0", "id": 1, "method": method, "params": {}})
    assert response.status_code == 200
    assert router.sent_tokens() == [TOKEN_ACME]
    assert router.backend.choices == [[]], "chosen with no calls: the owner-free choice"
    assert await router.audit() == [], "only tools/call is audited"


async def test_get_sse_passes_through_with_no_read_timeout(router: _Router) -> None:
    router.upstream.sse = True
    async with router.client.stream(
        "GET",
        GITHUB_ROUTER_ROUTE,
        headers={
            "authorization": f"Bearer {BEARER}",
            "accept": "text/event-stream",
            "mcp-session-id": "gh-1",
            "last-event-id": "evt-9",
        },
    ) as response:
        body = b"".join([chunk async for chunk in response.aiter_bytes()])
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/event-stream"
    assert response.headers["mcp-session-id"] == "gh-1"
    assert body == b'event: message\ndata: {"jsonrpc":"2.0","method":"ping"}\n\n'
    (sent,) = router.upstream.requests
    assert sent.method == "GET"
    assert sent.headers["authorization"] == f"Bearer {TOKEN_ACME}", "the owner-free token"
    assert sent.headers["mcp-session-id"] == "gh-1", "kept past StripSessionIdMiddleware"
    assert sent.headers["last-event-id"] == "evt-9", "the stream resumes where it stopped"
    assert sent.extensions["timeout"]["read"] is None, "a quiet stream is not cut off"


async def test_an_upstream_401_rereads_the_rows_and_retries_once(router: _Router) -> None:
    router.upstream.reject = {TOKEN_ACME}
    stale = router.backend.tokens["acme"]

    async def rotate(caller: RouterCaller, calls: Sequence[ToolCall]) -> RoutedToken | Refusal:
        router.backend.choices.append([c.name for c in calls])
        if len(router.backend.choices) == 1:
            return stale
        return _token(TOKEN_ACME_NEW, ACME, (1,))

    router.backend.choose = rotate  # type: ignore[method-assign]
    response = await router.post(_call(1, "acme", "app"))
    assert response.status_code == 200
    assert router.sent_tokens() == [TOKEN_ACME, TOKEN_ACME_NEW]
    assert len(router.backend.choices) == 2, "the rows are read again, once"
    (row,) = await router.audit()
    assert (row.outcome, row.reason) == ("allowed", "routed")
    assert row.github_token_id != stale.token_id, "the audit names the token that was used"


async def test_a_second_upstream_401_is_not_retried_again(router: _Router) -> None:
    router.upstream.reject = {TOKEN_ACME}
    response = await router.post(_call(1, "acme", "app"))
    assert response.status_code == 502, "our caller's bearer is fine; GitHub refused ours"
    assert router.sent_tokens() == [TOKEN_ACME, TOKEN_ACME]
    (row,) = await router.audit()
    assert row.reason == "upstream_unauthorized"


async def test_a_body_over_the_cap_is_413_and_not_sent(router: _Router) -> None:
    response = await router.client.post(
        GITHUB_ROUTER_ROUTE,
        content=b"x" * (MAX_BODY_BYTES + 1),
        headers={"authorization": f"Bearer {BEARER}", "content-type": "application/json"},
    )
    assert response.status_code == 413
    assert router.upstream.requests == []
    assert router.backend.choices == []


async def test_only_allowlisted_headers_go_upstream(router: _Router) -> None:
    response = await router.post(
        _call(1, "acme", "app"),
        **{
            "mcp-session-id": "gh-1",
            "mcp-protocol-version": "2025-06-18",
            "x-mcp-toolsets": "repos,issues",
            "x-mcp-readonly": "true",
            "x-mcp-lockdown": "true",
            "x-mcp-unknown": "1",
            "cookie": "daimon=1",
            "x-forwarded-for": "10.0.0.1",
        },
    )
    assert response.status_code == 200
    (sent,) = router.upstream.requests
    assert sent.headers["authorization"] == f"Bearer {TOKEN_ACME}"
    assert BEARER not in json.dumps(dict(sent.headers)), "the router bearer stays here"
    forwarded = set(sent.headers) - {"host", "content-length", "accept-encoding", "connection",
                                     "user-agent"}  # fmt: skip
    assert forwarded == {
        "authorization",
        "accept",
        "content-type",
        "mcp-session-id",
        "mcp-protocol-version",
        "x-mcp-toolsets",
        "x-mcp-readonly",
        "x-mcp-lockdown",
    }
    assert "set-cookie" not in response.headers, "only allowlisted headers come back"
    assert response.headers["mcp-session-id"] == "gh-1"


async def test_no_token_or_bearer_reaches_the_logs(
    router: _Router, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    router.upstream.reject = {TOKEN_ACME}
    with structlog.testing.capture_logs() as logs:
        await router.post(_call(1, "acme", "app"))
        await router.post(_call(2, "globex", "site"))
        await router.post(_call(3, "initech", "secret"))
        await router.client.get(GITHUB_ROUTER_ROUTE, headers={"authorization": "Bearer nope"})
    assert logs, "the router does log its requests"
    for secret in (BEARER, TOKEN_ACME, TOKEN_GLOBEX, "README.md"):
        assert secret not in caplog.text and secret not in repr(logs)


# --- through the core store and resolver ------------------------------------------

FERNET = MultiFernet([Fernet(Fernet.generate_key())])
ACME_REPO, GLOBEX_REPO = 1, 2


@dataclass
class _Core:
    router: _Router
    record_id: uuid.UUID
    bearer: str
    mapping_id: uuid.UUID
    agent_id: uuid.UUID
    clock: list[dt.datetime]

    async def post(self, body: object) -> httpx.Response:
        return await self.router.client.post(
            GITHUB_ROUTER_ROUTE,
            content=json.dumps(body),
            headers={"authorization": f"Bearer {self.bearer}", "content-type": "application/json"},
        )


async def _deliver(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    record_id: uuid.UUID,
    installation_id: int,
    repo_id: int,
    token: str,
    now: dt.datetime,
) -> uuid.UUID:
    row = await issued_tokens.create_pending(
        db, tenant_id=tenant_id, agent_id=agent_id, session_id="sesn_core",
        installation_id=installation_id, repo_ids=[repo_id],
        permissions=dict(PERMISSION_PROFILES["write"]),
        grant_versions={f"grant:{repo_id}": 1, f"authorization:{repo_id}": 1},
        expires_at=now + dt.timedelta(hours=1), router_session_id=record_id,
    )  # fmt: skip
    await issued_tokens.store_token(db, token_id=row.token_id, token=token, fernet=FERNET)
    await issued_tokens.mark_delivered(db, token_id=row.token_id)
    return row.token_id


@pytest.fixture
async def core(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> _Core:
    now = dt.datetime.now(dt.UTC)
    async with db_session_factory() as db, db.begin():
        tenant = await make_tenant(db, platform="discord", workspace_id="guild-2")
        principal = await make_platform_principal(
            db, platform="discord", external_id="user-2", tenant=tenant
        )
        agent_id = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="agent_r")
        record, bearer = await router_sessions.create(
            db, tenant_id=tenant.id, agent_id=agent_id, account_id=principal.account_id,
            expires_at=now + dt.timedelta(hours=2), ma_session_id="sesn_core",
        )  # fmt: skip
        mapping_id = uuid.uuid4()
        await create_thread_session(
            db, id=mapping_id, tenant_id=tenant.id, platform="discord", thread_id="thread-r",
            account_id=principal.account_id, ma_session_id="sesn_core", ma_agent_id="agent_r",
        )  # fmt: skip
        for installation_id, repo_id, token in (
            (ACME, ACME_REPO, TOKEN_ACME),
            (GLOBEX, GLOBEX_REPO, TOKEN_GLOBEX),
        ):
            await _deliver(
                db, tenant_id=tenant.id, agent_id=agent_id, record_id=record.id,
                installation_id=installation_id, repo_id=repo_id, token=token, now=now,
            )  # fmt: skip
    grants = GrantView(
        (
            GrantedRepo(ACME_REPO, "acme/app", ACME, "write"),
            GrantedRepo(GLOBEX_REPO, "globex/site", GLOBEX, "write"),
        ),
        working_repo_id=ACME_REPO,
    )

    async def grant_view(
        session: AsyncSession, *, tenant_id: uuid.UUID, agent_id: uuid.UUID
    ) -> GrantView:
        return grants

    # The grant view's own reads are covered by the core resolver tests.
    monkeypatch.setattr(router_tokens, "load_grant_view", grant_view)
    clock = [now]
    upstream = _Upstream()
    app = Starlette(middleware=[Middleware(StripSessionIdMiddleware)])
    app.add_route(
        GITHUB_ROUTER_ROUTE,
        build_github_router_route(
            backend=CoreRouterBackend(
                sessionmaker=db_session_factory, fernet=FERNET, clock=lambda: clock[0]
            ),
            sessionmaker=db_session_factory,
            client=httpx.AsyncClient(transport=httpx.MockTransport(upstream.handle)),
        ),
        methods=GITHUB_ROUTER_METHODS,
    )
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://d.test")
    caller = RouterCaller(record.id, tenant.id, agent_id, principal.account_id, "sesn_core")
    routed = _Router(client, _Backend(caller), upstream, db_session_factory, tenant.id)
    return _Core(routed, record.id, bearer, mapping_id, agent_id, clock)


async def test_a_live_session_bearer_is_accepted_and_its_use_recorded(core: _Core) -> None:
    response = await core.post({"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert response.status_code == 200
    async with core.router.factory() as db:
        record = await router_sessions.get(db, router_session_id=core.record_id)
    assert record is not None and record.last_used_at == core.clock[0]


async def test_a_closed_session_bearer_is_401(core: _Core) -> None:
    async with core.router.factory() as db, db.begin():
        await router_sessions.close_local(db, router_session_id=core.record_id)
    assert (await core.post({"jsonrpc": "2.0", "id": 1, "method": "ping"})).status_code == 401
    assert core.router.upstream.requests == []


async def test_an_expired_session_bearer_is_401(core: _Core) -> None:
    core.clock[0] += dt.timedelta(hours=3)
    assert (await core.post({"jsonrpc": "2.0", "id": 1, "method": "ping"})).status_code == 401
    assert core.router.upstream.requests == []


async def test_a_dead_session_bearer_is_401(core: _Core) -> None:
    async with core.router.factory() as db, db.begin():
        await mark_dead(db, id=core.mapping_id)
    assert (await core.post({"jsonrpc": "2.0", "id": 1, "method": "ping"})).status_code == 401
    assert core.router.upstream.requests == []


async def test_the_resolver_routes_each_owner_to_its_installation(core: _Core) -> None:
    assert (await core.post(_call(1, "acme", "app"))).status_code == 200
    assert (await core.post(_call(2, "GLOBEX", "Site"))).status_code == 200
    assert (await core.post({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})).status_code == 200
    assert core.router.sent_tokens() == [TOKEN_ACME, TOKEN_GLOBEX, TOKEN_ACME], (
        "tools/list takes the working repo's token"
    )
    rows = await core.router.audit()
    assert [r.github_installation_id for r in rows] == [ACME, GLOBEX]


async def test_an_ungranted_repo_goes_up_with_the_owner_free_token(core: _Core) -> None:
    # The direct path reads public repos with the working token; GitHub keeps
    # a private ungranted repo a 404 within that token's scope.
    response = await core.post(_call(1, "pymc-devs", "pymc-marketing"))
    assert response.status_code == 200
    assert core.router.sent_tokens() == [TOKEN_ACME], "the working repo's token"


async def test_the_resolver_refusals_reach_the_agent_unchanged(core: _Core) -> None:
    both = await core.post([_call(2, "acme", "app"), _call(3, "globex", "site")])
    unknown = await core.post(_call(4, "acme", "app", tool="brand_new_tool"))
    assert [m["error"]["message"] for m in both.json()] == [router_tokens.ONE_SCOPE_MESSAGE] * 2
    assert unknown.json()["error"]["message"] == (
        router_tokens.UNSUPPORTED_MESSAGE.format(tool="brand_new_tool")
    )
    assert core.router.upstream.requests == []
    rows = await core.router.audit()
    assert [r.reason for r in rows] == [
        "needs_one_scope",
        "needs_one_scope",
        "unsupported_call",
    ]


async def test_a_rotation_during_a_call_is_found_by_the_retry(core: _Core) -> None:
    factory = core.router.factory

    async def rotate_then_reject(request: httpx.Request) -> httpx.Response:
        token = request.headers["authorization"].removeprefix("Bearer ")
        core.router.upstream.requests.append(request)
        if token != TOKEN_ACME:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})
        async with factory() as db, db.begin():
            current = await issued_tokens.list_router_session_tokens(
                db, router_session_id=core.record_id, valid_after=core.clock[0]
            )
            new_id = await _deliver(
                db, tenant_id=core.router.tenant_id, agent_id=core.agent_id,
                record_id=core.record_id, installation_id=ACME, repo_id=ACME_REPO,
                token=TOKEN_ACME_NEW, now=core.clock[0],
            )  # fmt: skip
            # The rotation supersedes the old ACME row; GLOBEX's row stays current.
            kept = {row.token_id for row in current if row.installation_id != ACME}
            await issued_tokens.mark_session_tokens_superseded(
                db, session_id="sesn_core", except_ids=frozenset({new_id, *kept}),
                now=core.clock[0],
            )  # fmt: skip
        return httpx.Response(401)

    route = build_github_router_route(
        backend=CoreRouterBackend(sessionmaker=factory, fernet=FERNET, clock=lambda: core.clock[0]),
        sessionmaker=factory,
        client=httpx.AsyncClient(transport=httpx.MockTransport(rotate_then_reject)),
    )
    app = Starlette()
    app.add_route(GITHUB_ROUTER_ROUTE, route, methods=GITHUB_ROUTER_METHODS)
    core.router.client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://d.test"
    )
    response = await core.post(_call(1, "acme", "app"))
    assert response.status_code == 200
    assert core.router.sent_tokens() == [TOKEN_ACME, TOKEN_ACME_NEW]


# --- mounting ---------------------------------------------------------------------


def _app_settings(*, app: bool, keys: bool) -> Settings:
    return Settings(
        database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
        anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
        mcp=McpSettings(jwt_secret=SecretStr("a" * 32), public_url=HttpUrl("https://x/mcp")),
        github_app=(
            GithubAppSettings(app_id="123456", private_key=SecretStr("stub-pem-not-parsed"))
            if app
            else GithubAppSettings()
        ),
        crypto=CryptoSettings(keys=(SecretStr(Fernet.generate_key().decode()),) if keys else ()),
    )


@pytest.mark.parametrize(
    ("app", "keys", "mounted"),
    [(True, True, True), (True, False, False), (False, True, False)],
)
async def test_the_route_mounts_with_the_app_and_crypto_keys(
    sessionmaker: async_sessionmaker[AsyncSession], app: bool, keys: bool, mounted: bool
) -> None:
    built = create_mcp_app(
        settings=_app_settings(app=app, keys=keys),
        sessionmaker=sessionmaker,
        auth=StaticTokenVerifier(tokens={}),
    )
    paths = [r.path for r in built.routes if isinstance(r, Route)]
    assert (GITHUB_ROUTER_ROUTE in paths) is mounted
