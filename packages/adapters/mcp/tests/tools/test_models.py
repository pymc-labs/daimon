"""list_models answers "what models can I use / what does luna cost?" (friction #30)."""

import pytest
from daimon.adapters.mcp.tools.models import _list_models_impl
from daimon.core.agent_model_names import resolve_model
from mux.contracts.ids import ModelRef


def test_list_models_names_every_enabled_model_with_its_price_and_every_name_resolves():
    listed = _list_models_impl(("anthropic", "openai", "gemini"))
    by_id = {row["id"]: row for row in listed if isinstance(row, dict)}
    assert by_id["gpt-6-luna"]["name"] == "GPT-6 Luna" and "$" in str(by_id["gpt-6-luna"]["price"])
    for model_id in ("claude-opus-5-5", "gpt-6-astra", "gemini-4-argon"):
        assert model_id in by_id
    # The fast preset is a local key, not a model an agent or worker is set to by id.
    assert "claude-opus-5-5-fast" not in by_id
    for row in by_id.values():
        model = ModelRef(provider=row["provider"], id=row["id"])  # type: ignore[arg-type]
        assert resolve_model(str(row["name"])) == model


@pytest.mark.parametrize(
    ("enabled", "providers"),
    [
        (("anthropic",), {"anthropic"}),
        (("anthropic", "openai"), {"anthropic", "openai"}),
        (("anthropic", "gemini"), {"anthropic", "gemini"}),
    ],
)
def test_list_models_lists_only_the_tenants_enabled_providers(enabled, providers):
    listed = _list_models_impl(enabled)
    assert {row["provider"] for row in listed if isinstance(row, dict)} == providers
