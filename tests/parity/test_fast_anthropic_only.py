"""Executable record of a deliberate difference: Fast mode is Anthropic only.

Reasoning effort works on every provider that declares levels, but the faster
priced variant of a model exists only for Anthropic. No other profile declares a
`speed` control, so the catalog never marks their models fast, an agent edit
that asks for Fast off Anthropic is refused, and the web editor shows the switch
disabled with its reason.
"""

from __future__ import annotations

from pathlib import Path

from daimon.core import agent_changes
from daimon.core.agent_catalog import models_of
from daimon.core.agent_model_names import AgentModel
from daimon.core.agent_view import AgentConfigDoc, ModelChoice
from mux.contracts.ids import ModelRef
from mux.profiles import PROFILES

REPO_ROOT = Path(__file__).resolve().parents[2]


def _doc(**model: str) -> AgentConfigDoc:
    return AgentConfigDoc(
        name="helper",
        purpose="",
        avatar="",
        model=ModelChoice(
            id="gpt-6-astra", backend="openai", speed="standard", effort="high"
        ).model_copy(update=model),
        system="",
        skills=[],
        tools=[],
        mcp_servers=[],
        places=[],
    )


def test_no_provider_profile_declares_a_speed_control() -> None:
    for name, profile in PROFILES.items():
        assert "speed" not in profile.model_controls, f"{name} must not offer Fast"


def test_the_catalog_marks_only_anthropic_models_fast() -> None:
    entries = [
        AgentModel(ModelRef(provider="openai", id="gpt-6-astra"), "", 10.0, 50.0),
        AgentModel(ModelRef(provider="gemini", id="gemini-4-argon"), "", 2.0, 10.0),
    ]
    listed = models_of(
        entries,
        offered=[e.model for e in entries],
        priced=("gpt-6-astra", "gpt-6-astra-fast", "gemini-4-argon-fast"),
    )
    assert [m.fast for m in listed] == [False, False]


def test_an_edit_asking_for_fast_off_anthropic_is_refused() -> None:
    refusal = agent_changes.unsupported([], a=_doc(), b=_doc(speed="fast"))
    assert refusal is not None
    assert "Fast" in refusal


def test_the_web_editor_names_the_reason_fast_is_off() -> None:
    editor = REPO_ROOT / "web/src/modules/agents/inspector/ModelEditor.tsx"
    assert "Fast mode is Claude only" in editor.read_text()
