"""The panels' shared model select and switch preview words."""

from __future__ import annotations

from daimon.core.backend_refusals import refusal_card
from daimon.core.channel_backend import AgentBackendRefused
from daimon.core.model_switch import (
    SELECT_LIMIT,
    ModelPicker,
    ModelSwitchPreview,
    refusal_of,
    switch_fact_lines,
)
from daimon.core.models_catalog import ModelChoice
from mux.contracts.ids import ModelRef


def _choice(index: int) -> ModelChoice:
    return ModelChoice(
        id=f"m{index}",
        label=f"Model {index}",
        description=None,
        is_default=False,
        provider="openai",
    )


def test_the_select_keeps_the_current_model_within_the_limit() -> None:
    choices = tuple(_choice(i) for i in range(40))
    picker = ModelPicker(choices=choices, current=choices[39].model)
    shown = picker.limited()
    assert len(shown) == SELECT_LIMIT, "a Discord select holds 25 options"
    assert choices[39] in shown, "the current model stays in the select"


def test_a_short_list_is_shown_whole() -> None:
    choices = tuple(_choice(i) for i in range(3))
    assert ModelPicker(choices=choices, current=choices[0].model).limited() == choices


def test_a_pick_without_a_preview_shows_only_its_name() -> None:
    switch = ModelSwitchPreview(
        model=ModelRef(provider="anthropic", id="claude-sonnet-4-6"),
        label="Sonnet",
        before="anthropic",
        preview=None,
    )
    assert switch_fact_lines(switch, agent_name="Ada") == ("Model: Sonnet",)
    assert switch.allowed and switch.digest == "" and switch.revision == 0


def test_a_refusal_uses_the_shared_line() -> None:
    error = AgentBackendRefused("stale_preview")
    refusal = refusal_of(error, admin=False)
    assert (refusal.line, refusal.action) == refusal_card(error, admin=False)
    assert refusal.reason == "stale_preview"
