"""The panels' shared model select and switch preview words."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from daimon.core import model_switch
from daimon.core.backend_refusals import refusal_card
from daimon.core.channel_backend import AgentBackendRefused
from daimon.core.model_switch import (
    SELECT_LIMIT,
    ModelPicker,
    ModelSwitchPreview,
    SwitchRefusal,
    refusal_of,
    set_effort,
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


def _effort_service(
    monkeypatch: pytest.MonkeyPatch, *, backend: str | None, editor: bool = True
) -> Any:
    preview = SimpleNamespace(allowed=True, setting_revision=4, digest="d", declared_drops=("x",))
    revision = SimpleNamespace(
        backend=backend, profile=None, requires=None, thread_mode=None, effort=None
    )
    service = SimpleNamespace(
        may_edit=AsyncMock(return_value=editor),
        current=AsyncMock(return_value=revision),
        preview_settings=AsyncMock(return_value=preview),
        set_settings=AsyncMock(),
    )
    load = AsyncMock(return_value=(SimpleNamespace(), SimpleNamespace()))
    monkeypatch.setattr(model_switch, "_load", load)
    return service


async def _set(service: Any, effort: str) -> object:
    return await set_effort(
        service,
        cast("Any", None),
        actor=cast("Any", None),
        agent_name="Ada",
        effort=effort,
        admin=False,
    )


async def test_an_effort_pick_keeps_the_model_and_accepts_the_shown_losses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _effort_service(monkeypatch, backend="openai")
    assert await _set(service, "high") == "high"
    _agent, settings, revision, _actor = service.set_settings.await_args.args
    assert (settings.effort, revision) == ("high", 4)
    assert service.set_settings.await_args.kwargs == {
        "expected_preview_digest": "d",
        "accepted_drops": ("x",),
    }


async def test_an_effort_pick_is_refused_on_claude_for_a_non_editor_and_for_unknown_levels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claude = _effort_service(monkeypatch, backend=None)
    refused = await _set(claude, "high")
    assert isinstance(refused, SwitchRefusal)
    claude.set_settings.assert_not_awaited()
    viewer = _effort_service(monkeypatch, backend="openai", editor=False)
    assert isinstance(await _set(viewer, "high"), SwitchRefusal)
    viewer.set_settings.assert_not_awaited()
    assert isinstance(
        await _set(_effort_service(monkeypatch, backend="openai"), "max"), SwitchRefusal
    )
