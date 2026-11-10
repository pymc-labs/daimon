"""The model catalog: one source for every surface's model choices."""

from __future__ import annotations

import ast
import dataclasses
import uuid
from datetime import date
from pathlib import Path

import pytest
from daimon.core import pricing
from daimon.core.channel_backend import AgentBackendRefused
from daimon.core.constants import ALLOWED_MODEL_IDS, MODEL_DISPLAY_NAMES
from daimon.core.models_catalog import catalog_model, claude_model_choices, list_model_choices
from daimon.core.pricing import AGENT_MODEL_PRICING
from daimon.core.stores.domain import TenantBackendPolicy
from mux.contracts.ids import ModelRef


def test_every_priced_agent_model_has_a_display_name() -> None:
    for model_id in AGENT_MODEL_PRICING:
        assert model_id in MODEL_DISPLAY_NAMES, (
            f"{model_id} is priced as an agent model but has no display name"
        )


def test_claude_model_choices_marks_exactly_one_default() -> None:
    default = ALLOWED_MODEL_IDS[-1]
    choices = claude_model_choices(default=default)
    defaults = [choice for choice in choices if choice.is_default]
    assert len(defaults) == 1, "exactly one choice must be flagged default"
    assert defaults[0].id == default, "the flagged choice must be the requested default"
    assert defaults[0].description == "the default", "the default choice explains itself"
    for choice in choices:
        if choice.id != default:
            assert choice.description is None, "non-default choices carry no description"


def test_claude_model_choices_rejects_unknown_default() -> None:
    with pytest.raises(ValueError, match="not one of"):
        claude_model_choices(default="not-a-real-model")


def test_claude_model_choices_preserves_allowed_model_ids_order() -> None:
    choices = claude_model_choices(default=ALLOWED_MODEL_IDS[0])
    assert tuple(choice.id for choice in choices) == ALLOWED_MODEL_IDS, (
        "choice order must match ALLOWED_MODEL_IDS"
    )


def test_claude_model_choices_labels_match_display_names() -> None:
    choices = claude_model_choices(default=ALLOWED_MODEL_IDS[0])
    for choice in choices:
        assert choice.label == MODEL_DISPLAY_NAMES.get(choice.id, choice.id), (
            "label must come from MODEL_DISPLAY_NAMES"
        )


ROOT = Path(__file__).resolve().parents[3]
_ID_TABLE_OWNERS = frozenset({"pricing.py", "constants.py"})


def _production_sources() -> list[Path]:
    roots = [
        ROOT / "packages/core/daimon",
        *sorted((ROOT / "packages/adapters").glob("*/daimon")),
        ROOT / "apps",
    ]
    return [path for root in roots for path in root.rglob("*.py") if "tests" not in path.parts]


def _id_table_reads(path: Path) -> list[int]:
    lines: list[int] = []
    for node in ast.walk(ast.parse(path.read_text())):
        imported = isinstance(node, ast.ImportFrom) and any(
            alias.name == "ALLOWED_MODEL_IDS" for alias in node.names
        )
        if imported or (isinstance(node, ast.Attribute) and node.attr == "ALLOWED_MODEL_IDS"):
            lines.append(node.lineno)
    return lines


def test_only_the_catalog_reads_the_raw_model_id_table() -> None:
    """Every surface reads model ids through `models_catalog`, never `ALLOWED_MODEL_IDS`."""
    readers = [
        f"{path.relative_to(ROOT)}:{line}"
        for path in _production_sources()
        if path.name not in _ID_TABLE_OWNERS
        for line in _id_table_reads(path)
    ]
    assert not readers, f"read model ids via daimon.core.models_catalog instead: {readers}"


def _tenant(*enabled: str) -> TenantBackendPolicy:
    return TenantBackendPolicy(
        tenant_id=uuid.uuid4(),
        revision="1",
        enabled_backends=("anthropic", *enabled),  # type: ignore[arg-type]
    )


def test_catalog_hides_a_provider_the_tenant_has_not_enabled() -> None:
    """A tenant allowing Gemini only sees Claude and Gemini, never OpenAI."""
    providers = {
        choice.provider for choice in list_model_choices(tenant=_tenant("gemini"), default=None)
    }
    assert providers == {"anthropic", "gemini"}, f"allowlist must hide OpenAI: {providers}"


def test_catalog_without_provider_choice_is_claude_only() -> None:
    """No tenant policy means provider choice is off: Claude models only."""
    providers = {choice.provider for choice in list_model_choices(tenant=None, default=None)}
    assert providers == {"anthropic"}, f"no policy must mean Claude only: {providers}"


def test_catalog_hides_a_model_whose_price_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    """A model whose only price window has closed is not offered."""
    expired = {
        profile: tuple(
            dataclasses.replace(row, checked_on=date(2025, 12, 1), valid_before=date(2026, 1, 1))
            if row.model == "gpt-6-astra"
            else row
            for row in rows
        )
        for profile, rows in pricing._PROVIDER_PRICING.items()  # pyright: ignore[reportPrivateUsage]
    }
    monkeypatch.setattr(pricing, "_PROVIDER_PRICING", expired)
    ids = {choice.id for choice in list_model_choices(tenant=_tenant("openai"), default=None)}
    assert "gpt-6-astra" not in ids, "an expired price must hide the model"
    assert "gpt-6-luna" in ids, "a current price keeps its model"


def test_catalog_labels_name_model_provider_and_price() -> None:
    """Each entry reads as the model's name, its provider in plain words and its price."""
    choice = next(
        c
        for c in list_model_choices(tenant=_tenant("openai"), default=None)
        if c.id == "gpt-6-luna"
    )
    assert choice.label == "GPT-6 Luna", f"label is the model's name: {choice.label}"
    assert choice.description is not None, "the description carries provider and price"
    assert choice.description.startswith("OpenAI, ~$"), f"provider then price: {choice.description}"
    assert choice.value == "openai:gpt-6-luna", "the value is provider:id"


def test_catalog_flags_only_the_requested_default() -> None:
    """Exactly the default model is flagged, across providers."""
    default = ModelRef(provider="openai", id="gpt-6-luna")
    flagged = [
        c.model
        for c in list_model_choices(tenant=_tenant("openai"), default=default)
        if c.is_default
    ]
    assert flagged == [default], f"only the default is flagged: {flagged}"


def test_catalog_model_reads_provider_and_id() -> None:
    """`provider:id`, a bare id and a name each pick a catalog model."""
    tenant = _tenant("openai")
    luna = ModelRef(provider="openai", id="gpt-6-luna")
    assert catalog_model("openai:gpt-6-luna", tenant=tenant) == luna, "provider:id"
    assert catalog_model("gpt-6-luna", tenant=tenant) == luna, "bare id"
    assert catalog_model("GPT-6 Luna", tenant=tenant) == luna, "display name"


@pytest.mark.parametrize("value", ["openai:gpt-6-luna", "gemini:gemini-4-argon", "nope"])
def test_catalog_model_refuses_what_the_tenant_cannot_run(value: str) -> None:
    """A model outside this tenant's catalog is refused as not selectable."""
    with pytest.raises(AgentBackendRefused) as refused:
        catalog_model(value, tenant=None)
    assert refused.value.reason == "model_not_selectable", f"{value} is refused as not selectable"
