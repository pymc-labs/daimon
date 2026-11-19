"""Press-applies confirmation on the MCP side, on real Postgres.

A provider turn has no confirmation card, so a gated tool is recorded and carded
instead of run, and the wake turn's one-shot credential later runs exactly that call.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import Any, Literal, cast

import jwt as pyjwt
import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.confirmed_call import host_confirmed_call
from daimon.adapters.mcp.middleware.host_call_gate import (
    MISMATCH_TEXT,
    REACH_PLAN_TEXT,
    HostCallGate,
)
from daimon.adapters.mcp.middleware.mcp_identity import IdentityMiddleware
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._outbound import TurnScope, approve
from daimon.core import host_calls
from daimon.core.authz import Subject
from daimon.core.config import AnthropicSettings, DatabaseSettings, McpSettings, Settings
from daimon.core.confirmation import ServerSkillUpdateCard, confirmed_tools_for_turn
from daimon.core.host_calls import ConfirmedCallRunner, confirmed_call_prefix, run_confirmed_calls
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.outbound.reach import ReachPlan, ReachTally, turn_tally_key
from daimon.core.session_seal import SEAL_CLAIM
from daimon.core.stores import agent_tool_rules
from daimon.core.stores.domain import Role
from daimon.core.stores.host_call_cards import HostCallCardRow, list_cards
from daimon.core.stores.mcp_tokens import create_mcp_token_row, get_mcp_token
from daimon.core.stores.reach_tallies import lock_tally, save_tally
from daimon.core.stores.turn_origins import create_origin, get_session_origin
from daimon.core.tool_safety import (
    CONFIRMED_DAIMON_TOOLS,
    DAIMON_SERVER_NAME,
    ToolCall,
    ToolSafetyPolicy,
    confirmation_gated_tools,
)
from daimon.core.turn.approvals import agent_rule_lookup, interactive_decider
from daimon.core.turn.unattended_capabilities import protected_tools
from daimon.core.turn_origin import ExecutionFlags, current_execution_origin_id, execution_origin
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.server.transforms.search import BM25SearchTransform
from fastmcp.tools import Tool, ToolResult
from mcp.types import TextContent
from pydantic import HttpUrl, PostgresDsn, PrivateAttr, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_SECRET = "s" * 32
_URL = "https://mcp.example.com/mcp"
_USER = "user-1"
_MA_AGENT = "agent_x"
_THREAD = "thread-1"
_CHANNEL = "channel-1"
_MESSAGE = "msg-1"


_Ran = list[tuple[str, dict[str, Any], uuid.UUID | None]]


@dataclass
class Rig:
    """One thread's world: who is asking, what ran, and what the card posted."""

    sessions: async_sessionmaker[AsyncSession]
    tenant_id: uuid.UUID
    account_id: uuid.UUID
    agent_id: uuid.UUID
    grant: str
    middleware: IdentityMiddleware
    ran: _Ran
    posted: list[object]
    failure: list[Exception]
    #: More gated tools to serve as no-argument stubs under their real names.
    extra: tuple[str, ...] = ()

    def identity(self, **changes: Any) -> AuthIdentity:
        base = AuthIdentity(
            account_id=self.account_id,
            tenant_id=self.tenant_id,
            role=Role.USER,
            platform="discord",
            chat_agent_id=self.agent_id,
            platform_user_id=_USER,
            chat_session_id=self.grant,
            is_chat_execution=True,
            no_confirmation_surface=True,
        )
        return replace(base, **changes)

    def server(self, identity: AuthIdentity) -> FastMCP:
        """A server whose every request is dispatched as `identity`."""
        server = FastMCP("test")

        def delete_allowance(allowance_id: str) -> str:
            return self._record("delete_allowance", {"allowance_id": allowance_id})

        def revoke_web_link(kind: str) -> str:
            return self._record("revoke_web_link", {"kind": kind})

        def approve_reach(threads: int, places: int, people: int, where: str) -> str:
            plan = {"threads": threads, "places": places, "people": people, "where": where}
            return self._record("approve_reach", plan)

        def list_agents() -> str:
            return self._record("list_agents", {})

        def set_thread_model(model: str, revision: int | None = None) -> str:
            return self._record("set_thread_model", {"model": model, "revision": revision})

        for fn in (delete_allowance, revoke_web_link, approve_reach, list_agents, set_thread_model):
            server.tool(fn)
        for name in self.extra:
            field = CONFIRMED_DAIMON_TOOLS.get(name)
            properties = {} if field is None else {field: {"type": "string"}}
            schema = {"type": "object", "properties": properties, "additionalProperties": True}
            stub = _Stub(name=name, parameters=schema)
            stub._ran = self._record  # pyright: ignore[reportPrivateUsage]
            server.add_tool(stub)
        # The stock proxy: it re-enters the middleware with the inner tool's name.
        server.add_transform(BM25SearchTransform(max_results=5))
        server.add_middleware(_Dispatch(self.middleware, identity))
        return server

    def _record(self, name: str, args: dict[str, Any]) -> str:
        self.ran.append((name, args, host_confirmed_call.get()))
        if self.failure:
            raise self.failure[0]
        return f"{name} done"


class _Stub(Tool):
    """A gated tool under its real name, taking its confirmation field."""

    _ran: Callable[[str, dict[str, Any]], str] = PrivateAttr()

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        return ToolResult(content=[TextContent(type="text", text=self._ran(self.name, arguments))])


class _Dispatch(Middleware):
    """Stands in for the identity edge: one fixed identity into `_dispatch_for_backend`."""

    def __init__(self, middleware: IdentityMiddleware, identity: AuthIdentity) -> None:
        self._middleware = middleware
        self._identity = identity

    async def on_request(self, context: MiddlewareContext, call_next: CallNext) -> object:
        return await self._middleware._dispatch_for_backend(  # pyright: ignore[reportPrivateUsage]
            context, call_next, self._identity
        )


def _settings(**features: str) -> Settings:
    return Settings(
        database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
        anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
        mcp=McpSettings(jwt_secret=SecretStr(_SECRET), public_url=HttpUrl(_URL)),
        features=features,
    )


async def _none(_: MiddlewareContext) -> str | None:
    return None


@pytest.fixture
async def rig(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> Rig:
    now = dt.datetime.now(dt.UTC)
    grant = f"grant-{uuid.uuid4()}"
    async with committing_sessionmaker.begin() as session:
        tenant = await make_tenant(session, platform="discord", workspace_id="guild-1")
        account = await make_account(session, tenant=tenant)
        await make_platform_principal(
            session, platform="discord", external_id=_USER, tenant=tenant, account=account
        )
        await create_origin(
            session,
            tenant_id=tenant.id,
            account_id=account.id,
            platform="discord",
            parent_channel_id=_CHANNEL,
            thread_id=_THREAD,
            responder_ma_agent_id=_MA_AGENT,
            responder_name="Researcher",
            configuration_target_ma_agent_id=None,
            configuration_target_name=None,
            role=Role.USER,
            expires_at=now + dt.timedelta(hours=1),
            now=now,
            sender_foreign=False,
            shared_channel=False,
            chat_session_id=grant,
        )
    posted: list[object] = []

    async def post_switch_card(*_: object, card: object, **__: object) -> str:
        posted.append(card)
        return _MESSAGE

    monkeypatch.setattr(
        "daimon.adapters.mcp.tools.discord._switch_card.post_switch_card", post_switch_card
    )
    middleware = IdentityMiddleware(
        subject_resolver=_none,
        tenant_resolver=_none,
        role_resolver=_none,
        agent_id_resolver=_none,
        is_admin_resolver=_none,
        internal_resolver=_none,
        sessionmaker=committing_sessionmaker,
    )
    return Rig(
        sessions=committing_sessionmaker,
        tenant_id=tenant.id,
        account_id=account.id,
        agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=_MA_AGENT),
        grant=grant,
        middleware=middleware,
        ran=[],
        posted=posted,
        failure=[],
    )


def _attach(rig: Rig, **features: str) -> None:
    runtime = SimpleNamespace(session_factory=rig.sessions, settings=_settings(**features))
    rig.middleware.attach_host_call_gate(HostCallGate(runtime, public_url=_URL))  # pyright: ignore[reportArgumentType]


async def _call(
    rig: Rig, identity: AuthIdentity, name: str, args: dict[str, Any], *, proxy: bool = False
) -> str:
    """Call `name` as `identity`, directly or through the `call_tool` proxy; the result text."""
    async with Client(rig.server(identity)) as client:
        if proxy:
            result = await client.call_tool("call_tool", {"name": name, "arguments": args})
        else:
            result = await client.call_tool(name, args)
    return "".join(getattr(part, "text", "") for part in result.content)


async def _cards(rig: Rig) -> list[HostCallCardRow]:
    async with rig.sessions() as session:
        return await list_cards(session, tenant_id=rig.tenant_id)


async def _card(rig: Rig) -> HostCallCardRow:
    rows = await _cards(rig)
    assert len(rows) == 1, f"one card was recorded: {len(rows)}"
    return rows[0]


_ARGS = {"allowance_id": "a1"}


async def _approved(
    rig: Rig, args: dict[str, Any] = _ARGS, name: str = "delete_allowance"
) -> uuid.UUID:
    """Ask for a gated call and press Approve on its card, as the requester does."""
    _attach(rig)
    await _call(rig, rig.identity(), name, args)
    card = await _card(rig)

    async def admin(_: str) -> bool:
        return True

    pressed = await host_calls.press_host_call(
        sessionmaker=rig.sessions,
        enabled=True,
        tenant_id=rig.tenant_id,
        card_id=card.id,
        action="approve",
        clicker=_USER,
        message_id=_MESSAGE,
        current_admin=admin,
        now=dt.datetime.now(dt.UTC),
    )
    assert pressed is not None and pressed.approved, f"the requester's press approves: {pressed}"
    rig.ran.clear()
    return card.id


async def _credential(rig: Rig, card_id: uuid.UUID) -> AuthIdentity:
    """The one-shot identity a wake mints: registered token row, naming the card."""
    jti = uuid.uuid4()
    async with rig.sessions.begin() as session:
        await create_mcp_token_row(
            session,
            jti=jti,
            account_id=rig.account_id,
            tenant_id=rig.tenant_id,
            agent_id=str(rig.agent_id),
            label=None,
            created_at=dt.datetime.now(dt.UTC),
        )
    return rig.identity(confirmed_call_id=card_id, token_jti=jti, no_confirmation_surface=False)


async def _revoked(rig: Rig, identity: AuthIdentity) -> bool:
    assert identity.token_jti is not None, "the credential has a token row"
    async with rig.sessions() as session:
        row = await get_mcp_token(session, jti=identity.token_jti)
    assert row is not None, "the token row exists"
    return row.revoked_at is not None


# --- the gate records and cards a gated call ---------------------------------------


@pytest.mark.parametrize("proxy", [False, True], ids=["direct", "call_tool_proxy"])
async def test_ask_records_and_cards_a_gated_tool_when_called_by_name_or_proxy(
    rig: Rig, proxy: bool
) -> None:
    """The proxy re-enters with the inner name, so it is gated like a direct call."""
    _attach(rig)

    text = await _call(rig, rig.identity(), "delete_allowance", _ARGS, proxy=proxy)

    card = await _card(rig)
    assert text == host_calls.waiting_text("the person who asked"), f"waiting result: {text}"
    assert card.tool_name == "delete_allowance", "gated by the inner tool's name, not call_tool"
    assert card.status == "pending", "the call waits for the person"
    assert card.message_id == _MESSAGE, "the posted card is the one the press must match"
    assert len(rig.posted) == 1, "one Approve card is posted"
    assert rig.ran == [], "the tool body does not run before the press"


@pytest.mark.parametrize(
    "changes",
    [
        {"chat_session_id": None},
        {"chat_session_id": "grant-with-no-live-origin"},
        {"chat_agent_id": uuid.uuid4()},
        {"platform_user_id": None},
    ],
    ids=["no_claim", "no_live_origin", "responder_changed", "no_user"],
)
async def test_ask_refuses_when_the_call_cannot_be_tied_to_a_live_turn(
    rig: Rig, changes: dict[str, Any]
) -> None:
    """A provider-turn call with no session claim or no live origin fails closed."""
    _attach(rig)

    with pytest.raises(ToolError, match=host_calls.REFUSED_TEXT):
        await _call(rig, rig.identity(**changes), "delete_allowance", _ARGS)

    assert await _cards(rig) == [], "no record is kept for a refused call"
    assert rig.posted == [], "no card is posted"
    assert rig.ran == [], "the tool does not run"


_REACH = {"threads": 3, "places": 2, "people": 0, "where": "#general and #news"}


_DIGEST = "a" * 64


#: Gated tools the rig serves with real bodies; the rest are stubs.
_REAL_TOOLS = {"delete_allowance", "revoke_web_link", "approve_reach"}


#: Every tool that needs a card, with arguments its stub takes.
_GATED_ARGS = {
    "delete_allowance": _ARGS,
    "revoke_web_link": {"kind": "all"},
    "approve_reach": _REACH,
    # What these cards need to read as a configuration change.
    "request_channel_pin": {"preview_digest": _DIGEST, "expected_revision": 0, "model": "m"},
    "request_channel_pin_clear": {"preview_digest": _DIGEST, "expected_revision": 1},
    "request_provider_allowlist": {
        "preview_digest": _DIGEST,
        "expected_revision": "r1",
        "providers": ["openai"],
    },
}


async def skill_update(_: ToolCall) -> ServerSkillUpdateCard:
    return ServerSkillUpdateCard(name="s", version="1", new_version="2", changes={})


async def _claude_outcome(
    rig: Rig, policy: ToolSafetyPolicy, name: str, args: dict[str, Any]
) -> str:
    """What Claude's own decider does with the call: 'asked', 'ran' or 'refused'."""
    asked: list[object] = []

    async def confirm(prompt: object) -> Literal["denied"]:
        asked.append(prompt)
        return "denied"

    deps = SimpleNamespace(backend_selection_enabled=True, microsoft_send=True, tool_safety=policy)
    decide = interactive_decider(
        policy,
        requester_platform_user_id=_USER,
        confirm=confirm,  # pyright: ignore[reportArgumentType]
        trusted_servers=frozenset({DAIMON_SERVER_NAME}),
        confirmed_tools=confirmed_tools_for_turn(deps) | protected_tools(policy),  # pyright: ignore[reportArgumentType]
        rules_for=agent_rule_lookup(
            rig.sessions,
            tenant_id=rig.tenant_id,
            agent_id=_MA_AGENT,
            server_urls={DAIMON_SERVER_NAME: _URL},
            also_urls={DAIMON_SERVER_NAME: _URL},
            channel_id=_CHANNEL,
        ),
        daimon_rules=True,
        # Claude's card shows the skill's diff; a host card shows the call's own arguments.
        skill_update_for=skill_update,
    )
    call = ToolCall(
        tool_use_id="claude", server_name=DAIMON_SERVER_NAME, tool_name=name, input=args
    )
    result = await decide(call)
    return "ran" if result.allow else "asked" if asked else "refused"


async def _set_rule(rig: Rig, name: str, rule: Literal["ask", "deny"], channel: str | None) -> None:
    async with rig.sessions.begin() as session:
        await agent_tool_rules.set_rule(
            session,
            tenant_id=rig.tenant_id,
            agent_id=_MA_AGENT,
            agent_name="Researcher",
            tool_key=f"{DAIMON_SERVER_NAME}/{name}",
            channel_id=channel,
            server_url=_URL,
            rule=rule,
            set_by="admin",
            actor=Subject(is_admin=True),
        )


@pytest.mark.parametrize(
    ("safety", "rule"),
    [("off", None), ("on", None), ("on", "ask"), ("on", "channel_deny")],
)
@pytest.mark.parametrize("name", sorted(confirmation_gated_tools()))
async def test_each_gated_tool_asks_and_runs_as_on_claude(
    rig: Rig, name: str, safety: str, rule: str | None
) -> None:
    """OpenAI and Gemini turns share this credential. Each gated tool gets the outcome
    Claude's own decider gives it: a card, then one run after the press, or run or refused."""
    rig.extra = tuple(sorted(confirmation_gated_tools() - _REAL_TOOLS))
    field = CONFIRMED_DAIMON_TOOLS.get(name)
    args = _GATED_ARGS.get(name, {} if field is None else {field: "x"})
    on = safety == "on"
    settings = _settings()
    policy = settings.tool_safety.model_copy(update={"enabled": on, "agent_rules": on})
    settings = settings.model_copy(update={"tool_safety": policy})
    runtime = SimpleNamespace(session_factory=rig.sessions, settings=settings)
    rig.middleware.attach_host_call_gate(HostCallGate(runtime, public_url=_URL))  # pyright: ignore[reportArgumentType]
    if rule is not None:
        await _set_rule(
            rig, name, "ask" if rule == "ask" else "deny", None if rule == "ask" else _CHANNEL
        )
    expected = await _claude_outcome(rig, policy, name, args)

    try:
        await _call(rig, rig.identity(), name, args)
        carded = len(await _cards(rig)) == 1
        actual = "asked" if carded else "ran" if rig.ran else "refused"
    except ToolError:
        actual = "refused"
    assert actual == expected, f"{name} ({safety}, {rule}): the gate and Claude's decider differ"
    if actual != "asked":
        return
    card = await _card(rig)
    assert rig.ran == [], "carded, not run"

    async def admin(_: str) -> bool:
        return True

    pressed = await host_calls.press_host_call(
        sessionmaker=rig.sessions,
        enabled=True,
        tenant_id=rig.tenant_id,
        card_id=card.id,
        action="approve",
        clicker=_USER,
        message_id=_MESSAGE,
        current_admin=admin,
        now=dt.datetime.now(dt.UTC),
    )
    assert pressed is not None and pressed.approved, pressed
    await _call(rig, await _credential(rig, card.id), name, args)
    assert [ran for ran, _, _ in rig.ran] == [name], "ran once after the press"
    assert (await _card(rig)).status == "applied"


async def test_ask_refuses_a_malformed_reach_plan_before_any_card(rig: Rig) -> None:
    """The card would understate what approve_reach stores: nothing is asked."""
    _attach(rig)

    with pytest.raises(ToolError, match=REACH_PLAN_TEXT):
        await _call(rig, rig.identity(), "approve_reach", {**_REACH, "threads": -1})

    assert await _cards(rig) == [], "no record for a malformed plan"
    assert rig.posted == [], "no card is posted"


async def test_approve_moves_the_asking_turns_reach_tally_to_the_wake_turn_once(
    rig: Rig,
) -> None:
    """The press runs approve_reach in the wake turn; what the asking turn reached counts once."""
    card_id = await _approved(rig, _REACH, "approve_reach")
    origin_id = (await _card(rig)).origin_id
    asked, woke = turn_tally_key(origin_id), "turn:wake"
    async with rig.sessions.begin() as session:
        await lock_tally(session, tenant_id=rig.tenant_id, turn_key=asked)
        await save_tally(
            session, tenant_id=rig.tenant_id, turn_key=asked, tally=ReachTally(own_threads=2)
        )
    runtime = cast("McpRuntime", SimpleNamespace(session_factory=rig.sessions))
    auth = await _credential(rig, card_id)
    scope = TurnScope(key=woke, kind="live", own_places=frozenset(), requester_id=_USER)
    plan = ReachPlan(threads=3, places=2, people=0)
    token = host_confirmed_call.set(card_id)
    try:
        await approve(runtime, auth, scope, plan)
        await approve(runtime, auth, scope, plan)
    finally:
        host_confirmed_call.reset(token)

    async with rig.sessions.begin() as session:
        moved, approved = await lock_tally(session, tenant_id=rig.tenant_id, turn_key=woke)
        left, _ = await lock_tally(session, tenant_id=rig.tenant_id, turn_key=asked)
    assert moved == ReachTally(own_threads=2), "the asking turn's reach counts once, in the wake"
    assert approved == plan, "the plan is approved for the wake turn"
    assert left == ReachTally(), "nothing stays under the asking turn"


async def test_ask_refuses_when_the_host_call_gate_is_off(rig: Rig) -> None:
    """With the feature off, today's refusal stands."""
    _attach(rig, host_call_gate="off")

    with pytest.raises(ToolError, match=host_calls.REFUSED_TEXT):
        await _call(rig, rig.identity(), "delete_allowance", _ARGS)

    assert await _cards(rig) == [], "no record with the gate off"
    assert rig.posted == [], "no card with the gate off"
    assert rig.ran == [], "the tool does not run with the gate off"


async def test_dispatch_refuses_a_gated_tool_when_no_gate_is_attached(rig: Rig) -> None:
    """A partial server with no gate refuses as before and never runs the tool."""
    with pytest.raises(ToolError, match="needs a confirmation card"):
        await _call(rig, rig.identity(), "delete_allowance", _ARGS)

    assert rig.ran == [], "the tool does not run"


async def test_dispatch_runs_an_ungated_tool_through_the_proxy(rig: Rig) -> None:
    """Only gated tools wait for a card; reads run."""
    _attach(rig)

    await _call(rig, rig.identity(), "list_agents", {}, proxy=True)

    assert [name for name, _, _ in rig.ran] == ["list_agents"], "the read ran"
    assert await _cards(rig) == [], "a read records no card"


async def test_a_thread_model_pick_runs_and_only_its_move_cards(rig: Rig) -> None:
    """A pick or a move's preview runs at once; the move's confirming call waits on the card."""
    _attach(rig)

    await _call(rig, rig.identity(), "set_thread_model", {"model": "openai:gpt-5"})
    assert [name for name, _, _ in rig.ran] == ["set_thread_model"], "the pick ran"
    assert await _cards(rig) == [], "a pick records no card"

    await _call(rig, rig.identity(), "set_thread_model", {"model": "openai:gpt-5", "revision": 0})
    assert len(rig.ran) == 1, "the move waits for the card"
    assert (await _card(rig)).tool_name == "set_thread_model", "the move is carded"


async def test_a_thread_model_pick_is_refused_with_the_gate_off(rig: Rig) -> None:
    """With the gate off a move can't be carded, so the tool is refused, as each gated tool."""
    _attach(rig, host_call_gate="off")

    with pytest.raises(ToolError, match=host_calls.REFUSED_TEXT):
        await _call(rig, rig.identity(), "set_thread_model", {"model": "openai:gpt-5"})

    assert rig.ran == [] and await _cards(rig) == [], "nothing runs or cards with the gate off"


# --- the one-shot credential runs only its own call --------------------------------


async def test_confirmed_credential_runs_its_call_once_and_is_revoked(rig: Rig) -> None:
    """The consumed call runs under its own card, finishes applied and spends the token."""
    card_id = await _approved(rig)
    identity = await _credential(rig, card_id)

    text = await _call(rig, identity, "delete_allowance", _ARGS)

    card = await _card(rig)
    assert text == "delete_allowance done", f"the tool's own result: {text}"
    assert rig.ran == [("delete_allowance", _ARGS, card_id)], "ran once, as the approved card"
    assert card.status == "applied", f"finished applied: {card.status}"
    assert await _revoked(rig, identity), "the credential is revoked after its call"
    with pytest.raises(ToolError, match=MISMATCH_TEXT):
        await _call(rig, identity, "delete_allowance", _ARGS)
    assert len(rig.ran) == 1, "a second use of the credential runs nothing"


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("revoke_web_link", {"kind": "all"}),
        ("delete_allowance", {"allowance_id": "another"}),
    ],
    ids=["other_tool", "other_digest"],
)
async def test_confirmed_credential_refuses_any_other_call(
    rig: Rig, name: str, args: dict[str, Any]
) -> None:
    """A credential for one approved call cannot run another tool or other arguments."""
    card_id = await _approved(rig)
    identity = await _credential(rig, card_id)

    with pytest.raises(ToolError, match=MISMATCH_TEXT):
        await _call(rig, identity, name, args)

    assert rig.ran == [], "nothing ran"
    assert (await _card(rig)).status == "approved", "the approved call is still waiting"


async def test_confirmed_credential_marks_the_card_failed_when_the_tool_fails(rig: Rig) -> None:
    """A tool's own refusal is recorded on the card and still reaches the caller."""
    card_id = await _approved(rig)
    identity = await _credential(rig, card_id)
    rig.failure.append(ToolError("The allowance is already gone."))

    with pytest.raises(ToolError, match="already gone"):
        await _call(rig, identity, "delete_allowance", _ARGS)

    card = await _card(rig)
    assert card.status == "failed", f"failed card: {card.status}"
    assert card.outcome is not None and "already gone" in card.outcome, f"outcome: {card.outcome}"
    assert await _revoked(rig, identity), "a failed call spends its credential too"


async def test_confirmed_credential_marks_the_card_failed_when_the_tool_raises(rig: Rig) -> None:
    """An unexpected error is recorded too, and the call is never retried."""
    card_id = await _approved(rig)
    identity = await _credential(rig, card_id)
    rig.failure.append(RuntimeError("boom"))

    with pytest.raises(ToolError, match="boom"):
        await _call(rig, identity, "delete_allowance", _ARGS)

    card = await _card(rig)
    assert card.status == "failed", f"failed card: {card.status}"
    assert card.outcome is not None and "boom" in card.outcome, f"outcome: {card.outcome}"
    with pytest.raises(ToolError, match=MISMATCH_TEXT):
        await _call(rig, identity, "delete_allowance", _ARGS)
    assert len(rig.ran) == 1, "a failed call is not run again"


# --- a wake turn runs its approved calls end to end --------------------------------


def _runner(rig: Rig, *, call_tool: host_calls.CallTool, thread_id: str = _THREAD):
    return ConfirmedCallRunner(
        sessionmaker=rig.sessions,
        secret=_SECRET.encode(),
        tenant_id=rig.tenant_id,
        platform="discord",
        thread_id=thread_id,
        executing_agent_id=rig.agent_id,
        call_tool=call_tool,
        clock=lambda: dt.datetime.now(dt.UTC),
    )


def _through_server(rig: Rig, jtis: list[uuid.UUID]) -> host_calls.CallTool:
    """The runner's transport, with the token's claims read as the identity edge would."""

    async def call(token: str, name: str, args: Mapping[str, object]) -> object:
        claims = pyjwt.decode(token, _SECRET, algorithms=["HS256"], options={"verify_exp": False})
        jti = uuid.UUID(claims["jti"])
        jtis.append(jti)
        identity = rig.identity(
            confirmed_call_id=uuid.UUID(claims["confirmed_call"]),
            chat_agent_id=uuid.UUID(claims["chat_agent_id"]),
            chat_session_id=claims["chat_session_id"],
            token_jti=jti,
            no_confirmation_surface=False,
        )
        async with Client(rig.server(identity)) as client:
            return await client.call_tool(name, dict(args))

    return call


@contextmanager
def _in_turn(origin_id: uuid.UUID | None) -> Iterator[None]:
    token = current_execution_origin_id.set(origin_id)
    try:
        yield
    finally:
        current_execution_origin_id.reset(token)


async def _origin_id(rig: Rig) -> uuid.UUID:
    async with rig.sessions() as session:
        row = await get_session_origin(
            session,
            tenant_id=rig.tenant_id,
            account_id=rig.account_id,
            platform="discord",
            chat_session_id=rig.grant,
            now=dt.datetime.now(dt.UTC),
        )
    assert row is not None, "the turn's origin exists"
    return row.id


async def _token_revoked(rig: Rig, jti: uuid.UUID) -> bool:
    async with rig.sessions() as session:
        row = await get_mcp_token(session, jti=jti)
    return row is not None and row.revoked_at is not None


async def test_run_confirmed_calls_runs_the_approved_call_once_and_applies_it(rig: Rig) -> None:
    """Approved record, minted credential, one execution, revoked credential, applied record."""
    card_id = await _approved(rig)
    jtis: list[uuid.UUID] = []
    prefix = confirmed_call_prefix(_runner(rig, call_tool=_through_server(rig, jtis)))

    with _in_turn(await _origin_id(rig)):
        first = await prefix()
        again = await prefix()

    card = await _card(rig)
    assert rig.ran == [("delete_allowance", _ARGS, card_id)], "the call executed exactly once"
    assert card.status == "applied", f"record applied: {card.status}"
    assert "delete_allowance done" in first, f"the result reaches the turn: {first}"
    assert again == first, "a recovered send resends the same text and runs nothing"
    assert len(jtis) == 1 and await _token_revoked(rig, jtis[0]), "the credential is revoked"


async def test_run_confirmed_calls_marks_the_call_failed_when_the_tool_fails(rig: Rig) -> None:
    """The tool's refusal ends on the card, and the turn is told it failed."""
    await _approved(rig)
    rig.failure.append(ToolError("The allowance is already gone."))
    jtis: list[uuid.UUID] = []

    with _in_turn(await _origin_id(rig)):
        text = await run_confirmed_calls(_runner(rig, call_tool=_through_server(rig, jtis)))

    card = await _card(rig)
    assert card.status == "failed", f"record failed: {card.status}"
    assert "already gone" in text, f"the turn hears why: {text}"
    assert await _token_revoked(rig, jtis[0]), "the credential is revoked after a failure"


async def test_run_confirmed_calls_says_not_run_when_the_call_never_reaches_the_server(
    rig: Rig,
) -> None:
    """A transport failure never executed anything: failed, didn't run, credential spent."""
    await _approved(rig)
    jtis: list[uuid.UUID] = []

    async def broken(token: str, name: str, args: Mapping[str, object]) -> object:
        jtis.append(
            uuid.UUID(
                pyjwt.decode(token, _SECRET, algorithms=["HS256"], options={"verify_exp": False})[
                    "jti"
                ]
            )
        )
        raise ConnectionError("no route")

    with _in_turn(await _origin_id(rig)):
        await run_confirmed_calls(_runner(rig, call_tool=broken))

    card = await _card(rig)
    assert card.status == "failed", f"record failed: {card.status}"
    assert card.outcome == host_calls.NOT_RUN_TEXT, f"says it didn't run: {card.outcome}"
    assert rig.ran == [], "nothing executed"
    assert await _token_revoked(rig, jtis[0]), "the unused credential is revoked"


async def test_a_confirmed_call_carries_its_turns_seal_and_slack_context(rig: Rig) -> None:
    """The one-shot credential is as sealed and Slack-bound as the wake turn running it."""
    await _approved(rig)
    seen: list[dict[str, Any]] = []
    slack = uuid.uuid4()

    async def capture(token: str, name: str, args: Mapping[str, object]) -> object:
        seen.append(
            pyjwt.decode(token, _SECRET, algorithms=["HS256"], options={"verify_exp": False})
        )
        raise ConnectionError("captured only")

    flags = ExecutionFlags(slack_turn_context_id=slack, seal_ids=frozenset({"seal-1"}))
    with execution_origin(await _origin_id(rig), flags):
        await run_confirmed_calls(_runner(rig, call_tool=capture))

    [claims] = seen
    assert claims.get(SEAL_CLAIM) == ["seal-1"], f"the turn's seal travels: {claims}"
    assert claims.get("slack_turn_context_id") == str(slack), f"and its Slack context: {claims}"


async def test_run_confirmed_calls_fails_closed_outside_a_live_turn(rig: Rig) -> None:
    """With no turn origin there is no person or credential: nothing runs or is spent."""
    await _approved(rig)
    jtis: list[uuid.UUID] = []

    with _in_turn(None):
        text = await run_confirmed_calls(_runner(rig, call_tool=_through_server(rig, jtis)))

    card = await _card(rig)
    assert card.status == "approved", f"left for its own wake or the sweep: {card.status}"
    assert text == "" and rig.ran == [] and jtis == [], "no credential was minted, nothing ran"
