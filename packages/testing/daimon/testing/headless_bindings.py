"""Headless bindings for frozen QA catalog operations that are not host turns.

Some catalog scenarios carry no mention or reply at all (`est_turns: 0`): the
operation they check is a form submit or a database fact, not a turn. Each
binding here drives the real product path for one such operation, offline and
against an isolated database, and returns what it observed. A binding never
judges: the catalog's own assertion decides PASS or FAIL from its receipt.

`credential_submit.mcp_token` (QA-NEW27-MISTYPED-TOKEN-HEADLESS) submits a
token through the real Discord MCP credential modal. The modal's own probe
logic (`probe_mcp_server`) asks a real token-gated MCP server, the loopback QA
host, whether it accepts the token; only the probe's HTTP transport is mapped
to that host's loopback socket (`loopback_transport`). `db_check` runs one
frozen read-only query exactly as written, and raises `DbCheckInvalid` when
the database refuses it. `http_check` serves a real app on loopback, with its
lifespan, and issues the catalog's unauthenticated GET.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
from anthropic import AsyncAnthropic
from pydantic import JsonValue
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
)
from starlette.types import ASGIApp

# A public-shaped name the product's URL policy accepts; the probe's transport
# maps it to the in-process QA host, so no DNS lookup or network call happens.
QA_TOKEN_MCP_URL = "https://qa-token-mcp.example.com/mcp"
_GUILD_ID = 700000000000000027
_REQUESTER = "7000000000000000270"
_MA_AGENT_ID = "agent_qa_headless_new27"
_PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]*(?:\.[a-z0-9_]+)*)\}")


class BindingRefused(ValueError):
    """A binding was pointed at something this run does not own; nothing was contacted."""


_OFFLINE_BASE_URL = "https://anthropic.invalid"


def refuse_ambient_anthropic() -> None:
    """Refuse any ambient provider setting by name, never reading a value.

    The shared check (`daimon.testing.isolated_sdk`) snapshots names by
    iteration and refuses every known SDK setting and any other
    ANTHROPIC_/OPENAI_/GOOGLE_/GEMINI_ name, present even empty.
    """
    from daimon.testing.isolated_sdk import (
        AmbientSDKConfigurationError,
        _require_clean_environment,  # pyright: ignore[reportPrivateUsage]
    )

    try:
        _require_clean_environment()
    except AmbientSDKConfigurationError as err:
        raise BindingRefused(str(err)) from None


def isolated_anthropic(handler: Callable[[httpx.Request], httpx.Response]) -> AsyncAnthropic:
    """An offline SDK client from the shared isolated builder, over `handler`.

    The builder refuses ambient settings before construction and before every
    copy, and passes every credential, routing and header value explicitly.
    """
    from daimon.testing.isolated_sdk import build_async_anthropic

    refuse_ambient_anthropic()
    return build_async_anthropic(
        transport=httpx.MockTransport(handler),
        api_key="offline-fixture",
        base_url=_OFFLINE_BASE_URL,
    )


def _refuse_all(request: httpx.Request) -> httpx.Response:
    return httpx.Response(500, json={"error": {"message": "not served offline"}})


def check_owned_sessionmaker(sessionmaker: object) -> None:
    """Refuse, before any connection, a factory whose bind or any routed bind is not owned.

    The shared policy (`daimon.testing.db.require_owned_sessionmaker`) reads
    only the factory's configuration: no session is opened, no engine connects.
    """
    from daimon.testing.db import require_owned_sessionmaker

    try:
        require_owned_sessionmaker(sessionmaker)
    except ValueError as err:
        raise BindingRefused(str(err)) from None


# Settings must name a database; the app is handed the owned factory instead, so
# this placeholder is never connected to and no engine is built from it.
_UNUSED_DSN = "postgresql+asyncpg://unused:unused@127.0.0.1:1/unused_qa"


def _owned_url(url: str, origins: frozenset[str]) -> str:
    """Refuse any final URL that is not on one of this run's served loopback origins."""
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL:
        raise BindingRefused("http_check URL does not parse") from None
    if parsed.userinfo:
        raise BindingRefused("http_check URL must not carry credentials")
    origin = f"{parsed.scheme}://{parsed.host}:{parsed.port}"
    if parsed.scheme != "http" or parsed.host != "127.0.0.1" or origin not in origins:
        raise BindingRefused("http_check URL is not on a served loopback origin")
    return url


class DbCheckInvalid(ValueError):
    """The frozen query cannot run against this schema: a catalog defect, not a verdict."""


class _Loopback(httpx.AsyncBaseTransport):
    """Send every request to one loopback origin, keeping its path and query."""

    def __init__(self, origin: str) -> None:
        self._origin = httpx.URL(origin)
        self._inner = httpx.AsyncHTTPTransport(trust_env=False)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        request.url = request.url.copy_with(
            scheme=self._origin.scheme, host=self._origin.host, port=self._origin.port
        )
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


def loopback_transport(served_url: str) -> httpx.AsyncBaseTransport:
    """A transport mapping a public-shaped URL to a served loopback QA host."""
    origin = httpx.URL(served_url)
    if origin.scheme != "http" or origin.host != "127.0.0.1" or origin.port is None:
        raise BindingRefused("the QA host is served on loopback only")
    return _Loopback(f"{origin.scheme}://{origin.host}:{origin.port}")


@dataclass(frozen=True)
class McpTokenSubmitReceipt:
    """What one submit did: the request it used and every effect it reached."""

    # The request's only UUID identity (`credential_requests` is keyed by its
    # secret token and has no `id` column).
    request_key: uuid.UUID
    outcome: str | None
    used: bool
    probe_statuses: tuple[int, ...]
    replies: tuple[str, ...]
    # Every Managed Agents request the submit made, as "METHOD path".
    ma_requests: tuple[str, ...]
    values: dict[str, str] = field(default_factory=dict[str, str])


def _agent_json(tenant_id: uuid.UUID) -> dict[str, Any]:
    return {
        "id": _MA_AGENT_ID,
        "type": "agent",
        "name": "qa-headless",
        "description": None,
        "system": None,
        "model": {"id": "claude-sonnet-5"},
        "mcp_servers": [],
        "skills": [],
        "tools": [],
        "metadata": {"daimon_tenant": str(tenant_id)},
        "archived_at": None,
        "created_at": "2026-10-10T00:00:00Z",
        "updated_at": "2026-10-10T00:00:00Z",
        "version": 1,
    }


async def submit_mcp_token(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    mcp_transport: httpx.AsyncBaseTransport,
    token: str,
    url: str = QA_TOKEN_MCP_URL,
) -> McpTokenSubmitReceipt:
    """Submit `token` for `url` through the real MCP credential modal.

    Seeds one live MCP credential request in a fresh install, then runs
    `McpCredentialModal.on_submit` as its requester would. Managed Agents
    answers only the agent reads the modal needs; any other request (a vault
    write, an attach) is answered 500 and recorded, so a receipt always shows
    whether the token was stored.
    """
    check_owned_sessionmaker(sessionmaker)
    refuse_ambient_anthropic()
    import discord
    from cryptography.fernet import Fernet
    from daimon.adapters.discord.credential_modals import McpCredentialModal
    from daimon.adapters.discord.runtime import DiscordRuntime
    from daimon.core.credential_requests import mint_request_token
    from daimon.core.github_credentials import build_multifernet
    from daimon.core.ma_identity import derive_agent_uuid
    from daimon.core.ma_resolver import new_resolver_cache
    from daimon.core.mcp_oauth.discovery import McpProbe, probe_mcp_server
    from daimon.core.notebooks._rate_limit import RateLimiter
    from daimon.core.scope import DeploymentDefault
    from daimon.core.stores.credential_requests import (
        create_credential_request,
        peek_credential_request,
    )
    from daimon.testing.factories import make_account, make_tenant
    from daimon.testing.turn_fakes import deps_for
    from pydantic import HttpUrl

    async with sessionmaker() as session, session.begin():
        tenant = await make_tenant(session, platform="discord", workspace_id=str(_GUILD_ID))
        account = await make_account(session, tenant=tenant)
        row = await create_credential_request(
            session,
            token=mint_request_token(),
            kind="mcp",
            tenant_id=tenant.id,
            agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=_MA_AGENT_ID),
            account_id=account.id,
            target="qa-token-mcp",
            mcp_server_url=url,
            requester_platform_user_id=_REQUESTER,
            channel_id="qa-headless",
            platform=None,
            parent_channel_id=None,
            origin_thread_id=None,
            posted_message_id=None,
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
            idempotency_key=uuid.uuid4(),
            target_ma_agent_id=_MA_AGENT_ID,
            target_name="qa-headless",
            requested_work=None,
        )

    ma_requests: list[str] = []

    def managed_agents(request: httpx.Request) -> httpx.Response:
        ma_requests.append(f"{request.method} {request.url.path}")
        if request.method == "GET" and request.url.path == "/v1/agents":
            return httpx.Response(
                200, json={"data": [_agent_json(row.tenant_id)], "has_more": False}
            )
        if request.method == "GET" and request.url.path == f"/v1/agents/{_MA_AGENT_ID}":
            return httpx.Response(200, json=_agent_json(row.tenant_id))
        return httpx.Response(500, json={"error": {"message": "not served offline"}})

    probe_statuses: list[int] = []

    async def probe(mcp_server_url: str, bearer_token: str) -> McpProbe:
        async with httpx.AsyncClient(transport=mcp_transport, follow_redirects=False) as http:
            result = await probe_mcp_server(
                http, mcp_server_url=mcp_server_url, bearer_token=bearer_token
            )
        probe_statuses.append(result.status_code)
        return result

    settings = MagicMock()
    settings.mcp.public_url = HttpUrl("https://qa-daimon.example.com/mcp")
    settings.mcp.jwt_secret.get_secret_value.return_value = "q" * 32
    client = isolated_anthropic(managed_agents)
    runtime = DiscordRuntime(
        settings=settings,
        anthropic=client,
        sessionmaker=sessionmaker,
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
        turn_deps=replace(
            deps_for(client, sessionmaker),
            fernet=build_multifernet((Fernet.generate_key().decode(),)),
            turn_path=None,
        ),
        mcp_token_probe=probe,
    )
    modal = McpCredentialModal(runtime=runtime, request_row=row)
    modal.token_input._value = token  # pyright: ignore[reportPrivateUsage]  # what the person typed

    interaction = MagicMock()
    interaction.guild_id = _GUILD_ID
    # The requester submits as the server admin who asked for the token: connecting
    # a new MCP server is a mutation of the agent, which needs its owner.
    interaction.user = MagicMock(spec=discord.Member)
    interaction.user.id = int(_REQUESTER)
    interaction.user.guild_permissions.administrator = True
    interaction.user.guild_permissions.manage_guild = False
    interaction.guild.owner_id = int(_REQUESTER) + 1
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    await modal.on_submit(interaction)

    async with sessionmaker() as session:
        after = await peek_credential_request(session, token=row.token)
    replies = tuple(str(call.args[0]) for call in interaction.followup.send.call_args_list)
    return McpTokenSubmitReceipt(
        request_key=row.idempotency_key,
        outcome=after.outcome if after is not None else None,
        used=after is None or after.used_at is not None,
        probe_statuses=tuple(probe_statuses),
        replies=replies,
        ma_requests=tuple(ma_requests),
        values={"headless.request_id": str(row.idempotency_key)},
    )


def bind_placeholders(sql: str, values: Mapping[str, str]) -> str:
    """Fill the frozen query's placeholders with UUID values only.

    Every placeholder must be known and every value must parse as a UUID, so a
    binding value can never change the query's shape.
    """

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values:
            raise ValueError(f"unbound placeholder {{{name}}}")
        return str(uuid.UUID(values[name]))

    return _PLACEHOLDER.sub(replace, sql)


async def db_check(
    sessionmaker: async_sessionmaker[AsyncSession], sql: str, values: Mapping[str, str]
) -> JsonValue:
    """Run one frozen single-row query read-only and return its single value."""
    check_owned_sessionmaker(sessionmaker)
    bound = bind_placeholders(sql, values)
    if ";" in bound.rstrip().rstrip(";") or not bound.lstrip().lower().startswith("select"):
        raise ValueError("db_check runs exactly one SELECT")
    async with sessionmaker() as session, session.begin():
        await session.execute(text("SET TRANSACTION READ ONLY"))
        try:
            rows = (await session.execute(text(bound))).all()
        except DBAPIError as err:
            # The SQLSTATE only: the message can echo bound values.
            raise DbCheckInvalid(f"SQLSTATE {getattr(err.orig, 'sqlstate', None)}") from None
    if len(rows) != 1 or len(rows[0]) != 1:
        raise ValueError(f"db_check expects one value, got {len(rows)} row(s)")
    return json.loads(json.dumps(rows[0][0], default=str))


_BODY_CAP = 65_536
_SAFE_VALUE = re.compile(r"[A-Za-z0-9._-]{1,64}")


@dataclass(frozen=True)
class HttpReceipt:
    """One unauthenticated GET and what came back (body capped)."""

    url: str
    status: int
    content_type: str | None
    body: str


@asynccontextmanager
async def serve_loopback(app: ASGIApp) -> AsyncIterator[str]:
    """Serve `app` with its lifespan on a free loopback port; yields its origin."""
    import uvicorn

    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on")
    )
    task = asyncio.create_task(server.serve())
    while not server.started:
        if task.done():
            await task
            raise RuntimeError("the app stopped before it listened")
        await asyncio.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)


def expand_url(template: str, values: Mapping[str, str]) -> str:
    """Fill a frozen URL template: `{env.*}` with loopback origins, others with safe tokens."""

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values:
            raise ValueError(f"unbound placeholder {{{name}}}")
        value = values[name]
        if name.startswith("env."):
            if httpx.URL(value).host != "127.0.0.1":
                raise ValueError("http_check origins are loopback only")
            return value.rstrip("/")
        if not _SAFE_VALUE.fullmatch(value):
            raise ValueError(f"unsafe value for {{{name}}}")
        return value

    return _PLACEHOLDER.sub(replace, template)


async def http_check(template: str, values: Mapping[str, str]) -> HttpReceipt:
    """GET the expanded URL without credentials or redirects, as the catalog states."""
    url = expand_url(template, values)
    origins = frozenset(
        f"{o.scheme}://{o.host}:{o.port}"
        for o in (httpx.URL(v) for k, v in values.items() if k.startswith("env."))
    )
    # The final URL decides, not its placeholders: a literal or nested external
    # URL is refused here, before any client exists. No proxy or netrc is read.
    _owned_url(url, origins)
    async with httpx.AsyncClient(follow_redirects=False, timeout=10.0, trust_env=False) as http:
        response = await http.get(url)
    return HttpReceipt(
        url=url,
        status=response.status_code,
        content_type=response.headers.get("content-type"),
        body=response.text[:_BODY_CAP],
    )


def http_check_holds(assertion: Mapping[str, JsonValue], receipt: HttpReceipt) -> bool:
    """The catalog's `http_check` semantics: every stated expectation must hold."""
    if "expect_status" in assertion and receipt.status != assertion["expect_status"]:
        return False
    if "expect_content_type" in assertion:
        media = (receipt.content_type or "").split(";")[0].strip().lower()
        if media != str(assertion["expect_content_type"]).lower():
            return False
    absent = assertion.get("body_absent")
    return not (absent is not None and re.search(str(absent), receipt.body))


@asynccontextmanager
async def serve_catalog_hosts(
    sessionmaker: async_sessionmaker[AsyncSession], root: Path
) -> AsyncIterator[dict[str, str]]:
    """Serve the real MCP, notebook and report apps on loopback for `http_check`.

    Yields the catalog's `{env.*}` origins. Each app is built by its production
    factory with offline settings: a synthetic Anthropic key that is never
    called, no hub, and data directories under `root`. The notebook host runs
    unjailed because a test process cannot change identity.
    """
    from daimon.adapters.mcp.server import create_mcp_app
    from daimon.core.config import AnthropicSettings, DatabaseSettings, McpSettings, Settings
    from daimon.testing.qa_mcp_host import scrubbed_environment
    from notebook_host import config as notebook_config
    from notebook_host import main as notebook_main
    from pydantic import HttpUrl, PostgresDsn, SecretStr
    from report_host import config as report_config
    from report_host import main as report_main

    check_owned_sessionmaker(sessionmaker)
    refuse_ambient_anthropic()
    admin = [SecretStr(secrets.token_urlsafe(32))]
    with scrubbed_environment():
        settings = Settings(
            _env_file=None,  # pyright: ignore[reportCallIssue]
            database=DatabaseSettings(url=PostgresDsn(_UNUSED_DSN)),
            anthropic=AnthropicSettings(api_key=SecretStr("sk-qa-synthetic-no-egress")),
            mcp=McpSettings(
                jwt_secret=SecretStr(secrets.token_urlsafe(48)),
                public_url=HttpUrl("https://qa-daimon.example.com/mcp"),
            ),
        )
        mcp = create_mcp_app(
            settings=settings,
            # The validated factory itself: its schema, routing and provenance are kept.
            sessionmaker=sessionmaker,
            anthropic=isolated_anthropic(_refuse_all),
            billing_config=None,
        )
        notebooks = notebook_main.create_app(
            notebook_config.Settings(
                data_dir=root / "notebooks", admin_secrets=admin, allow_unjailed_spawn=True
            )
        )
        reports = report_main.create_app(
            report_config.Settings(
                data_dir=root / "reports",
                admin_secrets=admin,
                mcp_url=HttpUrl("https://qa-daimon.example.com/mcp"),
                public_url_base=HttpUrl("https://qa-reports.example.com"),
            )
        )
    async with (
        serve_loopback(mcp) as mcp_origin,
        serve_loopback(notebooks) as notebook_origin,
        serve_loopback(reports) as report_origin,
    ):
        yield {
            "env.mcp_public_url": mcp_origin,
            "env.notebook_host": notebook_origin,
            "env.report_host": report_origin,
        }


def credential_request_route(bind: object) -> dict[type, object]:
    """A per-model `binds` entry routing the credential-request model to `bind`.

    For guard proofs: tests outside this package may not import ORM models, so
    the real model reaches a session factory's routing map through here.
    """
    from daimon.core._models import CredentialRequest

    return {CredentialRequest: bind}
