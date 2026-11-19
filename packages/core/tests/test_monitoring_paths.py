"""One rule, every path: each chat and MCP turn that carries a person's question reaches
`daimon.core.monitoring`, including conversations begun before the switch.

Call sites are found from the AST; a path that deliberately keeps nothing is listed with
its reason. The MCP middleware and agent-chat paths are checked in the MCP package.
"""

from __future__ import annotations

import ast
import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from daimon.core import usage_sweep
from daimon.core.defaults.metadata import MA_METADATA_KEY_TENANT
from daimon.core.features.snapshot import CACHE
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.monitoring import (
    KeptQuestion,
    capture_answer,
    capture_question,
    continued_question,
)
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
    ): "queued work: its seed is no question; the answer goes with the one it carries on",
    (
        "packages/adapters/slack/daimon/adapters/slack/app.py",
        "_run_continuation_turn_observed",
    ): "queued work: its seed is no question; the answer goes with the one it carries on",
    (
        "packages/adapters/discord/daimon/adapters/discord/wizard_submit.py",
        "run_wizard_submit_turn_observed",
    ): "answers to the agent's own form; request_text would also turn on MCP context",
    (
        "packages/testing/daimon/testing/channel_accounting.py",
        "run_turn",
    ): "a test harness, not a production path",
    (
        "packages/testing/daimon/testing/turn_fakes.py",
        "run_prepared",
    ): "a test helper that forwards its caller's keywords, not a production path",
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


def _names(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _continuation_callers() -> list[tuple[str, str, ast.Call]]:
    """`run_prepared_turn` calls in functions that take a queued continuation row."""
    found: list[tuple[str, str, ast.Call]] = []
    for path in sorted([*ROOT.glob("packages/**/*.py"), *ROOT.glob("apps/**/*.py")]):
        rel = path.relative_to(ROOT).as_posix()
        if "/tests/" in rel or ".venv" in rel or "run_prepared_turn" not in path.read_text():
            continue
        for fn in ast.walk(ast.parse(path.read_text())):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            rows = [
                a
                for a in (*fn.args.args, *fn.args.kwonlyargs)
                if a.annotation is not None and "TaskContinuationRow" in ast.unparse(a.annotation)
            ]
            if rows:
                found += [(rel, fn.name, call) for call in _calls(fn, "run_prepared_turn")]
    return found


def test_queued_work_carries_its_row_and_never_asks_its_seed() -> None:
    """A continuation's seed is the queued work, not the person's words: a caller with a
    row passes it as `continues=`, and any `request_text` depends on that row."""
    callers = _continuation_callers()
    assert len(callers) >= 4, f"the scan found too few continuation callers: {callers}"
    wrong: list[tuple[str, str]] = []
    for rel, name, call in callers:
        kws = {kw.arg: kw.value for kw in call.keywords}
        carried = kws.get("continues")
        asked = kws.get("request_text")
        if carried is None or (asked is not None and not _names(carried) & _names(asked)):
            wrong.append((rel, name))
    assert not wrong, f"pass continues=<row> and no question without it: {wrong}"


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
    request_ref: str | None = None,
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
        request_ref=request_ref,
    )


async def _switch_on(
    session: AsyncSession, tenant_id: uuid.UUID, key: str, value: str = "on"
) -> None:
    await set_feature_value(
        session,
        tenant_id=tenant_id,
        target=FeatureTarget("agent", agent_id="agt_a"),
        key=key,
        value=value,
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


async def test_a_message_run_twice_keeps_one_question_under_its_platform_id(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A re-run of the same message keeps its first question and gets its answer once."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await _switch_on(db_session, tenant.id, "monitor_direct_messages")
    sm, ref = db_session_factory, "1700000000.000100"
    for _ in range(2):
        kept = await _keep(sm, tenant.id, account.id, platform="slack", request_ref=ref)
        assert kept is not None and kept.turn_ref == ref, kept
    assert await capture_answer(
        sm, tenant_id=tenant.id, conversation="thread-1", turn_ref=ref, content="hi", outcome="ok"
    )
    assert await _keep(sm, tenant.id, account.id, platform="slack", request_ref=ref) is None
    async with sm() as session:
        rows = await store.list_events(
            session,
            tenant_id=tenant.id,
            agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="agt_a"),
        )
    assert sorted((row.kind, row.turn_ref) for row in rows) == [("answer", ref), ("question", ref)]


async def test_queued_work_keeps_its_answer_with_the_question_it_carries_on(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Work queued while a kept question waits keeps its answer with that question; work
    queued after the answer, or for someone else, keeps nothing."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    other = await make_account(db_session, tenant=tenant)
    await _switch_on(db_session, tenant.id, "monitor_direct_messages")
    sm = db_session_factory
    assert await _keep(sm, tenant.id, account.id, platform="teams", request_ref="m1")

    async def carried(account_id: uuid.UUID) -> str | None:
        return await continued_question(
            sm,
            None,
            tenant_id=tenant.id,
            agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="agt_a"),
            platform="teams",
            conversation="thread-1",
            account_id=account_id,
            queued_at=datetime.now(UTC),
        )

    assert await carried(account.id) == "m1"
    assert await carried(other.id) is None, "carried someone else's question"
    assert await capture_answer(
        sm,
        tenant_id=tenant.id,
        conversation="thread-1",
        turn_ref="m1",
        content="on it",
        outcome="ok",
    )
    assert await carried(account.id) is None, "carried a question answered before it queued"
    work = "w1"  # The continuation row's id.
    assert await capture_answer(
        sm,
        tenant_id=tenant.id,
        conversation="thread-1",
        turn_ref=work,
        content="done",
        outcome="ok",
        question_ref="m1",
    )
    async with sm() as session:
        rows = await store.list_events(
            session,
            tenant_id=tenant.id,
            agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="agt_a"),
        )
    kept = sorted((row.kind, row.turn_ref, row.content) for row in rows)
    assert kept == [
        ("answer", "m1", "on it"),
        ("answer", work, "done"),
        ("question", "m1", "hello"),
    ]


async def test_a_continuation_is_kept_only_while_its_agent_is_monitored(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Work queued while a kept question waits loses its answer when monitoring is switched
    off before the work ends, and is not linked at all once it is off."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await _switch_on(db_session, tenant.id, "monitor_direct_messages")
    sm = db_session_factory
    assert await _keep(sm, tenant.id, account.id, platform="teams", request_ref="m1")
    agent_id = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="agt_a")

    async def carried() -> str | None:
        return await continued_question(
            sm,
            None,
            tenant_id=tenant.id,
            agent_id=agent_id,
            platform="teams",
            conversation="thread-1",
            account_id=account.id,
            queued_at=datetime.now(UTC),
        )

    assert await carried() == "m1", "the question was not carried while monitored"
    await _switch_on(db_session, tenant.id, "monitor_direct_messages", "off")
    assert await carried() is None, "linked a continuation of an unmonitored agent"
    assert not await capture_answer(
        sm,
        None,
        tenant_id=tenant.id,
        conversation="thread-1",
        turn_ref="w1",
        content="done",
        outcome="ok",
        question_ref="m1",
    ), "kept a continuation's answer after monitoring was switched off"
    async with sm() as session:
        rows = await store.list_events(session, tenant_id=tenant.id, agent_id=agent_id)
    assert [row.kind for row in rows] == ["question"], rows


async def test_a_continuation_is_not_linked_to_a_question_kept_before_monitoring_resumed(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """An old kept question still waiting for its answer is not the one a later turn's
    continuation carries on after monitoring was switched off and on again; a question kept
    since is."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await _switch_on(db_session, tenant.id, "monitor_direct_messages")
    sm = db_session_factory
    agent_id = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="agt_a")
    subject = store.MonitoredSubject(tenant.id, agent_id, "teams", account.id, "thread-1")
    old = datetime.now(UTC) - timedelta(days=1)
    async with sm() as session, session.begin():
        await store.record_events(
            session,
            subject,
            (store.MonitoredEntry("question", "old", turn_ref="m0", created_at=old),),
        )
    await _switch_on(db_session, tenant.id, "monitor_direct_messages", "off")
    await _switch_on(db_session, tenant.id, "monitor_direct_messages")

    async def carried() -> str | None:
        return await continued_question(
            sm,
            None,
            tenant_id=tenant.id,
            agent_id=agent_id,
            platform="teams",
            conversation="thread-1",
            account_id=account.id,
            queued_at=datetime.now(UTC),
        )

    assert await carried() is None, "linked to a question kept before monitoring resumed"
    assert await _keep(sm, tenant.id, account.id, platform="teams", request_ref="m1")
    assert await carried() == "m1", "did not link to the question kept since"


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
async def test_the_kept_replies_follow_the_settle_inside_its_budget(
    monkeypatch: pytest.MonkeyPatch, settle_s: float
) -> None:
    """Keeping replies runs after the billing settle and before the request is cleared,
    and never past `budget_s`: when the settle used it up, the request stays unchanged."""
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

    async def keep(*_args: object, **_kwargs: object) -> bool:
        order.append("keep")
        return True

    state = session_settle_state
    monkeypatch.setattr(state, "settle_request", settle_request)
    monkeypatch.setattr(state, "clear_settle_request", clear)
    monkeypatch.setattr(usage_sweep, "settle_stamped_session", settle)
    monkeypatch.setattr(usage_sweep, "_keep_monitored_replies", keep)
    session = cast(Any, SimpleNamespace(id="sesn_1", status="terminated", metadata={}))

    await usage_sweep.settle_finished_turn(
        cast(Any, None), cast(Any, _Db), session, markup=Decimal(1), budget_s=0.2
    )

    assert order == (["settle", "keep", "clear"] if settle_s == 0 else []), order


async def test_a_reply_that_could_not_be_read_is_kept_by_a_later_settle(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A settle whose reply read fails or runs out of time leaves the request, so the next
    sweep keeps the reply; once it is kept the request is cleared."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await _switch_on(db_session, tenant.id, "monitor_mcp")
    assert await capture_question(
        db_session_factory,
        None,
        tenant_id=tenant.id,
        agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="agt_a"),
        platform="mcp",
        account_id=account.id,
        conversation="sesn_1",
        turn_ref="evt_u1",
        content="hello",
    )
    async with db_session_factory() as s, s.begin():
        _, requested_at = await session_settle_state.request_settle(s, managed_session_id="sesn_1")
        await session_settle_state.name_settle_turn(
            s, managed_session_id="sesn_1", requested_at=requested_at, turn_event_id="evt_u1"
        )

    async def settle(*_args: object, **_kwargs: object) -> object:
        return SimpleNamespace(complete=True)

    reads: list[str] = []

    async def list_events(*_args: object, **_kwargs: object) -> _Page:
        reads.append("read")
        if len(reads) == 1:
            raise TimeoutError
        return _Page(
            [
                _event("session.status_idle"),
                _event("agent.message", text="the reply"),
                _event("user.message", "evt_u1"),
            ]
        )

    monkeypatch.setattr(usage_sweep, "settle_stamped_session", settle)
    monkeypatch.setattr(usage_sweep, "_list_events", list_events)
    session = cast(
        Any,
        SimpleNamespace(
            id="sesn_1",
            status="terminated",
            metadata={MA_METADATA_KEY_TENANT: str(tenant.id)},
        ),
    )

    async def pending() -> object:
        async with db_session_factory() as s:
            return await session_settle_state.settle_request(s, managed_session_id="sesn_1")

    async def kept() -> list[str | None]:
        async with db_session_factory() as s:
            rows = await store.list_events(
                s,
                tenant_id=tenant.id,
                agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="agt_a"),
            )
        return [row.content for row in rows if row.kind == "answer"]

    async def sweep() -> None:
        await usage_sweep.settle_finished_turn(
            cast(Any, None), db_session_factory, session, markup=Decimal(1), budget_s=None
        )

    await sweep()
    assert await pending() is not None, "the request was cleared with the reply unkept"
    assert await kept() == [], "kept a reply that could not be read"
    await sweep()
    assert await kept() == ["the reply"], "the later sweep did not keep the reply"
    assert await pending() is None, "the request outlived its kept reply"
