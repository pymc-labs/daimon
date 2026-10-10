"""A disposable, authenticated Daimon MCP host for live default-capability runs.

Built for one QA run and thrown away. Nothing in it touches an operator's
configuration, a production database or a real provider:

- Settings are built in a scrubbed environment with no `.env`, and every key
  is fresh for the run (MCP signing secret, gate bearer).
- The database is a run-owned schema (`qa_mcp_<run>`) in a database whose name
  says `qa` or `test`, seeded with one synthetic tenant, account and agent.
- The Anthropic client is a `MockTransport` that answers only the seeded
  agent, environment and session reads and refuses anything else: no egress.
- Only `describe_agent` and `list_events` exist on the server.

In front of the Daimon app sits `McpGate`, the only thing a tunnel may expose.
It serves `POST /mcp` alone, requires the run's bearer (constant-time
compare, minutes-long expiry, file-backed revocation), refuses batches and
any `tools/call` outside the allowlist before dispatch, and swaps the bearer
for the run's revocable Daimon agent token, so a provider never holds a
Daimon credential.

Everything a run creates is listed in its manifest; `cleanup` revokes first
(the bearer, then the Daimon token), then drops only the manifest's schema.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import hmac
import ipaddress
import json
import os
import re
import secrets
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit, urlunsplit

import httpx
from anthropic import AsyncAnthropic
from daimon.core.defaults.metadata import MA_METADATA_KEY_ACCOUNT
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_auth import mint_agent_mcp_token
from daimon.core.stores.mcp_tokens import revoke_mcp_token
from daimon.testing.db import (
    _create_schema_with_tables,  # pyright: ignore[reportPrivateUsage]
    _drop_schema,  # pyright: ignore[reportPrivateUsage]
    build_test_engine,
)
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import MARouter, build_fake_anthropic, list_response
from daimon.testing.ma_models import ma_agent, ma_environment, ma_session
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from starlette.types import ASGIApp, Message, Receive, Scope, Send

ALLOWED_TOOLS: tuple[str, ...] = ("describe_agent", "list_events")
QA_AGENT_ID = "agent_qa_synthetic"
QA_ENVIRONMENT_ID = "env_qa_synthetic"
QA_SESSION_ID = "sesn_qa_synthetic"
DEFAULT_ROOT = Path("/tmp/daimon-qa-mcp")
MAX_BODY_BYTES = 256 * 1024
_SCRUBBED_PREFIXES = ("DAIMON_", "STRIPE_", "MCP_", "ANTHROPIC_", "OPENAI_", "GEMINI_", "GOOGLE_")

# Bounded, synthetic read data: what `list_events` returns for the seeded session.
QA_EVENTS: tuple[dict[str, Any], ...] = (
    {
        "id": "sevt_qa_user",
        "type": "user.message",
        "content": [{"type": "text", "text": "QA synthetic question"}],
        "processed_at": "2026-10-10T00:00:00Z",
    },
    {
        "id": "sevt_qa_agent",
        "type": "agent.message",
        "content": [{"type": "text", "text": "QA synthetic answer"}],
        "processed_at": "2026-10-10T00:00:01Z",
    },
    {
        "id": "sevt_qa_idle",
        "type": "session.status_idle",
        "stop_reason": {"type": "end_turn", "event_ids": []},
        "processed_at": "2026-10-10T00:00:02Z",
    },
)


class QaHostError(RuntimeError):
    """The host refuses a configuration that could reach something real."""


@dataclass
class Manifest:
    """Everything one run created, and so everything its cleanup may remove."""

    run_id: str
    created_at: str
    expires_at: str
    host: str
    port: int
    database: str
    schema: str
    tenant_id: str
    token_jti: str | None = None
    tunnel_pid: int | None = None
    tunnel_url: str | None = None
    tunnel_lead_go: str | None = None
    # The host the built app believes it serves: proof no ambient setting leaked in.
    public_url_host: str | None = None
    status: str = "created"
    refused_egress: list[str] = field(default_factory=list[str])

    def path(self, root: Path) -> Path:
        return root / self.run_id / "manifest.json"

    def save(self, root: Path) -> Path:
        path = self.path(root)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2) + "\n")
        return path

    @classmethod
    def load(cls, path: Path) -> Manifest:
        return cls(**json.loads(path.read_text()))


@dataclass(frozen=True)
class RunBearer:
    """The run's gate credential: random, short-lived, revocable by a marker file."""

    token: str
    expires_at: dt.datetime
    revoked_marker: Path

    def accepts(self, presented: str | None, now: dt.datetime) -> bool:
        if presented is None or self.revoked_marker.exists() or now >= self.expires_at:
            return False
        return hmac.compare_digest(presented.encode(), self.token.encode())

    def revoke(self) -> None:
        self.revoked_marker.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.revoked_marker.touch()


def _sanitized(database_url: str) -> str:
    parts = urlsplit(database_url)
    host = parts.hostname or ""
    netloc = host + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def check_database(database_url: str) -> None:
    """Refuse anything but a local database whose name marks it disposable."""
    parts = urlsplit(database_url)
    name = parts.path.lstrip("/")
    if "qa" not in name and "test" not in name:
        raise QaHostError(f"database {name!r} is not marked qa/test")
    if parts.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise QaHostError(f"database host {parts.hostname!r} is not local")


def check_loopback(host: str) -> None:
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        raise QaHostError(f"the MCP origin must bind a loopback address, not {host!r}")


@contextmanager
def scrubbed_environment() -> Iterator[None]:
    """Hide every provider, billing and Daimon variable while settings are built."""
    saved = {k: v for k, v in os.environ.items() if k.startswith(_SCRUBBED_PREFIXES)}
    for key in saved:
        del os.environ[key]
    try:
        yield
    finally:
        os.environ.update(saved)


def synthetic_anthropic(
    tenant_id: uuid.UUID, account_id: uuid.UUID, refused: list[str]
) -> AsyncAnthropic:
    """An `AsyncAnthropic` that serves the seeded reads and refuses everything else."""
    router = MARouter()
    agent = ma_agent(
        id=QA_AGENT_ID,
        name="qa-agent",
        metadata={"daimon_tenant": str(tenant_id), "daimon_name": "qa-agent"},
    ).model_dump(mode="json")
    environment = ma_environment(id=QA_ENVIRONMENT_ID, name="qa-env").model_dump(mode="json")
    session = ma_session(
        id=QA_SESSION_ID,
        agent_id=QA_AGENT_ID,
        environment_id=QA_ENVIRONMENT_ID,
        status="idle",
        metadata={MA_METADATA_KEY_ACCOUNT: str(account_id)},
    ).model_dump(mode="json")
    router.add("GET", r"/v1/agents$", lambda _r, _m: list_response([agent]))
    router.add("GET", r"/v1/environments$", lambda _r, _m: list_response([environment]))
    router.add(
        "GET", rf"/v1/sessions/{QA_SESSION_ID}$", lambda _r, _m: httpx.Response(200, json=session)
    )
    router.add(
        "GET",
        rf"/v1/sessions/{QA_SESSION_ID}/events$",
        lambda _r, _m: httpx.Response(200, json={"data": list(QA_EVENTS), "next_page": None}),
    )

    def dispatch(request: httpx.Request) -> httpx.Response:
        try:
            return router.dispatch(request)
        except AssertionError:
            refused.append(f"{request.method} {request.url.path}")
            return httpx.Response(
                403,
                json={
                    "type": "error",
                    "error": {"type": "permission_error", "message": "qa host: no egress"},
                },
            )

    return build_fake_anthropic(dispatch)


class McpGate:
    """The only surface a tunnel may expose: `POST /mcp`, the run bearer, two tools."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        bearer: RunBearer,
        daimon_token: str,
        allowed_tools: tuple[str, ...] = ALLOWED_TOOLS,
        clock: Any = None,  # noqa: ANN401 - a zero-argument callable returning an aware datetime
    ) -> None:
        self._app = app
        self._bearer = bearer
        self._daimon_token = daimon_token
        self._allowed = frozenset(allowed_tools)
        self._clock = clock or (lambda: dt.datetime.now(dt.UTC))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self._app(scope, receive, send)
            return
        if scope["type"] != "http":
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["path"] not in ("/mcp", "/mcp/"):
            await _reply(send, 404, {"error": "not found"})
            return
        if scope["method"] != "POST":
            await _reply(send, 405, {"error": "method not allowed"})
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        authorization = headers.get("authorization", "")
        presented = authorization[7:] if authorization.lower().startswith("bearer ") else None
        if not self._bearer.accepts(presented, self._clock()):
            await _reply(send, 401, {"error": "unauthorized"})
            return
        body = await _read_body(receive)
        if body is None:
            await _reply(send, 413, {"error": "request too large"})
            return
        refusal = self._refusal(body)
        if refusal is not None:
            await _reply(send, 403, refusal)
            return
        forwarded = [
            (k, v) for k, v in scope["headers"] if k.lower() not in (b"authorization", b"cookie")
        ]
        forwarded.append((b"authorization", f"Bearer {self._daimon_token}".encode()))
        inner_scope = {**scope, "headers": forwarded, "path": "/mcp", "raw_path": b"/mcp"}

        delivered = False

        async def replay() -> Message:
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}

        await self._app(inner_scope, replay, send)

    def _refusal(self, body: bytes) -> dict[str, Any] | None:
        try:
            message = json.loads(body)
        except ValueError:
            return {"error": "invalid json"}
        if not isinstance(message, dict):
            return {"error": "batch requests are not accepted"}
        message = cast(dict[str, Any], message)
        if message.get("method") == "tools/call":
            params: object = message.get("params")
            name = cast(dict[str, Any], params).get("name") if isinstance(params, dict) else None
            if name not in self._allowed:
                return {
                    "jsonrpc": "2.0",
                    "id": message.get("id"),
                    "error": {"code": -32601, "message": f"tool {name!r} is not available"},
                }
        return None


async def _read_body(receive: Receive) -> bytes | None:
    chunks: list[bytes] = []
    size = 0
    while True:
        message = await receive()
        chunk = message.get("body", b"")
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            return None
        chunks.append(chunk)
        if not message.get("more_body", False):
            return b"".join(chunks)


async def _reply(send: Send, status: int, payload: Mapping[str, Any]) -> None:
    body = json.dumps(payload).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": body})


@dataclass
class QaHost:
    """A built host: the gate to serve, its bearer and manifest, and its engine."""

    gate: McpGate
    bearer: RunBearer
    manifest: Manifest
    root: Path
    engine: AsyncEngine
    tool_names: tuple[str, ...]


async def build_host(
    *,
    database_url: str,
    host: str = "127.0.0.1",
    port: int = 8765,
    ttl: dt.timedelta = dt.timedelta(minutes=30),
    root: Path = DEFAULT_ROOT,
    run_id: str | None = None,
    now: dt.datetime | None = None,
) -> QaHost:
    """Create the run's schema, seed it, and build the gated app. Nothing is served yet."""
    check_database(database_url)
    check_loopback(host)
    if ttl > dt.timedelta(hours=2):
        raise QaHostError("a QA bearer lives at most two hours")
    now = now or dt.datetime.now(dt.UTC)
    run_id = run_id or uuid.uuid4().hex[:12]
    schema = f"qa_mcp_{run_id}"
    tenant_id = uuid.uuid5(uuid.NAMESPACE_URL, f"daimon-qa-mcp:{run_id}")
    manifest = Manifest(
        run_id=run_id,
        created_at=now.isoformat(),
        expires_at=(now + ttl).isoformat(),
        host=host,
        port=port,
        database=_sanitized(database_url),
        schema=schema,
        tenant_id=str(tenant_id),
    )
    manifest.save(root)

    engine = build_test_engine(database_url, schema)
    await _create_schema_with_tables(engine, schema)
    manifest.status = "schema"
    manifest.save(root)
    try:
        return await _seed_and_build(engine, manifest, database_url, host, port, ttl, root, now)
    except BaseException:
        # A half-built run leaves nothing behind.
        await engine.dispose()
        await cleanup(manifest.path(root), database_url=database_url)
        raise


async def _seed_and_build(
    engine: AsyncEngine,
    manifest: Manifest,
    database_url: str,
    host: str,
    port: int,
    ttl: dt.timedelta,
    root: Path,
    now: dt.datetime,
) -> QaHost:
    from daimon.adapters.mcp.server import create_mcp_app
    from daimon.core.config import AnthropicSettings, DatabaseSettings, McpSettings, Settings
    from pydantic import HttpUrl, PostgresDsn, SecretStr

    run_id = manifest.run_id
    tenant_id = uuid.UUID(manifest.tenant_id)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    # The verifier signs with the setting's string value, as bytes.
    secret_text = secrets.token_urlsafe(48)
    secret = secret_text.encode()
    async with sessions() as session, session.begin():
        tenant = await make_tenant(session, id=tenant_id, workspace_id=f"qa-mcp-{run_id}")
        account = await make_account(session, tenant=tenant)
        daimon_token = await mint_agent_mcp_token(
            session,
            account_id=account.id,
            tenant_id=tenant.id,
            agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=QA_AGENT_ID),
            label=f"qa-mcp-{run_id}",
            secret=secret,
            now=now,
            ttl_days=1,
        )
    manifest.token_jti = _jti(daimon_token)

    with scrubbed_environment():
        settings = Settings(
            _env_file=None,  # pyright: ignore[reportCallIssue]
            database=DatabaseSettings(url=PostgresDsn(database_url)),
            anthropic=AnthropicSettings(api_key=SecretStr("sk-qa-synthetic-no-egress")),
            mcp=McpSettings(
                jwt_secret=SecretStr(secret_text),
                public_url=HttpUrl(f"http://{host}:{port}/mcp"),
            ),
        )
        app = create_mcp_app(
            settings=settings,
            sessionmaker=sessions,
            anthropic=synthetic_anthropic(tenant.id, account.id, manifest.refused_egress),
            billing_config=None,
        )
    manifest.public_url_host = urlsplit(str(settings.mcp.public_url)).hostname
    mcp = app.state.mcp
    # The provider's own list: every registered tool, before any session transform.
    for tool in await mcp.local_provider.list_tools():
        if tool.name not in ALLOWED_TOOLS:
            mcp.local_provider.remove_tool(tool.name)
    remaining = tuple(sorted(t.name for t in await mcp.local_provider.list_tools()))
    bearer = RunBearer(
        token=secrets.token_urlsafe(32),
        expires_at=now + ttl,
        revoked_marker=root / run_id / "revoked",
    )
    manifest.status = "built"
    manifest.save(root)
    gate = McpGate(app, bearer=bearer, daimon_token=daimon_token)
    return QaHost(
        gate=gate, bearer=bearer, manifest=manifest, root=root, engine=engine, tool_names=remaining
    )


def _jti(token: str) -> str | None:
    payload = token.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    jti = claims.get("jti")
    return str(jti) if jti is not None else None


async def cleanup(manifest_path: Path, *, database_url: str) -> Manifest:
    """Revoke, stop and delete what the manifest owns, in that order. Idempotent."""
    manifest = Manifest.load(manifest_path)
    root = manifest_path.parent.parent
    check_database(database_url)
    if _sanitized(database_url) != manifest.database:
        raise QaHostError("cleanup must target the manifest's own database")
    RunBearer(
        token="",
        expires_at=dt.datetime.now(dt.UTC),
        revoked_marker=root / manifest.run_id / "revoked",
    ).revoke()
    if manifest.tunnel_pid is not None:
        _stop_process(manifest.tunnel_pid)
        manifest.tunnel_pid = None
    engine = build_test_engine(database_url, manifest.schema)
    try:
        if await schema_exists(engine, manifest.schema):
            if manifest.token_jti is not None:
                sessions = async_sessionmaker(engine, expire_on_commit=False)
                async with sessions() as session, session.begin():
                    await revoke_mcp_token(
                        session, jti=uuid.UUID(manifest.token_jti), now=dt.datetime.now(dt.UTC)
                    )
            await _drop_schema(engine, manifest.schema)
    finally:
        await engine.dispose()
    manifest.status = "cleaned"
    manifest.save(root)
    return manifest


async def schema_exists(engine: AsyncEngine, schema: str) -> bool:
    async with engine.connect() as conn:
        found = await conn.scalar(
            text("SELECT 1 FROM information_schema.schemata WHERE schema_name = :s"), {"s": schema}
        )
    return found is not None


def _stop_process(pid: int) -> None:
    import signal

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return


@asynccontextmanager
async def serve(qa: QaHost) -> AsyncIterator[str]:
    """Serve the gate on its loopback port; yields the `/mcp` URL once it listens."""
    import uvicorn

    check_loopback(qa.manifest.host)
    config = uvicorn.Config(
        qa.gate, host=qa.manifest.host, port=qa.manifest.port, log_level="warning", lifespan="on"
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started:
        if task.done():
            await task
        await asyncio.sleep(0.05)
    bound = server.servers[0].sockets[0].getsockname()[1]
    qa.manifest.port = bound
    qa.manifest.status = "serving"
    qa.manifest.save(qa.root)
    try:
        yield f"http://{qa.manifest.host}:{bound}/mcp"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)


TUNNEL_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def tunnel_command(port: int) -> list[str]:
    """A cloudflared quick tunnel to the gate's loopback port, and nothing else."""
    return [
        "cloudflared",
        "tunnel",
        "--no-autoupdate",
        "--url",
        f"http://127.0.0.1:{port}",
    ]


async def open_tunnel(
    qa: QaHost,
    *,
    lead_go: str,
    spawn: Callable[[list[str]], Awaitable[tuple[int, str]]] | None = None,
) -> str:
    """Expose the gate through a temporary HTTPS tunnel. Only with a lead GO.

    `lead_go` names the lead's GO (an inbox note or timestamp) and is kept in
    the manifest. The tunnel forwards to the gate alone, which serves only
    `POST /mcp` with the run bearer. Returns the public `/mcp` URL.
    """
    if not lead_go.strip():
        raise QaHostError("a tunnel needs the lead's GO, recorded with --lead-go")
    if qa.manifest.status != "serving":
        raise QaHostError("serve the gate before opening a tunnel to it")
    pid, base = await (spawn or _spawn_cloudflared)(tunnel_command(qa.manifest.port))
    qa.manifest.tunnel_pid = pid
    qa.manifest.tunnel_url = f"{base}/mcp"
    qa.manifest.tunnel_lead_go = lead_go
    qa.manifest.save(qa.root)
    return qa.manifest.tunnel_url


async def _spawn_cloudflared(command: list[str]) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    assert process.stderr is not None
    deadline = asyncio.get_running_loop().time() + 30
    while asyncio.get_running_loop().time() < deadline:
        line = await asyncio.wait_for(process.stderr.readline(), timeout=30)
        found = TUNNEL_URL.search(line.decode(errors="replace"))
        if found:
            return process.pid, found.group(0)
        if not line:
            break
    process.terminate()
    raise QaHostError("cloudflared did not report a tunnel URL")
