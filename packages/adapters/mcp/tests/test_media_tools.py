"""End-to-end test of the three media tools via FastMCP registration.

The D-16 billing/admission/trusted-path matrix (below the guard tests)
drives the registered tools against real Postgres: a billed success writes
one usage_events row + one matching tenant_ledger debit; a failed Gemini
call writes neither; the trusted (platform_user_id=None) path writes
neither regardless of outcome; a depleted ledger denies with a
``TERMINAL ERROR:`` through the full FastMCP call_tool pipeline.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import AsyncIterator, Callable
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import httpx
import pytest
from anthropic.types.beta.sessions.beta_managed_agents_span_model_usage import (
    BetaManagedAgentsSpanModelUsage,
)
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.file_store import FileStore
from daimon.adapters.mcp.server import create_mcp_app
from daimon.adapters.mcp.services.audio import TTS_MODEL
from daimon.adapters.mcp.services.image import IMAGE_MODEL
from daimon.adapters.mcp.tools.media import register_media_tools
from daimon.core._models import UsageEvent
from daimon.core.config import (
    AnthropicSettings,
    DatabaseSettings,
    GeminiSettings,
    McpSettings,
    Settings,
)
from daimon.core.pricing import MODEL_PRICING, cost_of
from daimon.core.stores import tenant_ledger
from daimon.core.stores.domain import Role
from daimon.core.tenant_balance import debit_amount
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from fastmcp.server.middleware import Middleware, MiddlewareContext
from google.genai import types
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.types import ASGIApp, Message

from .factories import seed_tenant_and_account
from .services.conftest import make_stub_gemini


class _NoopGeminiClient:
    """Stand-in used purely to satisfy DI. The three tools never reach a live
    Gemini call in this test — invalid URL / no live audio etc. short-circuit
    before the SDK is invoked."""


class _SeedAuthMiddleware(Middleware):
    """Inject a trusted (platform_user_id=None) AuthIdentity so the shared
    admission gate (D-02) bypasses balance/cap checks without a real DB.

    Mirrors ``test_skills.py``'s helper of the same name — duplicated per
    the testing guideline (inline setup, no cross-test-file sharing).
    """

    def __init__(self, auth: AuthIdentity) -> None:
        self._auth = auth

    async def on_call_tool(
        self,
        context: MiddlewareContext[Any],
        call_next: Any,
    ) -> Any:
        await context.fastmcp_context.set_state("auth", self._auth, serializable=False)
        return await call_next(context)


def _trusted_auth() -> AuthIdentity:
    return AuthIdentity(
        account_id=uuid.uuid4(), tenant_id=uuid.uuid4(), role=Role.ADMIN, platform_user_id=None
    )


def _register(mcp: FastMCP, tmp_path: Path) -> None:
    mcp.add_middleware(_SeedAuthMiddleware(_trusted_auth()))
    register_media_tools(
        mcp,
        gemini_client=cast(Any, _NoopGeminiClient()),
        file_store=FileStore(base_dir=tmp_path),
        sessionmaker=cast(Any, MagicMock()),
        billing_config=None,
        markup=Decimal("1.0"),
    )


@pytest.mark.asyncio
async def test_register_media_tools_registers_three_tools(tmp_path: Path) -> None:
    mcp = FastMCP(name="t")
    _register(mcp, tmp_path)
    tools = await mcp.list_tools()
    names = {tool.name for tool in tools}
    assert {"generate_audio", "generate_image", "fetch_youtube_transcript"}.issubset(names), (
        f"all three media tools should be registered; got {names}"
    )


@pytest.mark.asyncio
async def test_fetch_youtube_transcript_rejects_non_youtube_url(tmp_path: Path) -> None:
    """The URL guard short-circuits before touching the Gemini client."""
    mcp = FastMCP(name="t")
    _register(mcp, tmp_path)
    async with Client(mcp) as client:
        with pytest.raises(ToolError, match="not a recognised YouTube URL"):
            await client.call_tool(
                "fetch_youtube_transcript",
                {"url": "https://vimeo.com/12345"},
            )


@pytest.mark.asyncio
async def test_generate_image_rejects_invalid_aspect_ratio(tmp_path: Path) -> None:
    """Aspect ratio guard short-circuits before touching the Gemini client."""
    mcp = FastMCP(name="t")
    _register(mcp, tmp_path)
    async with Client(mcp) as client:
        with pytest.raises(ToolError, match="Invalid aspect_ratio"):
            await client.call_tool(
                "generate_image",
                {"prompt": "a cat", "title": "kitty", "aspect_ratio": "not-a-ratio"},
            )


@pytest.mark.asyncio
async def test_generate_audio_rejects_empty_script(tmp_path: Path) -> None:
    """Script parser guard short-circuits before touching the Gemini client."""
    mcp = FastMCP(name="t")
    _register(mcp, tmp_path)
    async with Client(mcp) as client:
        with pytest.raises(ToolError, match="Invalid script"):
            await client.call_tool(
                "generate_audio",
                {"script": "   \n  ", "title": "empty"},
            )


# ---------------------------------------------------------------------------
# D-16 real-Postgres billing/admission/trusted-path matrix
# ---------------------------------------------------------------------------


def _image_response(
    payload: bytes, *, prompt_tokens: int, candidates_tokens: int, thoughts_tokens: int
) -> httpx.Response:
    response = types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(
                    parts=[types.Part(inline_data=types.Blob(data=payload, mime_type="image/png"))]
                ),
            )
        ],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=prompt_tokens,
            candidates_token_count=candidates_tokens,
            thoughts_token_count=thoughts_tokens,
            cached_content_token_count=0,
        ),
    )
    return httpx.Response(200, json=response.model_dump(mode="json", by_alias=True))


def _server_error_response() -> httpx.Response:
    return httpx.Response(
        500, json={"error": {"code": 500, "message": "boom", "status": "INTERNAL"}}
    )


async def _registered_billing_mcp(
    tmp_path: Path,
    *,
    auth: AuthIdentity,
    sessionmaker: async_sessionmaker[AsyncSession],
    handler: Callable[[httpx.Request], httpx.Response],
    markup: Decimal = Decimal("1.0"),
) -> FastMCP:
    mcp = FastMCP(name="t")
    mcp.add_middleware(_SeedAuthMiddleware(auth))
    register_media_tools(
        mcp,
        gemini_client=make_stub_gemini(handler),
        file_store=FileStore(base_dir=tmp_path),
        sessionmaker=sessionmaker,
        billing_config=None,
        markup=markup,
    )
    return mcp


@pytest.mark.asyncio
async def test_generate_image_billed_success_writes_usage_row_and_debits_ledger(
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    tenant_id, account_id = await seed_tenant_and_account(db_session)
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant_id,
        delta_usd=Decimal("10.00"),
        reason="trial",
        idempotency_key=f"trial:{tenant_id}",
    )
    await db_session.commit()

    png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 50

    def handler(_request: httpx.Request) -> httpx.Response:
        return _image_response(
            png_bytes, prompt_tokens=100, candidates_tokens=50, thoughts_tokens=10
        )

    auth = AuthIdentity(
        account_id=account_id, tenant_id=tenant_id, role=Role.USER, platform_user_id="U_CALLER"
    )
    mcp = await _registered_billing_mcp(
        tmp_path, auth=auth, sessionmaker=sessionmaker, handler=handler, markup=Decimal("2.0")
    )

    async with Client(mcp) as client:
        result = await client.call_tool("generate_image", {"prompt": "a cat", "title": "kitty"})
    assert not result.is_error, f"billed success should not error: {result!r}"

    rows = (
        (await db_session.execute(select(UsageEvent).where(UsageEvent.tenant_id == tenant_id)))
        .scalars()
        .all()
    )
    assert len(rows) == 1, "billed success should write exactly one usage_events row"
    row = rows[0]
    assert row.input_tokens == 100, "input_tokens should map prompt_token_count"
    assert row.output_tokens == 60, "output_tokens should fold thoughts into candidates (50+10)"
    assert row.cache_read_input_tokens == 0, "no cached tokens in this response"
    assert row.platform_user_id == "U_CALLER", "usage row should carry the caller's platform id"

    usage = BetaManagedAgentsSpanModelUsage(
        input_tokens=100,
        output_tokens=60,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    expected_debit = debit_amount(cost_of(usage, MODEL_PRICING[IMAGE_MODEL]), markup=Decimal("2.0"))
    balance = await tenant_ledger.get_balance(db_session, tenant_id=tenant_id)
    assert balance == Decimal("10.00") - expected_debit, (
        "tenant balance should visibly decrease by cost_of(usage, pricing) x markup"
    )


@pytest.mark.asyncio
async def test_generate_image_failed_call_writes_no_row_no_debit(
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    tenant_id, account_id = await seed_tenant_and_account(db_session)
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant_id,
        delta_usd=Decimal("10.00"),
        reason="trial",
        idempotency_key=f"trial:{tenant_id}",
    )
    await db_session.commit()

    def handler(_request: httpx.Request) -> httpx.Response:
        return _server_error_response()

    auth = AuthIdentity(
        account_id=account_id, tenant_id=tenant_id, role=Role.USER, platform_user_id="U_CALLER"
    )
    mcp = await _registered_billing_mcp(
        tmp_path, auth=auth, sessionmaker=sessionmaker, handler=handler
    )

    async with Client(mcp) as client:
        with pytest.raises(ToolError, match="Gemini API server error"):
            await client.call_tool("generate_image", {"prompt": "a cat", "title": "kitty"})

    rows = (
        (await db_session.execute(select(UsageEvent).where(UsageEvent.tenant_id == tenant_id)))
        .scalars()
        .all()
    )
    assert len(rows) == 0, "a failed Gemini call must write no usage_events row"
    balance = await tenant_ledger.get_balance(db_session, tenant_id=tenant_id)
    assert balance == Decimal("10.00"), "a failed Gemini call must write no ledger debit"


@pytest.mark.asyncio
async def test_trusted_path_writes_no_row_no_debit(
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """D-02: platform_user_id=None is the trusted, fully-unbilled path — no
    usage row and no debit are written even though the call succeeds."""
    tenant_id, account_id = await seed_tenant_and_account(db_session)
    await db_session.commit()

    png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 50

    def handler(_request: httpx.Request) -> httpx.Response:
        return _image_response(
            png_bytes, prompt_tokens=100, candidates_tokens=50, thoughts_tokens=10
        )

    auth = AuthIdentity(
        account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, platform_user_id=None
    )
    mcp = await _registered_billing_mcp(
        tmp_path, auth=auth, sessionmaker=sessionmaker, handler=handler
    )

    async with Client(mcp) as client:
        result = await client.call_tool("generate_image", {"prompt": "a cat", "title": "kitty"})
    assert not result.is_error, f"trusted path should succeed: {result!r}"

    rows = (
        (await db_session.execute(select(UsageEvent).where(UsageEvent.tenant_id == tenant_id)))
        .scalars()
        .all()
    )
    assert len(rows) == 0, "trusted (platform_user_id=None) path must write no usage row (D-02)"
    balance = await tenant_ledger.get_balance(db_session, tenant_id=tenant_id)
    assert balance == Decimal("0"), "trusted path must write no ledger debit (D-02)"


@pytest.mark.asyncio
async def test_generate_audio_multi_segment_writes_one_aggregated_row(
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A 2-segment script produces one row whose tokens sum both segments."""
    tenant_id, account_id = await seed_tenant_and_account(db_session)
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant_id,
        delta_usd=Decimal("10.00"),
        reason="trial",
        idempotency_key=f"trial:{tenant_id}",
    )
    await db_session.commit()

    pcm_silence = b"\x00\x00" * 240
    call_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        response = types.GenerateContentResponse(
            candidates=[
                types.Candidate(
                    content=types.Content(
                        parts=[
                            types.Part(
                                inline_data=types.Blob(data=pcm_silence, mime_type="audio/pcm")
                            )
                        ]
                    ),
                    finish_reason=types.FinishReason.STOP,
                )
            ],
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=10,
                candidates_token_count=5,
                thoughts_token_count=2,
                cached_content_token_count=0,
            ),
        )
        return httpx.Response(200, json=response.model_dump(mode="json", by_alias=True))

    auth = AuthIdentity(
        account_id=account_id, tenant_id=tenant_id, role=Role.USER, platform_user_id="U_CALLER"
    )
    mcp = await _registered_billing_mcp(
        tmp_path, auth=auth, sessionmaker=sessionmaker, handler=handler
    )

    async with Client(mcp) as client:
        result = await client.call_tool(
            "generate_audio",
            {"script": "[HOST_A] hello\n[HOST_B] world", "title": "two-seg"},
        )
    assert not result.is_error, f"multi-segment audio should succeed: {result!r}"
    assert call_count == 2, "TTS should run once per segment"

    rows = (
        (await db_session.execute(select(UsageEvent).where(UsageEvent.tenant_id == tenant_id)))
        .scalars()
        .all()
    )
    assert len(rows) == 1, "multi-segment audio should write one aggregated row per invocation"
    row = rows[0]
    assert row.input_tokens == 20, "input_tokens should sum both segments' prompt tokens"
    assert row.output_tokens == 14, "output_tokens should sum both segments' candidates+thoughts"
    assert row.model == TTS_MODEL, "usage row should carry the pinned TTS model id"


@contextlib.asynccontextmanager
async def _lifespan(app: ASGIApp) -> AsyncIterator[None]:
    """Drive ASGI lifespan startup/shutdown around an in-process HTTP call.

    Duplicated from ``tools/test_agents.py``'s harness of the same name per
    the testing guideline (inline setup, no cross-test-file sharing) — the
    full call_tool pipeline (auth -> IdentityMiddleware -> tool) only runs
    through the real ASGI app, not the in-memory ``Client(mcp)`` transport
    (which has no auth support at all).
    """
    send_q: asyncio.Queue[Message] = asyncio.Queue()
    recv_q: asyncio.Queue[Message] = asyncio.Queue()

    async def receive() -> Message:
        return await recv_q.get()

    async def send(message: Message) -> None:
        await send_q.put(message)

    async def run() -> None:
        await app({"type": "lifespan", "asgi": {"version": "3.0"}}, receive, send)

    task = asyncio.create_task(run())
    await recv_q.put({"type": "lifespan.startup"})
    msg = await send_q.get()
    assert msg["type"] == "lifespan.startup.complete", msg
    try:
        yield
    finally:
        await recv_q.put({"type": "lifespan.shutdown"})
        msg = await send_q.get()
        assert msg["type"] == "lifespan.shutdown.complete", msg
        await task


def _parse_jsonrpc(resp: httpx.Response) -> dict[str, object]:
    ct = resp.headers.get("content-type", "")
    if "text/event-stream" in ct:
        for line in resp.text.splitlines():
            if line.startswith("data: "):
                return json.loads(line[6:])  # type: ignore[return-value]
        raise AssertionError(f"No data line in SSE: {resp.text!r}")
    return resp.json()  # type: ignore[return-value]


async def _call_tool_via_http(
    app: ASGIApp, token: str, name: str, arguments: dict[str, object]
) -> dict[str, object]:
    """Initialize an MCP HTTP session and call tools/call; return the JSON-RPC result."""
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    transport = httpx.ASGITransport(app=app)  # pyright: ignore[reportArgumentType]
    async with _lifespan(app), httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        init_resp = await c.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            },
            headers=headers,
        )
        assert init_resp.status_code == 200, f"initialize failed: {init_resp.text}"
        session_id = init_resp.headers.get("mcp-session-id")
        if session_id:
            headers["Mcp-Session-Id"] = session_id
        call_resp = await c.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
            headers=headers,
        )
        assert call_resp.status_code == 200, f"tools/call failed: {call_resp.text}"
        return _parse_jsonrpc(call_resp)


@pytest.mark.asyncio
async def test_full_pipeline_denies_with_terminal_error_on_depleted_ledger(
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A depleted ledger denies through the full FastMCP call_tool pipeline
    (SPEC acceptance 1) — not just the _impl-level admission helper."""
    tenant_id, account_id = await seed_tenant_and_account(db_session)
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant_id,
        delta_usd=Decimal("-1.00"),
        reason="media_debit",
        idempotency_key=f"deplete:{tenant_id}",
    )
    await db_session.commit()

    token = "media-deny-token"
    claims = {
        "sub": str(account_id),
        "tenant_id": str(tenant_id),
        "role": "user",
        "platform_user_id": "U_CALLER",
        "client_id": "test",
    }

    app = create_mcp_app(
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
            anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
            mcp=McpSettings(public_url=HttpUrl("https://t.example.com/mcp")),
            gemini=GeminiSettings(api_key=SecretStr("test-gemini-key")),
        ),
        sessionmaker=sessionmaker,
        auth=StaticTokenVerifier(tokens={token: claims}),
    )

    result = await _call_tool_via_http(
        app, token, "generate_image", {"prompt": "a cat", "title": "kitty"}
    )
    payload = result.get("result", result)
    assert isinstance(payload, dict), f"unexpected tools/call shape: {result!r}"
    assert payload.get("isError"), f"depleted ledger should deny: {payload!r}"
    content = payload.get("content") or []
    message_text = " ".join(
        part.get("text", "")
        for part in content
        if isinstance(part, dict)  # type: ignore[union-attr]
    )
    assert "TERMINAL ERROR" in message_text, (
        f"deny message should start TERMINAL ERROR: {payload!r}"
    )
    assert "/billing" in message_text, f"deny message should name /billing: {payload!r}"

    rows = (
        (await db_session.execute(select(UsageEvent).where(UsageEvent.tenant_id == tenant_id)))
        .scalars()
        .all()
    )
    assert len(rows) == 0, "a denied call must write no usage_events row"
