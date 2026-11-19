"""Executable record of what GPT and Gemini turns deliberately do not take from Claude.

A routine runs only on Anthropic Managed Agents, and a built-in tool's switch stops at
the agent on a provider that compiles one revision per agent. Both are refused with a
message, never run quietly without the limit, and the provider guide says so.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from daimon.adapters.scheduler.main import routines_unsupported_error
from daimon.core.features.registry import FEATURES

DOCS = Path(__file__).resolve().parents[2] / "docs" / "providers.md"


def test_the_provider_guide_lists_routines_as_anthropic_only() -> None:
    row = next(line for line in DOCS.read_text().splitlines() if line.startswith("| Routines |"))
    cells = [cell.strip() for cell in row.strip("|").split("|")]
    assert cells[1:] == ["Yes", "Not supported", "Not supported", "Not supported"], (
        "routines run only on Anthropic"
    )


@pytest.mark.parametrize("provider", ["openai", "gemini"])
def test_a_routine_on_a_native_provider_is_refused_in_words(provider: str) -> None:
    message = routines_unsupported_error(provider)  # pyright: ignore[reportArgumentType]
    assert "Routines run only on Anthropic" in message, "the refusal says why"


@pytest.mark.parametrize("provider", ["openai", "gemini"])
def test_a_native_provider_reads_no_channel_or_thread_tool_switch(provider: str) -> None:
    tiers = FEATURES["tool.bash"].tiers_on(provider)
    assert "channel" not in tiers and "thread" not in tiers, (
        "so admission refuses a turn where a channel or thread switched a tool off"
    )
