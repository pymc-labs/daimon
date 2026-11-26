"""Every path that sends or resumes an agent-chat turn records who drives it, and only a
person signed in to the hub is recorded as someone the identity relay may name
(`daimon.core.agent_chat_requester`)."""

from __future__ import annotations

import ast
import dataclasses
import re
import uuid
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
from daimon.core.agent_chat_requester import driver_signed_in
from daimon.core.config import SignedIdentityServer
from daimon.core.hub_identity import HubTenant
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_identity import relay_url
from daimon.core.stores import session_settle_state
from daimon.core.stores.domain import Role
from daimon.core.stores.session_settle_state import AgentChatSession, SessionProvenance
from daimon.testing import AGENT_ID, build_turn_router
from daimon.testing.factories import make_ledger_entry, make_platform_principal, make_tenant
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from mux.testing.drivers.anthropic.ma import build_fake_anthropic, session_response
from mux.testing.drivers.anthropic.ma_models import ma_session, ma_session_agent
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from ..hub.test_turn_parity import _call_tool, _runtime

#: A send whose session carries no provenance stamp.
NO_PROVENANCE = SessionProvenance(grant=None, channel_id=None, account_id=None)

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
                    k.arg == "earlier"
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
            sent = send.args[1] if _called(send) == "_send_billed" else send.args[2]
            assert any(
                k.arg == "provenance"
                and ast.unparse(k.value) == f"session_provenance({ast.unparse(sent)}.metadata)"
                for k in nearest.keywords
            ), f"{name}: the provenance is the stamp of the session this call sends to"


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


#: What decides whether a send names its driver: the driver itself, the sends begun and
#: recorded, and the recorded turn; and where the session came from, which the MCP server
#: finds it by. The settle columns are billing state, written by turn workers and the
#: settle outside the fence, and must never decide either.
_DRIVER_STATE = {
    "agent_chat_signed_in",
    "agent_chat_sends",
    "agent_chat_recorded_send",
    "unattended_turn_event_id",
    "agent_chat_grant",
    "agent_chat_channel",
    "agent_chat_account_id",
}
_REPO = _MCP_ROOT.parents[4].parent


def _source_functions() -> list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    """Every function in the non-test Python of packages/ and apps/."""
    found: list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]] = []
    for top in ("packages", "apps"):
        for path in sorted((_REPO / top).rglob("*.py")):
            if {"tests", ".venv", "node_modules", "versions"} & set(path.parts):
                continue
            for fn in ast.walk(ast.parse(path.read_text())):
                if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                    found.append((f"{path.relative_to(_REPO)}:{fn.name}", fn))
    return found


def _fenced(fn: ast.AST, call: ast.Call) -> bool:
    return any(
        isinstance(n, ast.AsyncWith)
        and any(
            isinstance(item.context_expr, ast.Call)
            and _called(item.context_expr) == "session_mutation_fence"
            for item in n.items
        )
        and _inside(call, n)
        for n in ast.walk(fn)
    )


#: Store functions that only read the driver state; any other that names it writes it.
_DRIVER_STATE_READERS = {
    "count_agent_chat_sends",
    "is_agent_chat_signed_in",
    "unattended_turn",
    "find_agent_chat_session",
    "count_agent_chat_provenance_for_account",
}
#: Writers outside the fence that only clear where a session came from, for an account's
#: erasure or its tenant's deletion; no send decides from those columns.
_PROVENANCE_ERASERS = {
    "erase_agent_chat_provenance_for_account",
    "erase_agent_chat_provenance_for_tenant",
}


def _strings(node: ast.AST) -> list[str]:
    """The string a node spells, if it is a literal, a concatenation of literals or the
    literal parts of an f-string."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _strings(node.left), _strings(node.right)
        return [a + b for a in left for b in right] if left and right else []
    if isinstance(node, ast.JoinedStr):
        return ["".join(v for part in node.values for v in _strings(part))]
    return []


def _names_driver_state(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Whether a function names a driver-state column anywhere but its docstring: as a
    keyword, an attribute (ORM expression or assignment), or inside a string (raw SQL, a
    built name, or the `agent_chat_` prefix such a name is built from)."""
    body = fn.body
    if body and isinstance(body[0], ast.Expr) and _strings(body[0].value):
        body = body[1:]
    for node in (n for stmt in body for n in ast.walk(stmt)):
        if isinstance(node, ast.keyword) and node.arg in _DRIVER_STATE:
            return True
        if isinstance(node, ast.Attribute) and node.attr in _DRIVER_STATE:
            return True
        for spelled in _strings(node):
            if "agent_chat_" in spelled or any(column in spelled for column in _DRIVER_STATE):
                return True
    return False


def test_the_state_that_names_a_driver_is_written_only_under_the_session_fence() -> None:
    """Each column the decision reads has one store writer, called from one helper,
    which is called only inside a session fence. No code deletes a row."""
    store = Path(session_settle_state.__file__)
    writers: set[str] = set()
    for fn in ast.parse(store.read_text()).body:
        if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        calls = {_called(n) for n in ast.walk(fn) if isinstance(n, ast.Call)}
        assert "delete" not in calls, f"{fn.name} deletes a settle-state row"
        if fn.name not in _DRIVER_STATE_READERS and _names_driver_state(fn):
            writers.add(fn.name)
    assert writers == {
        "record_agent_chat_driver",
        "record_agent_chat_provenance",
        "record_unattended_turn",
        *_PROVENANCE_ERASERS,
    }

    functions = _source_functions()

    def callers(name: str) -> set[str]:
        return {
            where
            for where, fn in functions
            if any(isinstance(n, ast.Call) and _called(n) == name for n in ast.walk(fn))
        }

    for writer in ("record_agent_chat_driver", "record_agent_chat_provenance"):
        assert callers(writer) == {
            "packages/core/daimon/core/agent_chat_requester.py:record_driver"
        }, writer
    assert callers("erase_agent_chat_provenance_for_tenant") == {
        "packages/core/daimon/core/stores/tenants.py:delete_tenant"
    }
    # The account eraser is an `erasure.ERASABLE` entry, named there and nowhere else.
    users = {
        str(path.relative_to(_REPO))
        for path in (_REPO / "packages").rglob("*.py")
        if not {"tests", ".venv"} & set(path.parts)
        and "erase_agent_chat_provenance_for_account" in path.read_text()
    }
    assert users == {
        "packages/core/daimon/core/erasure.py",
        "packages/core/daimon/core/stores/session_settle_state.py",
    }
    assert callers("record_unattended_turn") == {
        "packages/adapters/mcp/daimon/adapters/mcp/tools/_agent_chat_safety.py:record_turn"
    }
    sites = 0
    for where, fn in functions:
        for call in (n for n in ast.walk(fn) if isinstance(n, ast.Call)):
            if _called(call) in {"record_driver", "record_turn"} and where.endswith(
                ("_start_turn_impl", "_continue_turn_impl")
            ):
                sites += 1
                assert _fenced(fn, call), f"{where} line {call.lineno} writes outside the fence"
    assert callers("record_driver") | callers("record_turn") == {
        "packages/adapters/mcp/daimon/adapters/mcp/tools/agent_chat.py:_start_turn_impl",
        "packages/adapters/mcp/daimon/adapters/mcp/tools/agent_chat.py:_continue_turn_impl",
    }
    assert sites == 5, "start: driver and turn; continue: resume, driver and turn"


#: Non-test files that may name the agent-chat columns, each with its reason.
_NAMES_THE_COLUMNS = {
    "packages/core/daimon/core/stores/session_settle_state.py": "the one store",
    "packages/core/daimon/core/_models.py": "the mapping",
    "packages/core/daimon/core/stores/agent_rekey.py": "exempts them as not agent ids",
    "packages/core/alembic/versions/0230_agent_chat_signed_in.py": "adds a column",
    "packages/core/alembic/versions/0231_agent_chat_send_count.py": "adds two columns",
    "packages/core/alembic/versions/0232_agent_chat_session_grant.py": "adds three columns",
}


def test_no_other_file_names_the_agent_chat_columns() -> None:
    """Raw SQL, a migration or a second store would write these columns outside the
    fence without calling the store, so no other non-test file may name them."""
    columns = (
        "agent_chat_signed_in",
        "agent_chat_sends",
        "agent_chat_recorded_send",
        "agent_chat_grant",
        "agent_chat_channel",
        "agent_chat_account_id",
    )
    naming = sorted(
        str(path.relative_to(_REPO))
        for top in ("packages", "apps", "scripts")
        for path in (_REPO / top).rglob("*")
        if path.suffix in {".py", ".sql"}
        and not {"tests", ".venv", "node_modules", "__pycache__"} & set(path.parts)
        and re.search(rf"\b({'|'.join(columns)})\b", path.read_text(encoding="utf-8"))
    )
    assert naming == sorted(_NAMES_THE_COLUMNS)


def test_each_turn_records_the_number_its_own_driver_record_gave() -> None:
    """`record_turn` writes the number of the send it follows, never a count read later."""
    for where, fn in _source_functions():
        if not where.endswith(("_start_turn_impl", "_continue_turn_impl")):
            continue
        numbered = [
            n
            for n in ast.walk(fn)
            if isinstance(n, ast.Assign)
            and isinstance(n.value, ast.Await)
            and isinstance(n.value.value, ast.Call)
            and _called(n.value.value) == "record_driver"
        ]
        turns = [n for n in ast.walk(fn) if isinstance(n, ast.Call) and _called(n) == "record_turn"]
        assert turns, where
        for turn in turns:
            before = [a for a in numbered if a.lineno < turn.lineno]
            assert before, f"{where}: record_turn with no numbered driver record before it"
            target = max(before, key=lambda a: a.lineno).targets[0]
            assert any(
                k.arg == "send" and ast.unparse(k.value) == ast.unparse(target)
                for k in turn.keywords
            ), f"{where} line {turn.lineno}: the turn records its own send's number"


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
_SENT_AT = "2026-09-06T00:00:00Z"
_IDLE = {
    "id": "sevt_idle",
    "type": "session.status_idle",
    "stop_reason": {"type": "end_turn", "event_ids": []},
    "processed_at": "2026-09-06T00:01:00Z",
}
_REPLY = {
    "id": "sevt_reply",
    "type": "agent.message",
    "content": [{"type": "text", "text": "done"}],
}


def _boundary(event_id: str, processed_at: str | None = _SENT_AT) -> dict[str, Any]:
    return {
        "id": event_id,
        "type": "user.message",
        "content": [{"type": "text", "text": "hi"}],
        "processed_at": processed_at,
    }


async def _world(
    db_session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    *,
    opted_in: bool = True,
    stamp: dict[str, str] | None = None,
) -> _World:
    """A hub person `u1`, with a server named for agent-chat requesters unless not
    `opted_in`. `stamp` adds to the metadata of every session the provider returns."""
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
            metadata={
                "daimon_agent_chat": "true",
                "daimon_account": str(principal.account_id),
                **(stamp or {}),
            },
        ),
    )
    router.add(
        "GET",
        r"/v1/sessions/([^/]+)/events$",
        lambda _r, _m: httpx.Response(200, json={"data": list(events), "next_page": None}),
    )
    # The earlier agent-chat send on the existing session, recorded and seen to end.
    await _recorded_send(factory, _EXISTING, _EARLIER)
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


async def _recorded_send(
    factory: async_sessionmaker[AsyncSession], session_id: str, turn_event_id: str
) -> None:
    """A key's send recorded as the send paths record it: counted, then its turn."""
    async with factory.begin() as db:
        send = await session_settle_state.record_agent_chat_driver(
            db, managed_session_id=session_id, provenance=NO_PROVENANCE, signed_in=False, send=True
        )
        await session_settle_state.record_unattended_turn(
            db,
            managed_session_id=session_id,
            turn_event_id=turn_event_id,
            rules_channel_id=None,
            send=send,
        )


async def _mark(factory: async_sessionmaker[AsyncSession], session_id: str) -> None:
    """A signed-in person's earlier turn, which a later driver must replace."""
    async with factory.begin() as db:
        sends = await session_settle_state.count_agent_chat_sends(db, managed_session_id=session_id)
        await session_settle_state.record_agent_chat_driver(
            db,
            managed_session_id=session_id,
            provenance=NO_PROVENANCE,
            signed_in=True,
            send=True,
            new_session=sends is None,
            sends_read=sends.begun if sends is not None else None,
        )
    assert await _signed_in(factory, session_id)


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


@pytest.mark.parametrize("opted_in", [True, False])
@pytest.mark.parametrize("tool", ["start_turn", "continue_turn"])
async def test_every_send_records_where_its_session_came_from(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tool: str,
    opted_in: bool,
) -> None:
    """With a relay server opted in or not, the MCP server can then find the session by its
    credential's grant, for the account that created it only."""
    stamp = {"daimon_chat_session_id": "grant-1", "daimon_channel": "chan-k"}
    world = await _world(db_session, db_session_factory, opted_in=opted_in, stamp=stamp)
    account = str(world.auth.account_id)
    created = ma_session(
        id=_NEW, agent_id=AGENT_ID, environment_id="env_test", status="idle",
        metadata={**stamp, "daimon_agent_chat": "true", "daimon_account": account},
    )  # fmt: skip
    arguments = {"daimon_id": world.daimon_id, "message": "status?"}
    if tool == "continue_turn":
        arguments["handle"] = _EXISTING
    with patch.object(agent_chat, "create_session", new=AsyncMock(return_value=created)):
        result = await _call_tool(
            world.app.http_app(path="/mcp", stateless_http=True, json_response=True),
            "/mcp",
            _TOKEN,
            name=tool,
            arguments=arguments,
        )
    assert not result["result"].get("isError"), result
    async with db_session_factory() as db:
        found = await session_settle_state.find_agent_chat_session(
            db, grant="grant-1", account_id=world.auth.account_id
        )
        other = await session_settle_state.find_agent_chat_session(
            db, grant="grant-1", account_id=uuid.uuid4()
        )
    session_id = _EXISTING if tool == "continue_turn" else _NEW
    assert found == AgentChatSession(managed_session_id=session_id, channel_id="chan-k")
    assert other is None, "another account's credential does not find it"


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
    await _recorded_send(db_session_factory, _EXISTING, "sevt_key_send")
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
        await _recorded_send(db_session_factory, _NEW, "sevt_key_send")
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
            sends = await session_settle_state.count_agent_chat_sends(db, managed_session_id=_NEW)
        assert sends == session_settle_state.AgentChatSends(
            begun=1, recorded=None, turn_event_id=None
        ), "sent, not recorded"
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


async def test_a_worker_keeping_its_settle_request_cannot_hide_a_key_send(
    db_session: AsyncSession, pooled_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """The earlier turn's worker reads no pending settle request, a key's send (accepted,
    then a 5xx) writes its own, and the worker's upsert names it after the earlier turn.
    Billing state then looks settled; the send count still shows the key's send."""
    world = await _world(db_session, pooled_sessionmaker)
    real = session_settle_state.settle_request
    reads = {"n": 0}

    async def key_sends_after_the_workers_read(
        session: AsyncSession, *, managed_session_id: str
    ) -> session_settle_state.PendingSettle | None:
        pending = await real(session, managed_session_id=managed_session_id)
        reads["n"] += 1
        if reads["n"] == 1:
            await _key_send_accepted_unrecorded(world, _EXISTING)
        return pending

    with patch.object(session_settle_state, "settle_request", new=key_sends_after_the_workers_read):
        await _agent_chat_safety._keep_settle_request(  # pyright: ignore[reportPrivateUsage]
            world.runtime, _EXISTING, _EARLIER
        )
    async with pooled_sessionmaker() as db:
        pending = await session_settle_state.settle_request(db, managed_session_id=_EXISTING)
    assert pending is not None and pending.turn_event_id == _EARLIER, "the key's request is hidden"
    result = await _call_tool(
        world.app.http_app(path="/mcp", stateless_http=True, json_response=True),
        "/mcp",
        _TOKEN,
        name="continue_turn",
        arguments={"daimon_id": world.daimon_id, "handle": _EXISTING, "message": "and?"},
    )
    assert not result["result"].get("isError"), result
    assert not await _signed_in(pooled_sessionmaker, _EXISTING), "the key's turn may still run"


@pytest.mark.parametrize("before", ["a failed send", "a session from before the count"])
async def test_one_turn_names_nobody_after_an_unrecorded_send_then_names_again(
    db_session: AsyncSession,
    pooled_sessionmaker: async_sessionmaker[AsyncSession],
    before: str,
) -> None:
    """A 5xx Managed Agents never accepted looks like one it accepted, and a session
    started before sends were counted has no count: the next send names nobody (and
    logs it), and records its own turn, so the send after that is named again."""
    world = await _world(db_session, pooled_sessionmaker)
    if before == "a failed send":
        await _key_send_accepted_unrecorded(world, _EXISTING)
    else:
        # The row as a release without the count left it.
        async with pooled_sessionmaker.begin() as db:
            await db.execute(
                text(
                    "UPDATE usage_reconcile_progress SET agent_chat_sends = NULL,"
                    " agent_chat_recorded_send = NULL WHERE managed_session_id = :s"
                ),
                {"s": _EXISTING},
            )

    async def continue_turn() -> bool:
        result = await _call_tool(
            world.app.http_app(path="/mcp", stateless_http=True, json_response=True),
            "/mcp",
            _TOKEN,
            name="continue_turn",
            arguments={"daimon_id": world.daimon_id, "handle": _EXISTING, "message": "and?"},
        )
        assert not result["result"].get("isError"), result
        return await _signed_in(pooled_sessionmaker, _EXISTING)

    with capture_logs() as logs:
        assert not await continue_turn(), "the earlier send may still be running"
    assert [e["event"] for e in logs].count("agent_chat.requester_withheld") == 1
    world.events[:] = [_IDLE, _REPLY, _boundary("sevt_boundary")]  # that turn ended
    assert await continue_turn(), "named again once a send was recorded and seen to end"


@pytest.mark.parametrize("new_session", [True, False])
async def test_a_send_begun_is_open_until_its_turn_is_recorded(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    new_session: bool,
) -> None:
    """The decision itself, without the start's write: a counted send with no recorded
    turn is open on a new session and on an existing one."""
    world = await _world(db_session, db_session_factory)
    person = dataclasses.replace(world.auth, signed_in_person=True)
    session_id = _NEW if new_session else _EXISTING
    if new_session:
        assert not (
            await _agent_chat_safety.earlier_turn_open(
                world.runtime, person, session_id, new_session=True
            )
        ).open, "nothing sent yet"
    else:
        assert not (
            await _agent_chat_safety.earlier_turn_open(
                world.runtime, person, session_id, new_session=False
            )
        ).open, "the recorded send was seen to end"
    async with db_session_factory.begin() as db:
        await session_settle_state.record_agent_chat_driver(
            db, managed_session_id=session_id, provenance=NO_PROVENANCE, signed_in=False, send=True
        )
    assert (
        await _agent_chat_safety.earlier_turn_open(
            world.runtime, person, session_id, new_session=new_session
        )
    ).open


@pytest.mark.parametrize(
    ("queued_at", "idle_at"),
    [
        (None, _IDLE["processed_at"]),  # still queued
        ("not a time", _IDLE["processed_at"]),  # unparseable
        (_SENT_AT, "2026-09-05T23:59:00Z"),  # the idle was processed before it
        (_SENT_AT, None),  # the idle has no time
        (_IDLE["processed_at"], _IDLE["processed_at"]),  # picked up as the idle was stamped
    ],
)
async def test_a_key_message_queued_behind_the_earlier_turn_names_nobody(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    queued_at: str | None,
    idle_at: Any,
) -> None:
    """A key's message sent while the earlier turn ran is counted and recorded, but
    queued: listed before that turn's idle, it must not read as its own turn ended.
    Only the provider's processing times show the order, not the list."""
    world = await _world(db_session, db_session_factory)
    await _recorded_send(db_session_factory, _EXISTING, "sevt_key_send")
    idle = {**_IDLE, "processed_at": idle_at}
    world.events[:] = [idle, _REPLY, _boundary("sevt_key_send", queued_at), _boundary(_EARLIER)]
    result = await _call_tool(
        world.app.http_app(path="/mcp", stateless_http=True, json_response=True),
        "/mcp",
        _TOKEN,
        name="continue_turn",
        arguments={"daimon_id": world.daimon_id, "handle": _EXISTING, "message": "and?"},
    )
    assert not result["result"].get("isError"), result
    assert not await _signed_in(db_session_factory, _EXISTING), "the key's turn may run next"


async def test_a_key_send_between_a_continues_read_and_write_names_nobody(
    db_session: AsyncSession, pooled_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """If the fence were lost, a key's whole send could land between the person's read
    and write; the write names the person only if the count it read is unchanged."""
    world = await _world(db_session, pooled_sessionmaker)
    real = _agent_chat_safety.earlier_turn_open

    async def read_then_key_sends(
        runtime: McpRuntime, auth: AuthIdentity, handle: str, *, new_session: bool
    ) -> Any:
        earlier = await real(runtime, auth, handle, new_session=new_session)
        assert not earlier.open, "read as ended"
        await _recorded_send(pooled_sessionmaker, handle, "sevt_key_send")
        return earlier

    with patch.object(agent_chat, "earlier_turn_open", new=read_then_key_sends):
        result = await _call_tool(
            world.app.http_app(path="/mcp", stateless_http=True, json_response=True),
            "/mcp",
            _TOKEN,
            name="continue_turn",
            arguments={"daimon_id": world.daimon_id, "handle": _EXISTING, "message": "and?"},
        )
    assert not result["result"].get("isError"), result
    assert not await _signed_in(pooled_sessionmaker, _EXISTING), "the key's turn runs next"


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
        patch.object(_agent_chat_safety, "_agent_chat_sends", new=unread),
    ):
        assert (
            await _agent_chat_safety.earlier_turn_open(
                world.runtime, person, _EXISTING, new_session=new_session
            )
        ).open, "counted as open, so the send names nobody"
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
