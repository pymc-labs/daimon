"""Tests for `daimon.core.models_catalog.list_model_choices`."""

from __future__ import annotations

import pytest
from daimon.core.constants import ALLOWED_MODEL_IDS, MODEL_DISPLAY_NAMES
from daimon.core.models_catalog import list_model_choices
from daimon.core.pricing import AGENT_MODEL_PRICING


def test_every_priced_agent_model_has_a_display_name() -> None:
    for model_id in AGENT_MODEL_PRICING:
        assert model_id in MODEL_DISPLAY_NAMES, (
            f"{model_id} is priced as an agent model but has no display name"
        )


def test_list_model_choices_marks_exactly_one_default() -> None:
    default = ALLOWED_MODEL_IDS[-1]
    choices = list_model_choices(default=default)
    defaults = [choice for choice in choices if choice.is_default]
    assert len(defaults) == 1, "exactly one choice must be flagged default"
    assert defaults[0].id == default, "the flagged choice must be the requested default"
    assert defaults[0].description == "the default", "the default choice explains itself"
    for choice in choices:
        if choice.id != default:
            assert choice.description is None, "non-default choices carry no description"


def test_list_model_choices_rejects_unknown_default() -> None:
    with pytest.raises(ValueError, match="not one of"):
        list_model_choices(default="not-a-real-model")


def test_list_model_choices_preserves_allowed_model_ids_order() -> None:
    choices = list_model_choices(default=ALLOWED_MODEL_IDS[0])
    assert tuple(choice.id for choice in choices) == ALLOWED_MODEL_IDS, (
        "choice order must match ALLOWED_MODEL_IDS"
    )


def test_list_model_choices_labels_match_display_names() -> None:
    choices = list_model_choices(default=ALLOWED_MODEL_IDS[0])
    for choice in choices:
        assert choice.label == MODEL_DISPLAY_NAMES.get(choice.id, choice.id), (
            "label must come from MODEL_DISPLAY_NAMES"
        )
