"""The drift gate between the seeded prompt/skill's tool claims and the live
tool registry.

This is the structural fix for how the routines lie got into the seeded
prompt in the first place: prose drifts from the live tool surface over
time, but tool names do not — so tool names are what gets checked, against a
real chat-turn-shaped session driven through the real MCP handshake, every
time this suite runs.

Every identifier-shaped token in the seeded `daimon` agent's system prompt,
and in every skill it references (not just `workspace-setup`), is checked:

- If the token is not a real, currently-registered tool name (an ordinary
  parameter name or other prose that merely looks identifier-shaped), it is
  ignored — UNLESS it's written in call syntax (immediately followed by
  `(`), which is an unambiguous claim that the model should invoke it. A
  call-syntax reference to a name that turns out not to exist at all fails
  as "unregistered".
- If it is a real tool, it must be discoverable by a chat-turn-shaped
  session: an account subject, no `agent_id` claim, and a Discord platform
  identity. Admin-only tools may be named for a handoff, but must remain
  hidden from the member session and discoverable to the admin session.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import itertools
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import httpx
import pytest
from daimon.adapters.mcp.server import create_mcp_app
from daimon.core.config import (
    AnthropicSettings,
    DatabaseSettings,
    DiscordSettings,
    McpSettings,
    Settings,
)
from daimon.core.defaults.loader import (
    load_agent_specs,
    load_skill_paths,
    load_skill_spec,
)
from daimon.core.mcp_auth import mint_jwt
from daimon.core.stores import accounts
from daimon.core.stores.domain import Role
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from daimon.testing.ma import MARouter, build_fake_anthropic
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.types import ASGIApp, Message

pytestmark = pytest.mark.asyncio

SECRET = "a" * 32
_NOW = dt.datetime(2026, 4, 24, tzinfo=dt.UTC)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULTS = REPO_ROOT / "defaults"

# Identifier-shaped candidate tokens: lowercase snake_case with at least one
# underscore. Every real MCP tool name in this codebase is multi-word, so
# this shape is a cheap, effective first filter before anything hits the
# network — plain English words in the surrounding prose never match it.
_TOKEN_RE = re.compile(r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b")

# Naming a gated operation lets a member hand the exact request to an admin.
# These are still real tools: both their admin visibility and member hiding
# are checked below, rather than exempting them from registry validation.
_ADMIN_HANDOFF_TOOLS: set[str] = {
    "delete_skill",
    "archive_agent",
    "sync_skills",
    "set_agent_default",
    "clear_agent_default",
}


_FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)


def _strip_fenced_code_blocks(text: str) -> str:
    """Drop ```-fenced example code (bash/python usage snippets) before any
    extraction. A genuine MCP tool reference in this codebase's skills is
    always given as inline single-backtick prose (`tool_name` or
    `tool_name(args)`); a fenced block is illustrative library usage
    (`pd.read_csv(...)`, `az.from_netcdf(...)`, a user-defined helper inside
    a worked example) that would otherwise look identical, in shape, to a
    real tool call."""
    return _FENCED_CODE_RE.sub("", text)


def _extract_candidates(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text))


def _is_call_syntax(token: str, text: str) -> bool:
    """True if `token` appears immediately followed by '(' anywhere in `text`,
    as a BARE call — not attribute-qualified.

    An unambiguous tool-invocation shape (`create_routine(...)`), as opposed
    to a bare parameter-name mention inside another tool's own argument list
    (e.g. `cron_expr` documented as one of create_routine's arguments), or a
    qualified call into an ordinary Python library used in worked examples
    (`az.from_netcdf(...)`, `pd.read_csv(...)`) — every real MCP tool call in
    this codebase's skills is a bare name, never `module.tool_name(...)`.
    """
    return re.search(rf"(?<!\.)\b{re.escape(token)}\(", text) is not None


def _collect_texts() -> tuple[str, dict[str, str]]:
    """Return (system prompt text, {skill name: skill body}) for the seeded
    `daimon` agent and every custom skill its `skills:` block references —
    not a hardcoded single skill path, so a second seeded skill is covered
    automatically."""
    specs = load_agent_specs(DEFAULTS / "agents")
    daimon = next(s for s in specs if s.name == "daimon")
    system = _strip_fenced_code_blocks(daimon.system or "")
    referenced_skill_ids = {ref.skill_id for ref in daimon.skills if ref.type == "custom"}
    bodies: dict[str, str] = {}
    for skill_dir in load_skill_paths(DEFAULTS / "skills"):
        spec, body = load_skill_spec(skill_dir)
        if spec.name in referenced_skill_ids:
            bodies[spec.name] = _strip_fenced_code_blocks(body)
    return system, bodies


@contextlib.asynccontextmanager
async def _lifespan(app: ASGIApp) -> AsyncIterator[None]:
    send_queue: asyncio.Queue[Message] = asyncio.Queue()
    receive_queue: asyncio.Queue[Message] = asyncio.Queue()

    async def receive() -> Message:
        return await receive_queue.get()

    async def send(message: Message) -> None:
        await send_queue.put(message)

    async def run_lifespan() -> None:
        await app({"type": "lifespan", "asgi": {"version": "3.0"}}, receive, send)

    task = asyncio.create_task(run_lifespan())

    await receive_queue.put({"type": "lifespan.startup"})
    msg = await send_queue.get()
    assert msg["type"] == "lifespan.startup.complete", msg
    try:
        yield
    finally:
        await receive_queue.put({"type": "lifespan.shutdown"})
        msg = await send_queue.get()
        assert msg["type"] == "lifespan.shutdown.complete", msg
        await task


def _parse_jsonrpc_response(resp: httpx.Response) -> dict[str, object]:
    content_type = resp.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        for line in resp.text.splitlines():
            if line.startswith("data: "):
                return json.loads(line[6:])  # type: ignore[return-value]
        raise AssertionError(f"No data line in SSE response: {resp.text!r}")
    return resp.json()  # type: ignore[return-value]


@contextlib.asynccontextmanager
async def _open_session(
    app: ASGIApp, *, token: str
) -> AsyncIterator[Callable[[str, dict[str, object] | None], Awaitable[dict[str, object]]]]:
    """Initialize one real MCP session and yield a caller for repeated
    JSON-RPC calls against it — the handshake runs once, not per query."""
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
        counter = itertools.count(2)

        async def _call(method: str, params: dict[str, object] | None = None) -> dict[str, object]:
            body = {"jsonrpc": "2.0", "id": next(counter), "method": method, "params": params or {}}
            resp = await c.post("/mcp", json=body, headers=headers)
            assert resp.status_code == 200, f"{method} failed ({resp.status_code}): {resp.text}"
            return _parse_jsonrpc_response(resp)

        yield _call


async def _search_text(
    call: Callable[[str, dict[str, object] | None], Awaitable[dict[str, object]]],
    query: str,
) -> str:
    result = await call("tools/call", {"name": "search_tools", "arguments": {"query": query}})
    payload = result.get("result", result)
    content = payload.get("content", [])  # type: ignore[union-attr]
    return "\n".join(item.get("text", "") for item in content if isinstance(item, dict))


def _make_app(sessionmaker: async_sessionmaker[AsyncSession]) -> ASGIApp:
    return create_mcp_app(
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
            anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
            discord=DiscordSettings(bot_token=SecretStr("test-discord-token")),
            mcp=McpSettings(jwt_secret=SecretStr(SECRET), public_url=HttpUrl("https://x/mcp")),
        ),
        sessionmaker=sessionmaker,
        anthropic=build_fake_anthropic(MARouter().dispatch),
    )


async def _seed_admin_token(sessionmaker: async_sessionmaker[AsyncSession]) -> str:
    async with sessionmaker() as s, s.begin():
        tenant = await make_tenant(s, platform="discord", workspace_id="prompt-claims-admin")
        account = await make_account(s, tenant=tenant)
        await make_platform_principal(
            s,
            platform="discord",
            external_id="discord-admin-1",
            tenant=tenant,
            account=account,
        )
        await accounts.set_role(s, account.id, Role.ADMIN)
        account_id = account.id
    return mint_jwt(account_id=account_id, secret=SECRET.encode(), now=_NOW)


async def _seed_chat_turn_token(sessionmaker: async_sessionmaker[AsyncSession]) -> str:
    """A token shaped exactly like a Discord chat turn: account subject, no
    `agent_id` claim, no `is_admin` claim, a Discord-bound platform identity —
    the same identity shape used by the adapter."""
    async with sessionmaker() as s, s.begin():
        tenant = await make_tenant(s, platform="discord", workspace_id="prompt-claims-chat-turn")
        account = await make_account(s, tenant=tenant)
        await make_platform_principal(
            s,
            platform="discord",
            external_id="discord-user-1",
            tenant=tenant,
            account=account,
        )
        account_id = account.id
    return mint_jwt(account_id=account_id, secret=SECRET.encode(), now=_NOW)


async def test_seeded_prompt_and_skill_tool_claims_are_chat_turn_reachable(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Setup guidance names reachable tools and preserves role-specific discovery."""
    system_text, skill_bodies = _collect_texts()

    candidates: dict[str, str] = {}
    for token in _extract_candidates(system_text):
        candidates.setdefault(token, "the seeded system prompt")
    for skill_name, body in skill_bodies.items():
        for token in _extract_candidates(body):
            candidates.setdefault(token, f"the {skill_name!r} skill")

    app = _make_app(db_session_factory)
    admin_token = await _seed_admin_token(db_session_factory)
    chat_token = await _seed_chat_turn_token(db_session_factory)

    admin_search_text: dict[str, str] = {}
    async with _open_session(app, token=admin_token) as admin_call:
        for token in candidates:
            admin_search_text[token] = await _search_text(admin_call, token.replace("_", " "))

    chat_search_text: dict[str, str] = {}
    async with _open_session(app, token=chat_token) as chat_call:
        for token in candidates:
            chat_search_text[token] = await _search_text(chat_call, token.replace("_", " "))

    all_text = "\n".join([system_text, *skill_bodies.values()])
    offending: list[str] = []
    for token, source in sorted(candidates.items()):
        heading = rf"^### {re.escape(token)}\b"
        is_registered = re.search(heading, admin_search_text[token], re.MULTILINE) is not None
        is_chat_discoverable = re.search(heading, chat_search_text[token], re.MULTILINE) is not None
        if token in _ADMIN_HANDOFF_TOOLS:
            assert is_registered, f"admin handoff names an undiscoverable tool: {token}"
            assert not is_chat_discoverable, f"admin-only tool is exposed to a member: {token}"
            continue
        if not is_registered and not _is_call_syntax(token, all_text):
            # Not a real tool, and not written as a call — an ordinary
            # identifier-shaped word (a parameter name, a YAML key, ...).
            continue
        if not is_chat_discoverable:
            reason = (
                "registered but not reachable from a chat turn"
                if is_registered
                else "unregistered — no such tool exists"
            )
            offending.append(f"{token!r} (named in {source}): {reason}")

    assert not offending, (
        "the seeded prompt/skill names a tool a chat turn cannot reach:\n" + "\n".join(offending)
    )
