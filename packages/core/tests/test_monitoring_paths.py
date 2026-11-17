"""One rule, every path: each chat and MCP turn that carries a person's question reaches
`daimon.core.monitoring`, including conversations begun before the switch.

Call sites are found from the AST; a path that deliberately keeps nothing is listed with
its reason. The MCP middleware and agent-chat paths are checked in the MCP package.
"""

from __future__ import annotations

import ast
import asyncio
import uuid
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from daimon.core import usage_sweep
from daimon.core.defaults.metadata import MA_METADATA_KEY_TENANT
from daimon.core.features.snapshot import CACHE
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.monitoring import KeptQuestion, capture_question
from daimon.core.stores import monitoring as store
from daimon.core.stores import session_settle_state
from daimon.core.stores.domain import FeatureTarget
from daimon.core.stores.feature_values import set_feature_value
from daimon.core.turn import run
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

ROOT = Path(__file__).resolve().parents[3]
RUN = ROOT / "packages/core/daimon/core/turn/run.py"

#: `run_prepared_turn` callers that pass no `request_text`, so keep nothing, and why.
NO_QUESTION: dict[tuple[str, str], str] = {
    (
        "packages/adapters/discord/daimon/adapters/discord/bot.py",
        "_run_continuation_turn_observed",
    ): "a watch or job wake resumes the agent; nobody asked anything",
    (
        "packages/adapters/slack/daimon/adapters/slack/app.py",
        "_run_continuation_turn_observed",
    ): "a watch or job wake resumes the agent; nobody asked anything",
    (
        "packages/adapters/discord/daimon/adapters/discord/wizard_submit.py",
        "run_wizard_submit_turn_observed",
    ): "answers to the agent's own form; request_text would also turn on MCP context",
    (
        "packages/testing/daimon/testing/channel_accounting.py",
        "run_turn",
    ): "a test harness, not a production path",
}


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    return func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)


def _calls(tree: ast.AST, name: str) -> list[ast.Call]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call) and _call_name(n) == name]


def _turn_call_sites() -> dict[tuple[str, str], list[ast.Call]]:
    """Every non-test `run_prepared_turn(...)` call, by file and the enclosing module- or
    class-level function."""
    sites: dict[tuple[str, str], list[ast.Call]] = {}
    for path in sorted([*ROOT.glob("packages/**/*.py"), *ROOT.glob("apps/**/*.py")]):
        rel = path.relative_to(ROOT).as_posix()
        if "/tests/" in rel or ".venv" in rel or "run_prepared_turn" not in path.read_text():
            continue
        tree = ast.parse(path.read_text())
        for parent in ast.walk(tree):
            if not isinstance(parent, ast.Module | ast.ClassDef):
                continue
            for owner in parent.body:
                if isinstance(owner, ast.FunctionDef | ast.AsyncFunctionDef):
                    calls = _calls(owner, "run_prepared_turn")
                    if calls:
                        sites.setdefault((rel, owner.name), []).extend(calls)
    return sites


def test_every_turn_caller_passes_the_question_or_says_why_not() -> None:
    """A new chat path that forgets `request_text` would never be monitored."""
    sites = _turn_call_sites()
    assert len(sites) >= 8, f"the scan found too few call sites: {sorted(sites)}"
    missing = sorted(
        site
        for site, calls in sites.items()
        if site not in NO_QUESTION
        and not all(any(kw.arg == "request_text" for kw in call.keywords) for call in calls)
    )
    assert not missing, f"pass request_text= or list the caller in NO_QUESTION: {missing}"
    stale = sorted(
        site
        for site in NO_QUESTION
        if site not in sites
        or any(kw.arg == "request_text" for call in sites[site] for kw in call.keywords)
    )
    assert not stale, f"NO_QUESTION entries that no longer apply: {stale}"


def test_run_prepared_turn_keeps_the_question_and_both_endings() -> None:
    """The turn keeps its question, then its answer on success and on failure."""
    tree = ast.parse(RUN.read_text())
    [fn] = [
        n
        for n in tree.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "run_prepared_turn"
    ]
    assert _calls(fn, "_keep_question"), "run_prepared_turn must call _keep_question"
    handlers = [h for h in ast.walk(fn) if isinstance(h, ast.ExceptHandler)]
    in_handler = {id(c) for h in handlers for c in _calls(h, "capture_answer")}
    answers = _calls(fn, "capture_answer")
    assert in_handler, "a failed turn must keep its answer from the except path"
    assert any(id(c) not in in_handler for c in answers), "a finished turn must keep its answer"


def _all_on(_key: str) -> bool:
    return True


def _prepared(*, is_dm: bool | None, account_id: uuid.UUID, agent: str = "agt_a") -> Any:
    grant = None if is_dm is None else SimpleNamespace(is_dm=is_dm)
    admission = SimpleNamespace(
        grant=grant,
        agent=SimpleNamespace(id=agent),
        account_id=account_id,
        feature_on=_all_on,
    )
    return SimpleNamespace(admission=admission)


async def _keep(
    sessions: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    *,
    platform: str,
    is_dm: bool | None = True,
    origin: str = "chat",
    request_text: str | None = "hello",
    thread_id: str = "thread-1",
) -> KeptQuestion | None:
    deps = cast(Any, SimpleNamespace(sessionmaker=sessions, settings=None))
    return await run._keep_question(  # pyright: ignore[reportPrivateUsage]
        deps,
        _prepared(is_dm=is_dm, account_id=account_id),
        tenant_id=tenant_id,
        platform=platform,
        thread_id=thread_id,
        origin=cast(Any, origin),
        request_text=request_text,
    )


async def _switch_on(session: AsyncSession, tenant_id: uuid.UUID, key: str) -> None:
    await set_feature_value(
        session,
        tenant_id=tenant_id,
        target=FeatureTarget("agent", agent_id="agt_a"),
        key=key,
        value="on",
        set_by=None,
        surface="web",
    )
    await session.commit()
    CACHE.clear()  # As `change_feature` does.


async def test_every_direct_message_platform_and_web_chat_keeps_the_question(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Discord, Slack and Teams DMs and web chat keep the question; a channel turn, a
    routine, and a turn without the person's words keep nothing. A thread begun before
    the switch is kept from the switch on."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    sm = db_session_factory
    assert await _keep(sm, tenant.id, account.id, platform="discord") is None, "off kept"
    await _switch_on(db_session, tenant.id, "monitor_direct_messages")

    for platform, is_dm in (("discord", True), ("slack", True), ("teams", True), ("web", None)):
        kept = await _keep(sm, tenant.id, account.id, platform=platform, is_dm=is_dm)
        assert kept is not None, f"{platform} direct message was not kept"
    refused = {
        "channel": await _keep(sm, tenant.id, account.id, platform="slack", is_dm=False),
        "routine": await _keep(sm, tenant.id, account.id, platform="discord", origin="routine"),
        "no words": await _keep(sm, tenant.id, account.id, platform="teams", request_text=None),
        "mcp": await _keep(sm, tenant.id, account.id, platform="mcp"),
    }
    assert set(refused.values()) == {None}, f"kept turns that are no direct question: {refused}"
    async with sm() as session:
        rows = await store.list_events(
            session,
            tenant_id=tenant.id,
            agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="agt_a"),
        )
    assert sorted(row.platform for row in rows) == ["discord", "slack", "teams", "web"], rows


def _event(kind: str, event_id: str = "", text: str = "", stop: str = "end_turn") -> Any:
    return SimpleNamespace(
        type=kind,
        id=event_id,
        content=[SimpleNamespace(text=text)],
        stop_reason=SimpleNamespace(type=stop),
    )


class _Page:
    def __init__(self, data: list[Any]) -> None:
        self.data = data

    def has_next_page(self) -> bool:
        return False

    async def get_next_page(self) -> _Page:  # pragma: no cover - one page
        raise AssertionError("one page only")


async def test_the_settle_keeps_mcp_replies_once_their_turn_ends(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An MCP reply is kept by the settle that sees its turn end, as the turn's newest
    agent message; a turn paused on a confirmation waits for a later settle."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await _switch_on(db_session, tenant.id, "monitor_mcp")
    for ref in ("evt_u1", "evt_u2"):
        assert await capture_question(
            db_session_factory,
            None,
            tenant_id=tenant.id,
            agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="agt_a"),
            platform="mcp",
            account_id=account.id,
            conversation="sesn_1",
            turn_ref=ref,
            content=ref,
        )
    pages = [
        [
            _event("session.status_idle", stop="requires_action"),
            _event("agent.message", text="checking"),
            _event("user.message", "evt_u2"),
            _event("session.status_idle"),
            _event("agent.message", text="first answer"),
            _event("agent.message", text="thinking"),
            _event("user.message", "evt_u1"),
        ],
        [
            _event("session.status_idle"),
            _event("agent.message", text="second answer"),
            _event("session.status_idle", stop="requires_action"),
            _event("agent.message", text="checking"),
            _event("user.message", "evt_u2"),
        ],
    ]

    async def list_events(*_args: object, **_kwargs: object) -> _Page:
        return _Page(pages.pop(0))

    monkeypatch.setattr(usage_sweep, "_list_events", list_events)
    session = cast(
        Any, SimpleNamespace(id="sesn_1", metadata={MA_METADATA_KEY_TENANT: str(tenant.id)})
    )
    answers: dict[str, list[str | None]] = {}
    for _settle in range(2):
        await usage_sweep._keep_monitored_replies(  # pyright: ignore[reportPrivateUsage]
            cast(Any, None), db_session_factory, session
        )
        async with db_session_factory() as db:
            rows = await store.list_events(
                db,
                tenant_id=tenant.id,
                agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="agt_a"),
            )
        answers[str(_settle)] = sorted(
            (row.content for row in rows if row.kind == "answer"), key=str
        )

    assert answers == {"0": ["first answer"], "1": ["first answer", "second answer"]}, answers


class _Db:
    async def __aenter__(self) -> _Db:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    def begin(self) -> _Db:
        return self


@pytest.mark.parametrize("settle_s", [0.0, 1.0], ids=["in_budget", "out_of_budget"])
async def test_the_kept_replies_wait_for_the_settle_and_have_their_own_budget(
    monkeypatch: pytest.MonkeyPatch, settle_s: float
) -> None:
    """Keeping replies never eats the billing settle's budget: it runs after, even when the
    settle ran out of time."""
    order: list[str] = []
    request = SimpleNamespace(turn_event_id="evt_u1")

    async def settle_request(*_args: object, **_kwargs: object) -> object:
        return request

    async def clear(*_args: object, **_kwargs: object) -> None:
        order.append("clear")

    async def settle(*_args: object, **_kwargs: object) -> object:
        await asyncio.sleep(settle_s)
        order.append("settle")
        return SimpleNamespace(complete=True)

    async def keep(*_args: object, **_kwargs: object) -> None:
        order.append("keep")

    state = session_settle_state
    monkeypatch.setattr(state, "settle_request", settle_request)
    monkeypatch.setattr(state, "clear_settle_request", clear)
    monkeypatch.setattr(usage_sweep, "settle_stamped_session", settle)
    monkeypatch.setattr(usage_sweep, "_keep_monitored_replies", keep)
    session = cast(Any, SimpleNamespace(id="sesn_1", status="terminated", metadata={}))

    await usage_sweep.settle_finished_turn(
        cast(Any, None), cast(Any, _Db), session, markup=Decimal(1), budget_s=0.2
    )

    assert order == (["settle", "clear", "keep"] if settle_s == 0 else ["keep"]), order
