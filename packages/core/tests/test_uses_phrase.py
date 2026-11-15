"""What a changed agent runs on is said as a model name, never a copy of the agent."""

import pytest
from daimon.core.turn.notices import uses_phrase


@pytest.mark.parametrize(
    ("provider", "model", "said"),
    [
        ("gemini", "gemini-4-argon", "Gemini 4 Argon"),
        ("openai", None, "OpenAI"),
        ("gemini", None, "Gemini"),
        ("selfhosted", "llama-x", "a self-hosted model (llama-x)"),
    ],
)
def test_a_model_is_named_the_way_a_person_says_it(provider, model, said):
    assert uses_phrase(provider, model) == said
