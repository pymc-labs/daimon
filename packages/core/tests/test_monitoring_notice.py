"""One rule, every path: each monitored path tells the person, with the same words, when
`daimon.core.monitoring.must_tell` says so.

Turn paths come from `test_monitoring_paths`: each `run_prepared_turn` caller that keeps a
question hands it a lifecycle that shows the notice. Places a person sees before any
question are listed in SURFACES.
"""

from __future__ import annotations

import ast
import datetime as dt
import functools
import uuid
from types import SimpleNamespace
from typing import Any, cast

import pytest
from daimon.core.direct_messages import _CollectReply  # pyright: ignore[reportPrivateUsage]
from daimon.core.features.snapshot import CACHE
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.monitoring import (
    CHAT_NOTICE,
    MCP_NOTICE,
    MonitoringNotice,
    MonitoringState,
    capture_question,
    dismiss_notice,
    monitored_agents,
    monitored_notice,
    monitors_any,
    must_tell,
    record_notice,
)
from daimon.core.stores.domain import FeatureTarget
from daimon.core.stores.feature_values import set_feature_value
from daimon.core.turn.lifecycle import tell_monitored
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from . import test_monitoring_paths as paths
from .test_monitoring_paths import NO_QUESTION, ROOT, RUN

_calls = paths._calls  # pyright: ignore[reportPrivateUsage]
_keep = paths._keep  # pyright: ignore[reportPrivateUsage]
_turn_call_sites = paths._turn_call_sites  # pyright: ignore[reportPrivateUsage]

DISCORD = "packages/adapters/discord/daimon/adapters/discord"
SLACK = "packages/adapters/slack/daimon/adapters/slack"
TEAMS = "packages/adapters/teams/daimon/adapters/teams"
EVALS = "packages/core/daimon/core/evals"
WEB = "packages/adapters/web/daimon/adapters/web"
MCP = "packages/adapters/mcp/daimon/adapters/mcp/tools"
CORE_DM = "packages/core/daimon/core/direct_messages.py"

#: Each turn caller that keeps a question, and the lifecycle (file, class) that shows it.
NOTICE_LIFECYCLES: dict[tuple[str, str], tuple[str, str]] = {
    (f"{DISCORD}/bot.py", "_orchestrate_observed_guarded"): (
        f"{DISCORD}/lifecycle.py",
        "DiscordTurnLifecycle",
    ),
    (f"{SLACK}/app.py", "_run_thread_turn_observed_guarded"): (
        f"{SLACK}/lifecycle.py",
        "SlackTurnLifecycle",
    ),
    (f"{TEAMS}/app.py", "_bind_and_run"): (f"{TEAMS}/lifecycle.py", "TeamsTurnLifecycle"),
    (f"{WEB}/turns.py", "_bind_and_run"): (f"{WEB}/lifecycle.py", "WebTurnLifecycle"),
    (CORE_DM, "_run_admitted_dm_turn"): (CORE_DM, "_CollectReply"),
    (f"{EVALS}/runner/turns.py", "_turn"): (f"{EVALS}/chat/surface.py", "EvalSurfaceLifecycle"),
}

#: How each lifecycle shows the notice: the neutral card `test_plain_words_parity` checks.
SHOWN_BY = {
    "DiscordTurnLifecycle": "build_notice_embed",
    "SlackTurnLifecycle": "post_notice",
    "TeamsTurnLifecycle": "notice_card",
    "WebTurnLifecycle": "notice_json",
    "_CollectReply": "_tell",
    "EvalSurfaceLifecycle": "add",
}

#: Turn callers that pass the question yet never keep it, so tell nothing, and why.
NEVER_KEPT: dict[tuple[str, str], str] = {
    ("packages/core/daimon/core/helpers.py", "_run"): "a helper's ask comes from an agent",
}

#: Places shown before any question: (file, function) -> the call that reads monitoring.
SURFACES: dict[tuple[str, str], str] = {
    (f"{WEB}/chat.py", "detail"): "monitored_notice",
    (f"{MCP}/agent_chat.py", "_describe_agent_impl"): "monitored_notice",
    (f"{MCP}/hub.py", "_list_daimons_impl"): "monitored_agents",
}


def _tree(rel: str) -> ast.Module:
    return ast.parse((ROOT / rel).read_text())


def _function(tree: ast.AST, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    [fn] = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name == name
    ]
    return fn


def test_every_kept_question_path_has_a_lifecycle_that_tells() -> None:
    """A new chat path that keeps a question must say how the person is told."""
    sites = set(_turn_call_sites()) - set(NO_QUESTION)
    assert sites == set(NOTICE_LIFECYCLES) | set(NEVER_KEPT), (
        "list the new path in NOTICE_LIFECYCLES or NEVER_KEPT: "
        f"{sorted(sites ^ (set(NOTICE_LIFECYCLES) | set(NEVER_KEPT)))}"
    )
    for (site_file, _), (rel, cls) in NOTICE_LIFECYCLES.items():
        assert cls in (ROOT / site_file).read_text(), f"{site_file} does not use {cls}"
        [node] = [n for n in ast.walk(_tree(rel)) if isinstance(n, ast.ClassDef) and n.name == cls]
        hooks = [
            n
            for n in node.body
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "on_monitoring_notice"
        ]
        assert hooks, f"{cls} never shows the monitoring notice"
        assert _calls(hooks[0], SHOWN_BY[cls]), f"{cls} shows no neutral notice card"


def test_never_kept_callers_do_not_pass_a_chat_origin() -> None:
    sites = _turn_call_sites()
    for site in NEVER_KEPT:
        for call in sites[site]:
            [origin] = [kw.value for kw in call.keywords if kw.arg == "origin"]
            assert isinstance(origin, ast.Constant) and origin.value != "chat", site


def test_run_prepared_turn_tells_right_after_keeping() -> None:
    fn = _function(_tree(RUN.relative_to(ROOT).as_posix()), "run_prepared_turn")
    [keep] = _calls(fn, "_keep_question")
    [tell] = _calls(fn, "tell_monitored")
    assert keep.lineno < tell.lineno


def test_direct_message_replies_without_a_card_still_tell() -> None:
    """`_CollectReply` shows nothing itself: each caller passes a card or `tell`."""
    found = 0
    for path in ROOT.glob("packages/**/*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if "/tests/" in rel or ".venv" in rel or "reply_to_dm" not in path.read_text():
            continue
        for name in ("reply_to_dm", "reply_to_dm_result"):
            for call in _calls(_tree(rel), name):
                found += 1
                passed = {kw.arg for kw in call.keywords}
                assert passed & {"card_for", "tell"}, f"{rel}:{call.lineno} shows no notice"
    assert found >= 3


def test_every_surface_shown_before_a_question_reads_monitoring() -> None:
    for (rel, name), call in SURFACES.items():
        assert _calls(_function(_tree(rel), name), call), f"{rel} {name} never calls {call}"
    chat = _tree(f"{MCP}/agent_chat.py")
    keepers = [
        fn
        for fn in ast.walk(chat)
        if isinstance(fn, ast.AsyncFunctionDef)
        and fn.name != "_keep_question"
        and _calls(fn, "_keep_question")
    ]
    assert len(keepers) >= 2, [fn.name for fn in keepers]
    for fn in keepers:
        handles = _calls(fn, "_turn_handle")
        assert handles and all(len(c.args) + len(c.keywords) == 4 for c in handles), fn.name
    ask = _function(chat, "_ask_impl")
    assert any(kw.arg == "monitoring" for c in _calls(ask, "AskResult") for kw in c.keywords)
    result = _function(chat, "_ask_tool_result")
    assert any(isinstance(n, ast.Attribute) and n.attr == "monitoring" for n in ast.walk(result))


T0 = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)


@pytest.mark.parametrize(
    ("on", "since", "told_at", "tell"),
    [
        (False, None, None, False),
        (True, None, None, True),
        (True, T0, None, True),
        (True, T0, T0 - dt.timedelta(seconds=1), True),
        (True, T0, T0 + dt.timedelta(seconds=1), False),
        (True, None, T0, False),
    ],
)
def test_must_tell(on: bool, since: dt.datetime | None, told_at: dt.datetime | None, tell: bool):
    state = MonitoringState(cast(Any, SimpleNamespace(on=on)), since)
    assert must_tell(state, told_at) is tell


def test_the_notice_is_two_lines_with_the_same_help() -> None:
    for notice in (CHAT_NOTICE, MCP_NOTICE):
        assert notice.text == f"{notice.line}\n\n{notice.help}"
    assert CHAT_NOTICE.help == MCP_NOTICE.help == "Ask an admin if you have questions."


async def test_tell_monitored_is_best_effort() -> None:
    shown: list[MonitoringNotice] = []

    class Shows:
        async def on_monitoring_notice(self, notice: MonitoringNotice) -> None:
            shown.append(notice)

    class Fails:
        async def on_monitoring_notice(self, notice: MonitoringNotice) -> None:
            raise RuntimeError("platform down")

    assert await tell_monitored(cast(Any, Shows()), CHAT_NOTICE)
    assert not await tell_monitored(cast(Any, Fails()), CHAT_NOTICE)
    assert not await tell_monitored(cast(Any, object()), CHAT_NOTICE)
    assert await tell_monitored(_CollectReply(functools.partial(_append, shown)), MCP_NOTICE)
    assert not await tell_monitored(_CollectReply(), MCP_NOTICE)
    assert shown == [CHAT_NOTICE, MCP_NOTICE]


async def _append(shown: list[MonitoringNotice], notice: MonitoringNotice) -> None:
    shown.append(notice)


async def _switch(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, key: str, value: str
) -> None:
    async with sm() as session, session.begin():
        await set_feature_value(
            session,
            tenant_id=tenant_id,
            target=FeatureTarget("agent", agent_id="agt_a"),
            key=key,
            value=value,
            set_by=None,
            surface="web",
        )
    CACHE.clear()  # As `change_feature` does.


async def test_each_person_is_told_once_per_agent_per_switch_on(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The first kept question tells until the notice is shown; after that no chat, thread
    or platform tells again. Switching off and on again tells once more."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    sm, tid, aid = db_session_factory, tenant.id, account.id
    assert await _keep(sm, tid, aid, platform="slack", thread_id="before") is None
    await _switch(sm, tid, "monitor_direct_messages", "on")

    unshown = await _keep(sm, tid, aid, platform="discord", thread_id="one")
    first = await _keep(sm, tid, aid, platform="discord", thread_id="one")
    assert unshown is not None and unshown.notice == CHAT_NOTICE
    assert first is not None and first.notice == CHAT_NOTICE, "an unshown notice tells again"
    assert await record_notice(sm, first.subject, CHAT_NOTICE, turn_ref=first.turn_ref)
    for platform, is_dm, thread in (
        ("discord", True, "one"),
        ("discord", True, "two"),
        ("slack", True, "before"),
        ("teams", True, "t"),
        ("web", None, "w"),
    ):
        kept = await _keep(sm, tid, aid, platform=platform, is_dm=is_dm, thread_id=thread)
        assert kept is not None and kept.notice is None, (platform, thread)

    await _switch(sm, tid, "monitor_direct_messages", "off")
    await _switch(sm, tid, "monitor_direct_messages", "on")
    told = await _keep(sm, tid, aid, platform="discord", thread_id="two")
    assert told is not None and told.notice == CHAT_NOTICE, "a new switch-on must tell again"


async def test_a_dismissed_web_notice_stays_gone_until_monitoring_restarts(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    sm, tid, agent = db_session_factory, tenant.id, _agent_uuid(tenant.id)

    async def shown() -> MonitoringNotice | None:
        return await monitored_notice(
            sm, None, tenant_id=tid, agent_id=agent, platform="web", account_id=account.id
        )

    assert not await dismiss_notice(
        sm, None, tenant_id=tid, agent_id=agent, account_id=account.id, conversation="w"
    ), "nothing to dismiss while the agent is not monitored"
    await _switch(sm, tid, "monitor_direct_messages", "on")
    assert await shown() == CHAT_NOTICE
    assert await dismiss_notice(
        sm, None, tenant_id=tid, agent_id=agent, account_id=account.id, conversation="w"
    )
    assert await shown() is None
    await _switch(sm, tid, "monitor_direct_messages", "off")
    await _switch(sm, tid, "monitor_direct_messages", "on")
    assert await shown() == CHAT_NOTICE


async def test_mcp_questions_carry_the_mcp_notice(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    sm = db_session_factory
    agent = uuid.uuid4()
    async with sm() as session:
        assert not await monitors_any(session, None, tenant_id=tenant.id, platform="slack")
    await _switch(sm, tenant.id, "monitor_mcp", "on")

    async def ask(turn_ref: str, conversation: str = "session-1") -> MonitoringNotice | None:
        kept = await capture_question(
            sm,
            None,
            tenant_id=tenant.id,
            agent_id=_agent_uuid(tenant.id),
            platform="mcp",
            account_id=account.id,
            conversation=conversation,
            turn_ref=turn_ref,
            content="hello",
        )
        assert kept is not None
        if kept.notice is not None:
            assert await record_notice(sm, kept.subject, kept.notice, turn_ref=turn_ref)
        return kept.notice

    assert await ask("t1") == MCP_NOTICE
    assert await ask("t2") is None
    assert await ask("t3", "session-2") is None, "over MCP the person is told once per agent"
    async with sm() as session:
        assert await monitors_any(session, None, tenant_id=tenant.id, platform="slack")
        monitored = await monitored_agents(
            session,
            None,
            tenant_id=tenant.id,
            agent_ids=(_agent_uuid(tenant.id), agent),
            platform="mcp",
        )
    assert monitored == {_agent_uuid(tenant.id)}


def _agent_uuid(tenant_id: uuid.UUID) -> uuid.UUID:
    return derive_agent_uuid(tenant_id=tenant_id, ma_agent_id="agt_a")
