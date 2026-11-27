"""With `watch_approval` skip, the session gate lets only make and resume go uncarded.

`compute_approval` skip does the same for launch and extend, and nothing else."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools import _session_gate
from daimon.core.session_snapshot import hash_tools, session_tools
from daimon.core.tool_safety import (
    COMPUTE_APPROVAL_TOOLS,
    OPEN_TOOL_SAFETY,
    WATCH_APPROVAL_TOOLS,
    WATCH_CONFIRMED_TOOLS,
    ToolSafetyPolicy,
)

from .test_session_gate_rules import (  # pyright: ignore[reportPrivateUsage]
    AGENT,
    AUTH,
    _runtime,  # pyright: ignore[reportPrivateUsage]
    patch_session_gate_collaborators,
)

__all__ = ["patch_session_gate_collaborators"]  # the autouse fixture the gate tests share
ORIGIN = SimpleNamespace(
    attended=True, responder_ma_agent_id="agent", platform="discord", session_thread_id="t1"
)


def _skip(monkeypatch: pytest.MonkeyPatch, *, skipped: bool) -> AsyncMock:
    seen = AsyncMock(return_value=skipped)
    monkeypatch.setattr(_session_gate, "watch_card_skipped", seen)
    # With no live session the gate reports it, so a pass can only come from the skip.
    monkeypatch.setattr(_session_gate, "get_live_thread_session", AsyncMock(return_value=None))
    return seen


def _nodb_runtime() -> McpRuntime:
    runtime = cast(Any, _runtime())
    runtime.session_factory = _NoDb
    return cast(McpRuntime, runtime)


class _NoDb:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *_a: object) -> None:
        return None


@pytest.mark.parametrize("tool", sorted(WATCH_APPROVAL_TOOLS))
async def test_skip_lets_the_watch_tools_run_without_a_card(
    monkeypatch: pytest.MonkeyPatch, tool: str
) -> None:
    seen = _skip(monkeypatch, skipped=True)
    runtime = _nodb_runtime()
    gap = await _session_gate.session_card_gap(runtime, AUTH, cast(Any, ORIGIN), tool_name=tool)
    assert gap is None, f"{tool} needs no card when the agent is set to skip"
    seen.assert_awaited_once()


@pytest.mark.parametrize("tool", sorted(WATCH_CONFIRMED_TOOLS - WATCH_APPROVAL_TOOLS))
async def test_skip_never_relaxes_key_rotation_long_jobs_or_machine_launches(
    monkeypatch: pytest.MonkeyPatch, tool: str
) -> None:
    seen = _skip(monkeypatch, skipped=True)
    runtime = _nodb_runtime()
    gap = await _session_gate.session_card_gap(runtime, AUTH, cast(Any, ORIGIN), tool_name=tool)
    assert gap == "no_live_session", f"{tool} still needs a live, gated session"
    seen.assert_not_awaited()


@pytest.mark.parametrize("tool", sorted(WATCH_APPROVAL_TOOLS))
async def test_ask_is_unchanged(monkeypatch: pytest.MonkeyPatch, tool: str) -> None:
    _skip(monkeypatch, skipped=False)
    runtime = _nodb_runtime()
    gap = await _session_gate.session_card_gap(runtime, AUTH, cast(Any, ORIGIN), tool_name=tool)
    assert gap == "no_live_session", "with ask, a watch tool still needs the card's session"


async def test_an_unattended_origin_is_refused_even_when_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _skip(monkeypatch, skipped=True)
    origin = SimpleNamespace(**{**ORIGIN.__dict__, "attended": False})
    gap = await _session_gate.session_card_gap(
        _runtime(), AUTH, cast(Any, origin), tool_name="create_watch"
    )
    assert gap == "no_origin", "skip never lets a turn nobody watches enroll a watch"


@pytest.mark.parametrize(
    "policy", [OPEN_TOOL_SAFETY, ToolSafetyPolicy(enabled=True)], ids=["safety_off", "safety_on"]
)
@pytest.mark.parametrize("tool", ["rotate_watch_key", "start_long_job"])
async def test_a_session_bound_under_skip_still_proves_the_other_cards(
    monkeypatch: pytest.MonkeyPatch, tool: str, policy: ToolSafetyPolicy
) -> None:
    runtime = _runtime()
    runtime.settings.tool_safety = policy
    monkeypatch.setattr(_session_gate, "daimon_rule_tools_for", AsyncMock(return_value=frozenset()))
    recorded = session_tools(
        AGENT,
        frozenset(),
        tool_safety=policy,
        public_url="https://mcp.example/mcp",
        confirmed_tools=WATCH_CONFIRMED_TOOLS - WATCH_APPROVAL_TOOLS,
    )
    live = cast(
        Any, SimpleNamespace(effective_config=SimpleNamespace(tools_sha256=hash_tools(recorded)))
    )
    proven = await _session_gate._recorded_as_gated(  # pyright: ignore[reportPrivateUsage]
        runtime, AUTH, live, tool_name=tool, ma_agent_id=AGENT.id,
        reported_tools_sha256=hash_tools(AGENT.tools),
    )  # fmt: skip
    assert proven, f"{tool} still asks in a session bound while watch cards were skipped"
    skipped_tool = await _session_gate._recorded_as_gated(  # pyright: ignore[reportPrivateUsage]
        runtime, AUTH, live, tool_name="create_watch", ma_agent_id=AGENT.id,
        reported_tools_sha256=hash_tools(AGENT.tools),
    )  # fmt: skip
    assert not skipped_tool, "the recorded template never proves a card it does not hold"


def _compute_skip(monkeypatch: pytest.MonkeyPatch, *, watch: bool = False) -> AsyncMock:
    """`compute_approval` is skip; `watch_approval` is skip only with ``watch``."""
    _skip(monkeypatch, skipped=watch)
    seen = AsyncMock(return_value=True)
    monkeypatch.setattr(_session_gate, "compute_card_skipped", seen)
    return seen


@pytest.mark.parametrize("tool", sorted(COMPUTE_APPROVAL_TOOLS))
async def test_compute_skip_lets_launch_and_extend_run_without_a_card(
    monkeypatch: pytest.MonkeyPatch, tool: str
) -> None:
    seen = _compute_skip(monkeypatch)
    gap = await _session_gate.session_card_gap(
        _nodb_runtime(), AUTH, cast(Any, ORIGIN), tool_name=tool
    )
    assert gap is None, f"{tool} needs no card when the agent is set to skip"
    seen.assert_awaited_once()


@pytest.mark.parametrize("tool", sorted(WATCH_CONFIRMED_TOOLS - COMPUTE_APPROVAL_TOOLS))
async def test_compute_skip_never_relaxes_watches_key_rotation_or_long_jobs(
    monkeypatch: pytest.MonkeyPatch, tool: str
) -> None:
    seen = _compute_skip(monkeypatch)
    gap = await _session_gate.session_card_gap(
        _nodb_runtime(), AUTH, cast(Any, ORIGIN), tool_name=tool
    )
    assert gap == "no_live_session", f"{tool} still needs a live, gated session"
    seen.assert_not_awaited()


async def test_an_unattended_origin_is_refused_even_when_compute_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _compute_skip(monkeypatch)
    origin = SimpleNamespace(**{**ORIGIN.__dict__, "attended": False})
    for tool in sorted(COMPUTE_APPROVAL_TOOLS):
        gap = await _session_gate.session_card_gap(
            _runtime(), AUTH, cast(Any, origin), tool_name=tool
        )
        assert gap == "no_origin", f"skip never lets a turn nobody watches call {tool}"


@pytest.mark.parametrize(
    ("bound", "proves", "never"),
    [
        (
            WATCH_CONFIRMED_TOOLS - COMPUTE_APPROVAL_TOOLS,
            ["create_watch", "rotate_watch_key", "start_long_job", "update_watch"],
            sorted(COMPUTE_APPROVAL_TOOLS),
        ),
        (
            WATCH_CONFIRMED_TOOLS - WATCH_APPROVAL_TOOLS - COMPUTE_APPROVAL_TOOLS,
            ["rotate_watch_key", "start_long_job", "update_watch"],
            sorted(COMPUTE_APPROVAL_TOOLS | WATCH_APPROVAL_TOOLS),
        ),
        # With tool safety on, a session bound under skip before the policy read the
        # skip held every card; it still proves them all.
        (WATCH_CONFIRMED_TOOLS, sorted(WATCH_CONFIRMED_TOOLS), []),
    ],
    ids=["compute_skip", "both_skips", "every_card"],
)
@pytest.mark.parametrize(
    "policy", [OPEN_TOOL_SAFETY, ToolSafetyPolicy(enabled=True)], ids=["safety_off", "safety_on"]
)
async def test_a_session_bound_under_compute_skip_still_proves_the_other_cards(
    monkeypatch: pytest.MonkeyPatch,
    bound: frozenset[str],
    proves: list[str],
    never: list[str],
    policy: ToolSafetyPolicy,
) -> None:
    runtime = _runtime()
    runtime.settings.tool_safety = policy
    monkeypatch.setattr(_session_gate, "daimon_rule_tools_for", AsyncMock(return_value=frozenset()))
    recorded = session_tools(
        AGENT,
        frozenset(),
        tool_safety=policy,
        public_url="https://mcp.example/mcp",
        confirmed_tools=bound,
    )
    live = cast(
        Any, SimpleNamespace(effective_config=SimpleNamespace(tools_sha256=hash_tools(recorded)))
    )

    async def proven(tool: str) -> bool:
        return await _session_gate._recorded_as_gated(  # pyright: ignore[reportPrivateUsage]
            runtime, AUTH, live, tool_name=tool, ma_agent_id=AGENT.id,
            reported_tools_sha256=hash_tools(AGENT.tools),
        )  # fmt: skip

    for tool in proves:
        assert await proven(tool), f"{tool} still asks in a session bound under the skip"
    for tool in never:
        assert not await proven(tool), f"the recorded template never proves {tool}"
