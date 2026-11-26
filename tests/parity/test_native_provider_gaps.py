"""Executable record of what GPT and Gemini turns deliberately do not take from Claude.

A routine that sends a direct message, or whose agent runs code on its own, runs only
on Anthropic Managed Agents, and a built-in tool switched off in a channel or thread
refuses a channel's own deployment there. Each is refused with a message, never run
quietly without the limit, and the provider guide says so.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from daimon.adapters.scheduler.main import routines_unsupported_error
from daimon.core.features.registry import BUILTIN_TOOLS, FEATURES
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.unattended import require_native_unattended_capabilities

DOCS = Path(__file__).resolve().parents[2] / "docs" / "providers.md"


def test_the_provider_guide_lists_where_routines_run() -> None:
    row = next(line for line in DOCS.read_text().splitlines() if line.startswith("| Routines |"))
    cells = [cell.strip() for cell in row.strip("|").split("|")]
    assert cells[1:] == [
        "Yes",
        "Not supported, its hosted shell cannot be turned off",
        "Yes, for agents without automatic code execution",
        "Not supported",
    ]


def test_an_openai_routine_is_refused_for_its_hosted_shell() -> None:
    with pytest.raises(AdmissionDenied) as refused:
        require_native_unattended_capabilities(None, profile_id="openai.persistent_workspace")
    assert refused.value.reason == "backend_unsupported"


@pytest.mark.parametrize("provider", ["openai", "gemini"])
def test_a_dm_routine_on_a_native_provider_is_refused_in_words(provider: str) -> None:
    message = routines_unsupported_error(provider)  # pyright: ignore[reportArgumentType]
    assert "direct message run only on Anthropic" in message, "the refusal says why"


@pytest.mark.parametrize("provider", ["openai", "gemini"])
@pytest.mark.parametrize("tool", BUILTIN_TOOLS)
def test_a_native_provider_reads_channel_and_thread_tool_switches(provider: str, tool: str) -> None:
    tiers = FEATURES[f"tool.{tool}"].tiers_on(provider)
    assert "channel" in tiers and "thread" in tiers, "so the reply there leaves the tool out"
