"""Execute frozen base functions and current adapters against identical recordings."""

from __future__ import annotations

import dataclasses
import sys
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Literal
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import aiohttp
import discord
import pytest
from daimon.adapters.discord import bot as discord_bot
from daimon.adapters.discord import embed, turn_card_recovery
from daimon.adapters.slack import blockkit, boot_sweep
from daimon.adapters.slack.card_recovery import CardLookup, CardLookupStatus
from daimon.adapters.teams import app as teams_app
from daimon.core.stores.domain import ThreadSessionRow, TurnCardIntentRow
from daimon.core.turn.state import ToolUseBlock, TurnState
from daimon.core.turn.status_lines import format_draft, format_tool_lines, has_running_tool
from sqlalchemy.exc import SQLAlchemyError

_NOW = datetime(2026, 10, 3, tzinfo=UTC)
_ID = UUID(int=1)


def _base(name, module):
    """Load the recorded base statements with the adapter's dependencies."""
    frozen = ModuleType(f"turn_bookkeeping_base_{name}")
    frozen.__dict__.update(vars(module))
    frozen.__name__ = f"turn_bookkeeping_base_{name}"
    frozen.__dict__.update(
        dataclasses=dataclasses,
        dataclass=dataclasses.dataclass,
        Enum=Enum,
        Literal=Literal,
        format_draft=format_draft,
        format_tool_lines=format_tool_lines,
        has_running_tool=has_running_tool,
    )
    sys.modules[frozen.__name__] = frozen
    source = Path(__file__).with_name("turn_bookkeeping_base") / f"{name}.txt"
    exec(compile(source.read_text(), str(source), "exec"), frozen.__dict__)
    if hasattr(frozen, "TurnPhase"):
        frozen._TERMINAL_PHASES = frozenset({frozen.TurnPhase.DONE, frozen.TurnPhase.ERROR})
    if hasattr(frozen, "_PHASE_COLOR"):
        frozen._PHASE_COLOR = {frozen.TurnPhase(k.value): v for k, v in module._PHASE_COLOR.items()}
    return frozen


def _render(module, state):
    if hasattr(module, "to_embed_data"):
        return _state(module.to_embed_data(state, now=123.0))
    return module.to_blocks(state, now=123.0, cancel_key="turn"), module.to_fallback_text(
        state, now=123.0
    )


def _state(state):
    values = dataclasses.asdict(state)
    values["phase"] = values["phase"].value
    return values


@pytest.mark.parametrize("adapter,state_name", [(embed, "EmbedState"), (blockkit, "State")])
@pytest.mark.parametrize("phase", ["thinking", "tool_running", "done", "error"])
@pytest.mark.parametrize("label", ["", "a\nnew draft", "x" * 400])
def test_card_transitions_match_base(adapter, state_name, phase, label):
    old = _base("discord_card" if adapter is embed else "slack_card", adapter)
    fields = dict(
        text_preview="previous",
        agent_name="research",
        started_at=1.0,
        cost_str="$0.02",
        balance_str="$4.00 left",
        notice="Try again.",
    )
    before = getattr(old, state_name)(phase=old.TurnPhase(phase), **fields)
    after = getattr(adapter, state_name)(phase=adapter.TurnPhase(phase), **fields)
    for kind in ("message", "done", "error", "message"):
        before = old.update(before, old.EmbedEvent(kind=kind, label=label))
        after = adapter.update(after, adapter.EmbedEvent(kind=kind, label=label))
        assert _state(before) == _state(after)
        assert _render(old, before) == _render(adapter, after)
    for status in ("pending", "complete", "failed"):
        turn = TurnState(
            content=[
                ToolUseBlock(
                    kind="tool_use",
                    type="agent.tool_use",
                    id="tu_1",
                    name="bash",
                    input={},
                    status=status,
                )
            ]
        )
        # Exercise both the terminal latch and live activity transitions.
        for terminal in (True, False):
            left = before if terminal else getattr(old, state_name)()
            right = after if terminal else getattr(adapter, state_name)()
            left, right = old.update_activity(left, turn), adapter.update_activity(right, turn)
            assert _state(left) == _state(right)
            assert _render(old, left) == _render(adapter, right)


class _Sessions:
    def __init__(self, trace, failure=None):
        self.trace = trace
        self.failure = failure

    def __call__(self):
        return self

    def begin(self):
        self.trace.append("begin")
        return self

    async def __aenter__(self):
        self.trace.append("open")
        return self

    async def __aexit__(self, *args):
        self.trace.append("close")

    async def commit(self):
        self.trace.append("commit")
        if self.failure == "commit":
            raise SQLAlchemyError("recorded failure")


def _row(**updates):
    return ThreadSessionRow(
        id=_ID,
        tenant_id=_ID,
        platform="discord",
        thread_id="555",
        account_id=None,
        ma_session_id="sesn_old",
        watermark_message_id=None,
        status="live",
        created_at=_NOW,
        updated_at=_NOW,
        active_turn_message_id="777",
        active_turn_channel_id="555",
        active_turn_started_at=_NOW,
    ).model_copy(update=updates)


def _intent(known):
    return TurnCardIntentRow(
        id=_ID,
        tenant_id=_ID,
        platform="discord",
        thread_id="555",
        turn_token=_ID,
        channel_id="555",
        message_id="777" if known else None,
        status="posted" if known else "prepared",
        created_at=_NOW,
        updated_at=_NOW,
    )


def _log(trace):
    return SimpleNamespace(
        **{
            level: lambda event, **fields: trace.append((event, fields))
            for level in ("info", "warning", "exception")
        }
    )


@pytest.mark.parametrize("platform", ["discord", "slack"])
@pytest.mark.parametrize(
    "case",
    [
        "cleared",
        "moved",
        "edit_failed",
        "commit",
        "empty",
        "idle",
        "no_channel",
        "no_tenant",
        "no_started",
        "repeat",
    ],
)
async def test_boot_rows_match_base(monkeypatch, platform, case):
    module = discord_bot if platform == "discord" else boot_sweep
    traces = []
    for legacy in (True, False):
        trace = []
        sessions = _Sessions(trace, case)
        updates = {"platform": platform}
        if case == "idle":
            updates["active_turn_message_id"] = None
        if case == "no_channel":
            updates["active_turn_channel_id"] = None
        if case == "no_started":
            updates["active_turn_started_at"] = None
        row = _row(**updates)

        async def clear(session, *, id, expected_message_id, trace=trace):
            trace.append(("clear", id, expected_message_id))
            return case != "moved"

        async def interrupt(client, *, session_id, trace=trace):
            trace.append(("interrupt", session_id))
            return True

        async def edit(*, trace=trace, **kwargs):
            payload = kwargs["embed"].to_dict() if "embed" in kwargs else kwargs
            trace.append(("edit", payload))
            if case == "edit_failed":
                raise (
                    ValueError("recorded failure")
                    if platform == "discord"
                    else aiohttp.ClientError("recorded failure")
                )

        class FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return _NOW

        with monkeypatch.context() as patch:
            patch.setattr(module, "log", _log(trace))
            patch.setattr(module, "clear_active_turn_if_message_id", clear)
            patch.setattr(module, "interrupt_orphaned_session", interrupt)
            patch.setattr(
                module,
                "list_orphaned_turns",
                AsyncMock(return_value=[] if case == "empty" else [row]),
            )
            runtime = SimpleNamespace(sessionmaker=sessions, anthropic=object())
            if platform == "discord":
                patch.setattr(module, "datetime", FixedDatetime)
                patch.setattr(
                    module, "list_recoverable_turn_card_intents", AsyncMock(return_value=[])
                )
                message = SimpleNamespace(edit=edit)
                channel = MagicMock(spec=discord.TextChannel)
                channel.fetch_message = AsyncMock(return_value=message)
                owner = SimpleNamespace(
                    runtime=runtime,
                    _orphans_retired=case == "repeat",
                    _boot_turn_card_intents=None,
                    get_channel=lambda _, channel=channel: channel,
                    _start_turn_card_recovery=lambda trace=trace: trace.append("start"),
                )
                fn = (
                    _base("discord_boot", module)._retire_orphaned_turns_once
                    if legacy
                    else module.DaimonBot._retire_orphaned_turns_once
                )
                args, kwargs = (owner,), {}
            else:
                patch.setattr(
                    module,
                    "list_tenants_by_platform",
                    AsyncMock(
                        return_value=[]
                        if case == "no_tenant"
                        else [SimpleNamespace(id=_ID, external_id="T1")]
                    ),
                )
                patch.setattr(
                    module,
                    "resolve_web_client",
                    AsyncMock(return_value=SimpleNamespace(chat_update=edit)),
                )
                fn = (
                    _base("slack_boot", module).retire_orphaned_turns
                    if legacy
                    else module.retire_orphaned_turns
                )
                args, kwargs = (runtime,), {"now": _NOW}
            try:
                await fn(*args, **kwargs)
            except SQLAlchemyError:
                trace.append("db_error")
            traces.append(trace)
    assert traces[0] == traces[1]


@pytest.mark.parametrize("platform", ["discord", "slack"])
@pytest.mark.parametrize("known", [False, True])
@pytest.mark.parametrize(
    "case", ["found", "multiple", "miss", "incomplete", "record_moved", "edit_failed", "commit"]
)
async def test_card_intent_recovery_matches_base(monkeypatch, platform, known, case):
    module = turn_card_recovery if platform == "discord" else boot_sweep
    traces = []
    for legacy in (True, False):
        trace = []
        sessions = _Sessions(trace, case)
        clock = [0.0]
        intent = _intent(known)
        status = (
            "not_found" if case == "miss" else "indeterminate" if case == "incomplete" else case
        )
        status = status if status in ("not_found", "indeterminate", "multiple") else "found"

        async def sleep(delay, trace=trace, clock=clock):
            trace.append(("sleep", delay))
            clock[0] += delay

        async def record(*args, trace=trace, **kwargs):
            trace.append(("record", kwargs.get("message_id")))
            return case != "record_moved"

        async def retire(*args, trace=trace, **kwargs):
            trace.append(("retire", kwargs["expected_message_id"]))

        async def edit(*args, trace=trace, **kwargs):
            trace.append(("edit", {k: v for k, v in kwargs.items() if k != "sleep"}))
            if platform == "slack" and case == "edit_failed":
                raise aiohttp.ClientError("recorded failure")
            return case != "edit_failed"

        async def lookup(*args, trace=trace, clock=clock, status=status, **kwargs):
            trace.append(("lookup", clock[0]))
            ids = ("778", "779") if status == "multiple" else ("778",)
            if platform == "discord":
                return module.TurnCardSearchResult(
                    module.TurnCardSearchState(status),
                    tuple(int(x) for x in ids) if status in ("found", "multiple") else (),
                )
            return CardLookup(
                CardLookupStatus(status),
                message_timestamps=ids,
                retry_after_seconds=90.0 if status == "indeterminate" else None,
            )

        with monkeypatch.context() as patch:
            patch.setattr(module, "log", _log(trace))
            common = dict(sleep=sleep, now=lambda: _NOW, monotonic=lambda clock=clock: clock[0])
            if platform == "discord":
                patch.setattr(module, "find_turn_card_message", lookup)
                patch.setattr(module, "record_turn_card_message", record)
                patch.setattr(module, "_reconcile_matching_messages", edit)
                patch.setattr(module, "retire_terminal_turn_card", retire)
                fn = (
                    _base("discord_intent", module).reconcile_turn_card_intent
                    if legacy
                    else module.reconcile_turn_card_intent
                )
                await fn(sessions, intent=intent, thread=object(), **common)
            else:
                client = SimpleNamespace(
                    chat_update=edit, retry_handlers=[], session=SimpleNamespace(close=AsyncMock())
                )
                patch.setattr(
                    module,
                    "list_tenants_by_platform",
                    AsyncMock(return_value=[SimpleNamespace(id=_ID, external_id="T1")]),
                )
                patch.setattr(module, "resolve_web_client", AsyncMock(return_value=client))
                patch.setattr(module, "find_turn_card_by_key", lookup)
                patch.setattr(module, "_record_card_intent_message", record)
                patch.setattr(module, "_retire_card_intent", retire)
                fn = (
                    _base("slack_boot", module).recover_slack_card_intents
                    if legacy
                    else module.recover_slack_card_intents
                )
                await fn(SimpleNamespace(sessionmaker=sessions), [intent], **common)
                trace.append(("client_closed", client.session.close.await_count))
            traces.append(trace)
    assert traces[0] == traces[1]


@pytest.mark.parametrize("closed", [False, True])
@pytest.mark.parametrize("intent_id", [None, _ID])
@pytest.mark.parametrize("failure", [None, "retire", "clear"])
async def test_teams_settlement_matches_base(monkeypatch, closed, intent_id, failure):
    traces = []
    for legacy in (True, False):
        trace = []

        async def retire(session, *, id, expected_message_id, trace=trace):
            trace.append(("retire", id, expected_message_id))
            if failure == "retire":
                raise SQLAlchemyError("recorded failure")

        async def clear(session, *, id, trace=trace):
            trace.append(("clear", id))
            if failure == "clear":
                raise SQLAlchemyError("recorded failure")

        with monkeypatch.context() as patch:
            patch.setattr(teams_app, "retire_turn_card_intent", retire)
            patch.setattr(teams_app, "clear_active_turn", clear)
            patch.setattr(teams_app, "log", _log(trace))
            fn = _base("teams_settle", teams_app)._settle if legacy else teams_app.TeamsApp._settle
            await fn(
                SimpleNamespace(runtime=SimpleNamespace(sessionmaker=_Sessions(trace))),
                intent_id,
                "777",
                closed,
                {_ID, UUID(int=2)},
            )
        traces.append(trace)
    assert traces[0] == traces[1]
