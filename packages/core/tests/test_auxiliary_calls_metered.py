"""Side calls (titles, participation verdicts) run on any provider and are always metered."""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest
from anthropic import AsyncAnthropic
from daimon.core import auxiliary_calls
from daimon.core.anthropic_guard import ANTHROPIC_UNCONFIGURED_PLACEHOLDER
from daimon.core.auxiliary_calls import register_completions
from daimon.core.auxiliary_models import AUXILIARY_MODELS
from daimon.core.pricing import MODEL_PRICING
from daimon.core.thread_classifier import classify
from daimon.core.thread_naming import THREAD_NAMING_MODEL, suggest_thread_name
from daimon.core.thread_participation import SILENCE_ON_ERROR
from mux.contracts.completions import CompletionResult, CompletionUsage
from mux.contracts.ids import Provider, Scope
from mux.errors import ProviderError

KEYLESS = AsyncAnthropic(api_key=ANTHROPIC_UNCONFIGURED_PLACEHOLDER)
KEYED = AsyncAnthropic(api_key="test-key")


class _Fake:
    def __init__(self, text: str = "Weekly report", error: ProviderError | None = None) -> None:
        self.text, self.error = text, error
        self.models: list[str] = []

    async def complete(
        self,
        scope: Scope,
        model: str,
        system: str,
        prompt: str,
        max_tokens: int,
        effort: str | None = None,
    ) -> CompletionResult:
        self.models.append(model)
        if self.error is not None:
            raise self.error
        return CompletionResult(
            text=self.text, usage=CompletionUsage(input_tokens=11, output_tokens=4)
        )


@pytest.fixture
def registered() -> Iterator[dict[Provider, _Fake]]:
    fakes: dict[Provider, _Fake] = {"openai": _Fake(), "gemini": _Fake()}
    saved = dict(auxiliary_calls._FACTORIES)  # pyright: ignore[reportPrivateUsage]
    for provider, fake in fakes.items():
        register_completions(provider, lambda fake=fake: fake)  # type: ignore[misc]
    yield fakes
    auxiliary_calls._FACTORIES.clear()  # pyright: ignore[reportPrivateUsage]
    auxiliary_calls._FACTORIES.update(saved)  # pyright: ignore[reportPrivateUsage]


def test_every_auxiliary_model_is_priced() -> None:
    for provider, model in AUXILIARY_MODELS.items():
        assert MODEL_PRICING.get(model) is not None, f"{provider} side-call model is unpriced"


def test_anthropic_keeps_the_model_titles_always_ran_on() -> None:
    assert THREAD_NAMING_MODEL == "claude-haiku-5-5", "production side calls are unchanged"


@pytest.mark.parametrize("provider", ["openai", "gemini"])
async def test_naming_works_without_an_anthropic_key(
    registered: dict[Provider, _Fake], provider: Provider
) -> None:
    suggestion = await suggest_thread_name(
        KEYLESS, message_text="plan the weekly report", max_input_chars=2000, provider=provider
    )
    assert suggestion.name == "Weekly report", "the title comes from the provider's driver"
    assert (suggestion.provider, suggestion.model_id) == (provider, AUXILIARY_MODELS[provider]), (
        "the call is metered against the model it ran on"
    )
    assert registered[provider].models == [AUXILIARY_MODELS[provider]], "that driver was called"


@pytest.mark.parametrize("provider", ["openai", "gemini"])
async def test_classifier_fails_closed_on_a_driver_error_without_a_key(
    registered: dict[Provider, _Fake], provider: Provider
) -> None:
    registered[provider].error = ProviderError("overloaded", retryable=True)
    outcome = await classify(
        KEYLESS,
        model="claude-haiku-5-5",
        bot_display_name="Bot",
        recent=[],
        candidates=[],
        provider=provider,
    )
    assert outcome.verdict == SILENCE_ON_ERROR, "an outage means silence"
    assert outcome.usage is None, "a failed call has nothing to meter"


async def test_classifier_fails_closed_when_no_provider_is_reachable() -> None:
    outcome = await classify(
        KEYLESS, model="m", bot_display_name="Bot", recent=[], candidates=[], provider="openai"
    )
    assert outcome.verdict == SILENCE_ON_ERROR, "no driver registered means silence"


def test_no_core_module_calls_a_provider_completion_directly() -> None:
    root = Path(__file__).resolve().parents[1] / "daimon"
    offenders = [
        f"{path.name}:{node.lineno}"
        for path in root.rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Attribute)
        and node.attr in {"generate_content"}
        or isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "create"
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "messages"
    ]
    assert not offenders, f"side calls go through the completions port: {offenders}"
