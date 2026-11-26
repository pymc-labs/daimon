"""Executable record of what GPT and Gemini turns deliberately do not take from Claude.

An OpenAI routine needs the sandbox opt-in (its hosted shell cannot be left out of a
session, while Gemini's built-ins can), and a built-in tool switched off in a channel
or thread refuses a channel's own deployment there. Each is refused with a message,
never run quietly without the limit, and the provider guide says so.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from daimon.core import provider_routine
from daimon.core.features.registry import BUILTIN_TOOLS, FEATURES
from daimon.core.turn.errors import AdmissionDenied

DOCS = Path(__file__).resolve().parents[2] / "docs" / "providers.md"


def test_the_provider_guide_lists_where_routines_run() -> None:
    row = next(line for line in DOCS.read_text().splitlines() if line.startswith("| Routines |"))
    cells = [cell.strip() for cell in row.strip("|").split("|")]
    assert cells[1:] == ["Yes", "With sandbox tools turned on", "Yes", "Not supported"]


async def test_an_openai_routine_without_the_sandbox_opt_in_is_refused_in_words(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OpenAI's hosted shell is part of its session's environment, not a tool a fire
    can leave out, so without the opt-in the fire stops and names who can help."""
    revision = SimpleNamespace(profile="openai.persistent_workspace", backend="openai")

    async def admitted(*_: object, **__: object) -> object:
        return SimpleNamespace(backend_revision=revision)

    monkeypatch.setattr(provider_routine, "admit", admitted)
    monkeypatch.setattr(provider_routine, "at_fire_place", lambda admission, **_: admission)
    row: Any = SimpleNamespace(tenant_id=None, agent_name="analyst", channel_id="1", id=None)
    row.destination_kind = row.destination_id = row.destination_parent_id = None
    subject: Any = SimpleNamespace(is_admin=True, role_ids=(), verified_membership=True)
    deps: Any = SimpleNamespace()
    with pytest.raises(AdmissionDenied) as refused:
        await provider_routine.admit_provider_routine(
            deps, row, platform="discord", owner="1", subject=subject, sandbox=None
        )
    assert refused.value.reason == "backend_unsupported"
    assert "Ask a server admin to turn on sandbox tools" in str(refused.value)


@pytest.mark.parametrize("provider", ["openai", "gemini"])
@pytest.mark.parametrize("tool", BUILTIN_TOOLS)
def test_a_native_provider_reads_channel_and_thread_tool_switches(provider: str, tool: str) -> None:
    tiers = FEATURES[f"tool.{tool}"].tiers_on(provider)
    assert "channel" in tiers and "thread" in tiers, "so the reply there leaves the tool out"
