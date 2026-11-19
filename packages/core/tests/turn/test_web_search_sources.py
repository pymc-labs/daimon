"""The sources rule: one text on every provider path, present only with web search on."""

from __future__ import annotations

import inspect
import re
from types import SimpleNamespace
from typing import Any

import pytest
from daimon.core import headless_runner
from daimon.core.features.model_input import SessionToolset
from daimon.core.turn import run as turn_run
from daimon.core.turn.web_search_sources import GUIDANCE, sources_block
from mux.contracts.resources import AgentSpec, ToolSpec

_MODEL = {"provider": "openai", "id": "model"}


def _spec(*tools: ToolSpec) -> AgentSpec:
    return AgentSpec(name="a", model=_MODEL, tools=tools)  # type: ignore[arg-type]


def _claude(*, search: bool, toolset: SessionToolset | None = None) -> Any:
    tool: dict[str, Any] = {
        "type": "agent_toolset_20260401",
        "default_config": {"enabled": False},
        "configs": [{"name": "web_search", "enabled": search}],
    }

    def dump(*, mode: str) -> dict[str, Any]:
        return tool

    agent = SimpleNamespace(tools=[SimpleNamespace(model_dump=dump)])
    return SimpleNamespace(
        _native_spec=None,
        admission=SimpleNamespace(agent=agent, toolset=toolset or SessionToolset()),
    )


def _native(name: str, permission: str = "auto") -> Any:
    spec = _spec(ToolSpec(name=name, kind="builtin", permission=permission))  # type: ignore[arg-type]
    return SimpleNamespace(_native_spec=spec, admission=None)


def test_rule_is_short_and_asks_for_links() -> None:
    """The one instruction names sources and links, in one sentence."""
    assert "sources" in GUIDANCE and "links" in GUIDANCE, "the rule asks for linked sources"
    assert GUIDANCE.count(".") == 1, "the rule stays one sentence"
    assert GUIDANCE in sources_block(), "the block carries the rule"


@pytest.mark.parametrize(
    ("label", "prepared"),
    [
        ("claude", _claude(search=True)),
        ("openai", _native("web_search")),
        ("gemini", _native("google_search")),
    ],
)
def test_block_is_sent_on_every_provider_when_search_is_on(label: str, prepared: Any) -> None:
    """Claude, OpenAI and Gemini get the same block when their search is on."""
    assert turn_run._web_search_sources(prepared) == sources_block(), f"{label} gets the rule"  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    ("label", "prepared"),
    [
        ("claude tool off", _claude(search=False)),
        (
            "claude place off",
            _claude(search=True, toolset=SessionToolset(builtins={"web_search": False})),
        ),
        ("openai ask first", _native("web_search", "ask")),
        ("gemini ask first", _native("google_search", "ask")),
        ("openai without search", _native("code_execution")),
        ("native without tools", SimpleNamespace(_native_spec=_spec(), admission=None)),
    ],
)
def test_block_is_absent_when_search_is_off(label: str, prepared: Any) -> None:
    """No search, no rule: a turn that cannot search never mentions sources."""
    assert turn_run._web_search_sources(prepared) == "", f"{label} sends no rule"  # pyright: ignore[reportPrivateUsage]


def test_every_send_path_calls_the_one_helper() -> None:
    """The main send, the reseed send and the routine runner all use `sources_prefix`."""
    run_src = inspect.getsource(turn_run)
    assert len(re.findall(r"\+\s*sources\b", run_src)) >= 2, "main and reseed sends add the block"
    assert "sources_prefix(" in inspect.getsource(headless_runner), "the routine runner adds it"
