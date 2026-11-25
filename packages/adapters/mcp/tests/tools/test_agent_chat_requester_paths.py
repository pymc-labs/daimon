"""Every path that sends or resumes an agent-chat turn records who drives it, and only a
person signed in to the hub is recorded as someone the identity relay may name
(`daimon.core.agent_chat_requester`)."""

from __future__ import annotations

import ast
import dataclasses
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import anthropic
import httpx
import pytest
from anthropic.types.beta.sessions.beta_managed_agents_text_block import (
    BetaManagedAgentsTextBlock,
)
from anthropic.types.beta.sessions.beta_managed_agents_user_message_event import (
    BetaManagedAgentsUserMessageEvent,
)
from daimon.adapters import mcp as mcp_package
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.hub.app import build_hub_app
from daimon.adapters.mcp.hub.claims import encode_hub_claims
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools import _agent_chat_safety, agent_chat
from daimon.adapters.mcp.tools.agent_chat import (
    _ask_impl,  # pyright: ignore[reportPrivateUsage]
    _continue_turn_impl,  # pyright: ignore[reportPrivateUsage]
    _start_turn_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.agent_chat_requester import driver_signed_in, record_driver
from daimon.core.config import SignedIdentityServer
from daimon.core.hub_identity import HubTenant
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_identity import relay_url
from daimon.core.stores import session_settle_state
from daimon.core.stores.domain import Role
from daimon.testing import AGENT_ID, build_turn_router
from daimon.testing.factories import make_ledger_entry, make_platform_principal, make_tenant
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from mux.testing.drivers.anthropic.ma import build_fake_anthropic, session_response
from mux.testing.drivers.anthropic.ma_models import ma_session, ma_session_agent
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..hub.test_turn_parity import _call_tool, _runtime

_TOKEN = "tok"
_NEW = "ses_new_chat"
_EXISTING = "ses_existing_chat"
_SENDERS = {"_send_billed", "resume_paused"}
_MCP_ROOT = Path(mcp_package.__file__).parent


def _called(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    return func.attr if isinstance(func, ast.Attribute) else None


def _send_paths() -> dict[str, tuple[list[ast.Call], list[ast.Call], list[ast.AsyncWith]]]:
    """Each function of the MCP adapter that sends or resumes a turn: its sends, its
    `record_driver` calls and its `session_mutation_fence` blocks."""
    found: dict[str, tuple[list[ast.Call], list[ast.Call], list[ast.AsyncWith]]] = {}
    for path in sorted(_MCP_ROOT.rglob("*.py")):
        for fn in ast.walk(ast.parse(path.read_text())):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)]
            sends = [c for c in calls if _called(c) in _SENDERS]
            if sends:
                records = [c for c in calls if _called(c) == "record_driver"]
                fences = [
                    n
                    for n in ast.walk(fn)
                    if isinstance(n, ast.AsyncWith)
                    and any(
                        isinstance(item.context_expr, ast.Call)
                        and _called(item.context_expr) == "session_mutation_fence"
                        for item in n.items
                    )
                ]
                found[f"{path.relative_to(_MCP_ROOT)}:{fn.name}"] = (sends, records, fences)
    return found


def _inside(node: ast.AST, block: ast.AST) -> bool:
    return any(child is node for child in ast.walk(block))


def test_every_send_or_resume_of_an_agent_chat_turn_records_its_driver_first() -> None:
    paths = _send_paths()
    assert set(paths) == {
        "tools/agent_chat.py:_start_turn_impl",
        "tools/agent_chat.py:_continue_turn_impl",
    }, "a new path that sends an agent-chat turn must record its driver; add it here"
    for name, (sends, records, fences) in paths.items():
        for send in sends:
            before = [r for r in records if r.lineno < send.lineno]
            assert before, f"{name} line {send.lineno} sends with no driver recorded"
            nearest = max(before, key=lambda r: r.lineno)
            assert any(_inside(send, f) and _inside(nearest, f) for f in fences), (
                f"{name} line {send.lineno}: the driver record and the send share one "
                "session fence, so another caller's send cannot fall between them"
            )
            if _called(send) == "_send_billed":
                assert any(
                    k.arg == "earlier_turn_open"
                    and isinstance(k.value, ast.Await)
                    and isinstance(k.value.value, ast.Call)
                    and _called(k.value.value) == "earlier_turn_open"
                    for k in nearest.keywords
                ), f"{name}: whether an earlier send is open is read, never assumed"
            resuming = any(
                k.arg == "resuming" and isinstance(k.value, ast.Constant) and k.value.value
                for k in nearest.keywords
            )
            assert resuming == (_called(send) == "resume_paused"), (
                f"{name} line {send.lineno}: a resume records as one, a send does not"
            )
            assert any(
                k.arg == "signed_in_person" and ast.unparse(k.value) == "auth.signed_in_person"
                for k in nearest.keywords
            ), f"{name}: the driver comes from the caller's verified identity alone"


def test_only_the_hub_identity_is_a_signed_in_person() -> None:
    """Agent, coding and CLI keys are held by someone not established as one person."""
    repo = _MCP_ROOT.parents[4].parent
    setters = [
        f"{path.relative_to(repo)}:{node.lineno}"
        for top in ("packages", "apps")
        for path in sorted((repo / top).rglob("*.py"))
        if "tests" not in path.parts and ".venv" not in path.parts
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.keyword)
        and node.arg == "signed_in_person"
        and isinstance(node.value, ast.Constant)
        and node.value.value is True
    ]
    assert [s.rsplit(":", 1)[0] for s in setters] == [
        "packages/adapters/mcp/daimon/adapters/mcp/tools/hub.py"
    ], setters


def test_no_send_record_is_ever_deleted() -> None:
    """`earlier_turn_open` reads any send record on a new session as an earlier send,
    which holds only while nothing deletes a row of the settle-state table."""
    repo = _MCP_ROOT.parents[4].parent
    offenders = []
    for root in (repo / "packages", repo / "apps"):
        for path in root.rglob("*.py"):
            parts = set(path.parts)
            if parts & {"tests", "versions", ".venv", "node_modules"}:
                continue
            text = path.read_text(encoding="utf-8")
            if "delete(SessionSettleState" in text or (
                "usage_reconcile_progress" in text and path.name != "_models.py"
            ):
                offenders.append(str(path.relative_to(repo)))
    assert offenders == []


@dataclass
class _World:
    runtime: McpRuntime
    app: Any
    daimon_id: str
    auth: AuthIdentity
    status: dict[str, str]
    #: The transcript newest first, as `_read_turn` asks for it.
    events: list[dict[str, Any]]


_EARLIER = "sevt_earlier"
_IDLE = {
    "id": "sevt_idle",
    "type": "session.status_idle",
    "stop_reason": {"type": "end_turn", "event_ids": []},
}
_REPLY = {
    "id": "sevt_reply",
    "type": "agent.message",
    "content": [{"type": "text", "text": "done"}],
}


def _boundary(event_id: str) -> dict[str, Any]:
    return {"id": event_id, "type": "user.message", "content": [{"type": "text", "text": "hi"}]}


async def _world(
    db_session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    *,
    opted_in: bool = True,
) -> _World:
    """A hub person `u1`, with a server named for agent-chat requesters unless not
    `opted_in`."""
    tenant = await make_tenant(db_session, platform="discord", workspace_id="g1")
    principal = await make_platform_principal(
        db_session, platform="discord", external_id="u1", tenant=tenant
    )
    await make_ledger_entry(db_session, tenant=tenant, delta_usd=Decimal("10"))
    await db_session.commit()
    hub_tenant = HubTenant(
        tenant_id=tenant.id, account_id=principal.account_id, workspace_id="g1",
        workspace_name="Acme",
    )  # fmt: skip
    claims = encode_hub_claims(platform="discord", platform_user_id="u1", tenants=[hub_tenant])
    verifier = StaticTokenVerifier(
        tokens={_TOKEN: {"sub": "u1", "client_id": "c", "upstream_claims": claims}}
    )
    status = {"value": "idle"}
    events: list[dict[str, Any]] = [_IDLE, _REPLY, _boundary(_EARLIER)]
    router = build_turn_router(
        str(tenant.id),
        send_events_data=[
            BetaManagedAgentsUserMessageEvent(
                id="sevt_boundary",
                content=[BetaManagedAgentsTextBlock(type="text", text="hello")],
                type="user.message",
                processed_at=datetime(2026, 9, 6, tzinfo=UTC),
            ).model_dump(mode="json")
        ],
    )
    router.add(
        "GET",
        r"/v1/sessions/([^/]+)$",
        lambda _r, m: session_response(
            session_id=m.group(1),
            status=status["value"],
            agent_id=AGENT_ID,
            metadata={"daimon_agent_chat": "true", "daimon_account": str(principal.account_id)},
        ),
    )
    router.add(
        "GET",
        r"/v1/sessions/([^/]+)/events$",
        lambda _r, _m: httpx.Response(200, json={"data": list(events), "next_page": None}),
    )
    # The earlier agent-chat send on the existing session, recorded and seen to end.
    async with factory.begin() as db:
        await session_settle_state.record_unattended_turn(
            db, managed_session_id=_EXISTING, turn_event_id=_EARLIER, rules_channel_id=None
        )
    runtime = _runtime(build_fake_anthropic(router.dispatch), factory)
    if opted_in:
        runtime = dataclasses.replace(
            runtime,
            settings=runtime.settings.model_copy(
                update={
                    "mcp_identity": runtime.settings.mcp_identity.model_copy(
                        update={
                            "servers": [
                                _RELAY_SERVER.model_copy(update={"agent_chat_requester": True})
                            ]
                        }
                    )
                }
            ),
        )
    agent_id = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=AGENT_ID)
    key_auth = AuthIdentity(
        account_id=principal.account_id, tenant_id=tenant.id, role=Role.USER,
        platform="discord", external_id="g1", agent_id=agent_id, platform_user_id="u1",
    )  # fmt: skip
    return _World(
        runtime=runtime,
        app=build_hub_app(platform="discord", runtime=runtime, auth=verifier, billing_config=None),
        daimon_id=str(agent_id),
        auth=key_auth,
        status=status,
        events=events,
    )


def _new_session(*_a: object, **_kw: object) -> object:
    return ma_session(id=_NEW, agent_id=AGENT_ID, environment_id="env_test", status="idle")


async def _signed_in(factory: async_sessionmaker[AsyncSession], session_id: str) -> bool:
    return await driver_signed_in(factory, managed_session_id=session_id)


async def _mark(factory: async_sessionmaker[AsyncSession], session_id: str) -> None:
    """A signed-in person's earlier turn, which a later driver must replace."""
    await record_driver(
        factory,
        managed_session_id=session_id,
        signed_in_person=True,
        session_status=None,
        earlier_turn_open=False,
    )


@pytest.mark.parametrize("tool", ["ask", "start_turn", "continue_turn", "ask_continue"])
async def test_a_hub_turn_records_the_signed_in_person(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], tool: str
) -> None:
    world = await _world(db_session, db_session_factory)
    continuing = tool in ("continue_turn", "ask_continue")
    arguments: dict[str, str] = {"daimon_id": world.daimon_id, "message": "status?"}
    if continuing:
        arguments["handle"] = _EXISTING
    with patch.object(agent_chat, "create_session", new=AsyncMock(side_effect=_new_session)):
        result = await _call_tool(
            world.app.http_app(path="/mcp", stateless_http=True, json_response=True),
            "/mcp",
            _TOKEN,
            name="ask" if tool == "ask_continue" else tool,
            arguments=arguments,
        )
    assert not result["result"].get("isError"), result
    session_id = _EXISTING if continuing else _NEW
    assert await _signed_in(db_session_factory, session_id), "the hub sign-in names the person"


async def test_a_hub_send_to_a_session_running_another_turn_names_nobody(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    world = await _world(db_session, db_session_factory)
    await _mark(db_session_factory, _EXISTING)
    world.status["value"] = "running"
    await _call_tool(
        world.app.http_app(path="/mcp", stateless_http=True, json_response=True),
        "/mcp",
        _TOKEN,
        name="continue_turn",
        arguments={"daimon_id": world.daimon_id, "handle": _EXISTING, "message": "and?"},
    )
    assert not await _signed_in(db_session_factory, _EXISTING), (
        "the relay could not tell which of the two turns calls"
    )


@pytest.mark.parametrize("tool", ["continue_turn", "ask"])
async def test_a_hub_send_after_a_send_not_yet_seen_to_end_names_nobody(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], tool: str
) -> None:
    """The status still reads idle just after a key's send was accepted, so its turn
    has not started; the person's send right after must not lend it their name."""
    world = await _world(db_session, db_session_factory)
    async with db_session_factory.begin() as db:
        await session_settle_state.record_unattended_turn(
            db, managed_session_id=_EXISTING, turn_event_id="sevt_key_send", rules_channel_id=None
        )
    for transcript in (
        [_IDLE, _REPLY, _boundary(_EARLIER)],  # the key's message is not listed yet
        [_boundary("sevt_key_send"), _IDLE, _REPLY, _boundary(_EARLIER)],  # listed, not ended
    ):
        world.events[:] = transcript
        await _call_tool(
            world.app.http_app(path="/mcp", stateless_http=True, json_response=True),
            "/mcp",
            _TOKEN,
            name=tool,
            arguments={"daimon_id": world.daimon_id, "handle": _EXISTING, "message": "and?"},
        )
        assert world.status["value"] == "idle"
        assert not await _signed_in(db_session_factory, _EXISTING), (
            "an earlier send whose turn was not seen to end may still be calling"
        )


async def test_a_key_send_before_the_start_records_its_driver_names_nobody(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A key with the same account may list the new session and send on it before the
    start's own send; that turn must not run under the person's name."""
    world = await _world(db_session, db_session_factory)

    async def created_then_key_sends(*_a: object, **_kw: object) -> object:
        async with db_session_factory.begin() as db:
            await session_settle_state.record_unattended_turn(
                db, managed_session_id=_NEW, turn_event_id="sevt_key_send", rules_channel_id=None
            )
        return _new_session()

    with patch.object(
        agent_chat, "create_session", new=AsyncMock(side_effect=created_then_key_sends)
    ):
        await _call_tool(
            world.app.http_app(path="/mcp", stateless_http=True, json_response=True),
            "/mcp",
            _TOKEN,
            name="start_turn",
            arguments={"daimon_id": world.daimon_id, "message": "status?"},
        )
    assert not await _signed_in(db_session_factory, _NEW), "another caller sent first"


def _accepted_unseen() -> anthropic.InternalServerError:
    """A 5xx after the send: Managed Agents may have accepted it, so Daimon keeps its
    settle request but records no turn."""
    request = httpx.Request("POST", "https://api.anthropic.com/v1/sessions/s/events")
    return anthropic.InternalServerError(
        "upstream timed out", response=httpx.Response(503, request=request), body=None
    )


async def _key_send_accepted_unrecorded(world: _World, handle: str) -> None:
    """A key's continue on `handle`, through its real path, whose send may have been
    accepted although Daimon recorded no turn for it."""
    with (
        patch.object(
            agent_chat, "send_session_events", new=AsyncMock(side_effect=_accepted_unseen())
        ),
        pytest.raises(anthropic.APIStatusError),
    ):
        await _continue_turn_impl(world.runtime, world.auth, handle, "hi")


async def test_a_key_send_accepted_but_not_recorded_before_the_start_names_nobody(
    db_session: AsyncSession, pooled_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """The key's send ends in an error that may hide its acceptance, so no turn is
    recorded for it; its turn may still run, so the person's start must not name them.

    On independent connections, as in production, so the key's committed records
    outlive its fence's rolled-back transaction.
    """
    world = await _world(db_session, pooled_sessionmaker)

    async def created_then_key_sends(*_a: object, **_kw: object) -> object:
        await _key_send_accepted_unrecorded(world, _NEW)
        async with pooled_sessionmaker() as db:
            records = await session_settle_state.send_records(db, managed_session_id=_NEW)
        assert records is not None and records.turn_event_id is None, "sent, not recorded"
        return _new_session()

    with patch.object(
        agent_chat, "create_session", new=AsyncMock(side_effect=created_then_key_sends)
    ):
        result = await _call_tool(
            world.app.http_app(path="/mcp", stateless_http=True, json_response=True),
            "/mcp",
            _TOKEN,
            name="start_turn",
            arguments={"daimon_id": world.daimon_id, "message": "status?"},
        )
    assert not result["result"].get("isError"), result
    assert not await _signed_in(pooled_sessionmaker, _NEW), "another caller may have sent first"


async def test_a_key_send_accepted_but_not_recorded_or_listed_names_nobody_on_continue(
    db_session: AsyncSession, pooled_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """The same send on an existing session, before the transcript lists it: the
    newest listed turn is the recorded one and has ended, but the key's may run."""
    world = await _world(db_session, pooled_sessionmaker)
    await _key_send_accepted_unrecorded(world, _EXISTING)
    assert world.events == [_IDLE, _REPLY, _boundary(_EARLIER)], "not listed yet"
    result = await _call_tool(
        world.app.http_app(path="/mcp", stateless_http=True, json_response=True),
        "/mcp",
        _TOKEN,
        name="continue_turn",
        arguments={"daimon_id": world.daimon_id, "handle": _EXISTING, "message": "and?"},
    )
    assert not result["result"].get("isError"), result
    assert not await _signed_in(pooled_sessionmaker, _EXISTING), (
        "a send begun after the recorded turn may still be calling"
    )


@pytest.mark.parametrize("tool", ["continue_turn", "start_turn"])
async def test_with_no_server_opted_in_a_hub_send_names_nobody(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], tool: str
) -> None:
    world = await _world(db_session, db_session_factory, opted_in=False)
    arguments = {"daimon_id": world.daimon_id, "message": "and?"}
    if tool == "continue_turn":
        arguments["handle"] = _EXISTING
    with patch.object(agent_chat, "create_session", new=AsyncMock(side_effect=_new_session)):
        result = await _call_tool(
            world.app.http_app(path="/mcp", stateless_http=True, json_response=True),
            "/mcp",
            _TOKEN,
            name=tool,
            arguments=arguments,
        )
    assert not result["result"].get("isError"), result
    session_id = _EXISTING if tool == "continue_turn" else _NEW
    assert not await _signed_in(db_session_factory, session_id), (
        "a send made while off can never name anyone once the option is turned on"
    )


@pytest.mark.parametrize("new_session", [True, False])
async def test_with_no_server_opted_in_no_earlier_send_is_read(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    new_session: bool,
) -> None:
    """Off means a send does what it did before: no transcript or record read for it."""
    world = await _world(db_session, db_session_factory, opted_in=False)
    person = dataclasses.replace(world.auth, signed_in_person=True)
    unread = AsyncMock(side_effect=AssertionError("nothing is read while off"))
    with (
        patch.object(_agent_chat_safety, "_read_turn", new=unread),
        patch.object(_agent_chat_safety, "_send_records", new=unread),
    ):
        assert await _agent_chat_safety.earlier_turn_open(
            world.runtime, person, _EXISTING, new_session=new_session
        ), "counted as open, so the send names nobody"
    unread.assert_not_awaited()


_KEY_PATHS: dict[str, Callable[[McpRuntime, AuthIdentity], Any]] = {
    "start_turn": lambda runtime, auth: _start_turn_impl(runtime, auth, "hi"),
    "continue_turn": lambda runtime, auth: _continue_turn_impl(runtime, auth, _EXISTING, "hi"),
    "ask": lambda runtime, auth: _ask_impl(runtime, auth, "hi"),
    "ask_continue": lambda runtime, auth: _ask_impl(runtime, auth, "hi", handle=_EXISTING),
}


@pytest.mark.parametrize("kind", ["agent", "cli", None])
@pytest.mark.parametrize("path", sorted(_KEY_PATHS))
async def test_a_turn_an_agent_coding_or_cli_key_drives_names_nobody(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    kind: Any,
    path: str,
) -> None:
    """A coding key is an agent key whose row says so; neither is a signed-in person,
    even for the account of the person whose earlier hub turn is recorded."""
    world = await _world(db_session, db_session_factory)
    session_id = _EXISTING if "continue" in path else _NEW
    await _mark(db_session_factory, session_id)
    auth = dataclasses.replace(world.auth, token_kind=kind)
    with patch.object(agent_chat, "create_session", new=AsyncMock(side_effect=_new_session)):
        await _KEY_PATHS[path](world.runtime, auth)
    assert not await _signed_in(db_session_factory, session_id), "a key names nobody"


_RELAY_SERVER = SignedIdentityServer(
    name="tools",
    url="https://tools.example.com/mcp",
    auth="bearer",
    workspaces=frozenset({"discord:g1"}),
    agents=("Agent A",),
    requester_meta=True,
)
_PUBLIC_URL = "https://daimon.example.com/mcp"


def _with_relay(runtime: McpRuntime, *, opted_in: bool) -> McpRuntime:
    settings = runtime.settings.model_copy(
        update={
            "mcp": runtime.settings.mcp.model_copy(
                update={"public_url": _PUBLIC_URL, "jwt_secret": SecretStr("s" * 32)}
            ),
            "mcp_identity": runtime.settings.mcp_identity.model_copy(
                update={
                    "servers": [_RELAY_SERVER.model_copy(update={"agent_chat_requester": opted_in})]
                }
            ),
        }
    )
    return dataclasses.replace(runtime, settings=settings)


@pytest.mark.parametrize("opted_in", [True, False])
async def test_a_new_session_binds_its_relay_vault_only_while_a_server_opted_in(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], opted_in: bool
) -> None:
    world = await _world(db_session, db_session_factory)
    runtime = _with_relay(world.runtime, opted_in=opted_in)
    session = ma_session(
        id=_NEW, environment_id="env_test", status="idle",
        vault_ids=["vlt_relay"],
        agent=ma_session_agent(
            id=AGENT_ID,
            mcp_servers=[{"type": "url", "name": "tools", "url": relay_url(_PUBLIC_URL, "tools")}],
        ),
    )  # fmt: skip
    refresh = AsyncMock()
    with (
        patch.object(agent_chat, "create_session", new=AsyncMock(return_value=session)),
        patch.object(agent_chat, "refresh_relay_vault", new=refresh),
    ):
        await _start_turn_impl(runtime, world.auth, "hi")
    if not opted_in:
        refresh.assert_not_awaited()
        return
    refresh.assert_awaited_once()
    assert refresh.await_args is not None
    assert refresh.await_args.kwargs["vault_id"] == "vlt_relay"
    assert refresh.await_args.kwargs["agent_chat"] == _NEW, "bound before the first send"


@pytest.mark.parametrize("opted_in", [True, False])
async def test_a_continue_binds_the_relay_vault_of_a_session_started_before(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], opted_in: bool
) -> None:
    """A session opened before this binding existed gains it on its next turn."""
    world = await _world(db_session, db_session_factory)
    runtime = _with_relay(world.runtime, opted_in=opted_in)
    refresh = AsyncMock()
    with patch.object(agent_chat, "refresh_mounted_mcp_vaults", new=refresh):
        await _continue_turn_impl(runtime, world.auth, _EXISTING, "hi")
    assert refresh.await_args is not None
    assert refresh.await_args.kwargs["agent_chat"] == (_EXISTING if opted_in else None)
