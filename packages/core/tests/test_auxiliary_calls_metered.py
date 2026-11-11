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
from daimon.core.pricing import MODEL_PRICING, side_call_rates
from daimon.core.provider_keys import DEFAULT_KEY_ENV
from daimon.core.thread_classifier import classify
from daimon.core.thread_naming import THREAD_NAMING_MODEL, suggest_thread_name
from daimon.core.thread_participation import SILENCE_ON_ERROR
from daimon.core.turn import gemini_deployment, openai_deployment
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
    """Each side-call model has a rate, and only Anthropic's comes from the tool table."""
    for provider, model in AUXILIARY_MODELS.items():
        rates = side_call_rates(model)
        assert rates.input > 0 and rates.output > 0, f"{provider} side-call model is unpriced"
        in_table = model in MODEL_PRICING
        assert in_table == (provider == "anthropic"), f"{model} is priced from a sourced row"


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


async def test_naming_without_an_admitted_provider_uses_the_first_keyed(
    registered: dict[Provider, _Fake],
) -> None:
    """Callers that run before admission get the key registry's first provider."""
    suggestion = await suggest_thread_name(
        KEYLESS, message_text="plan the weekly report", max_input_chars=2000
    )
    first = next(iter(DEFAULT_KEY_ENV))
    assert suggestion.provider == first, f"expected {first}, got {suggestion.provider}"


@pytest.mark.parametrize("provider", ["openai", "gemini"])
def test_runtime_registers_side_calls_only_with_a_key(
    monkeypatch: pytest.MonkeyPatch, provider: Provider
) -> None:
    """Each provider runtime makes its completions reachable when its deployment key is set."""
    register = {"openai": openai_deployment, "gemini": gemini_deployment}[provider]
    saved = dict(auxiliary_calls._FACTORIES)  # pyright: ignore[reportPrivateUsage]
    try:
        for keyed in (False, True):
            auxiliary_calls._FACTORIES.clear()  # pyright: ignore[reportPrivateUsage]
            if keyed:
                monkeypatch.setenv(DEFAULT_KEY_ENV[provider], "test-key")
            else:
                monkeypatch.delenv(DEFAULT_KEY_ENV[provider], raising=False)
            register.register_runtime()
            registered = provider in auxiliary_calls._FACTORIES  # pyright: ignore[reportPrivateUsage]
            assert registered == keyed, (
                f"{provider} side calls registered={registered}, key={keyed}"
            )
    finally:
        auxiliary_calls._FACTORIES.clear()  # pyright: ignore[reportPrivateUsage]
        auxiliary_calls._FACTORIES.update(saved)  # pyright: ignore[reportPrivateUsage]


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
