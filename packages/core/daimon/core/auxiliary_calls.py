"""Small side calls on whichever provider the install can reach.

Anthropic whenever a key exists, so production behaves as before. Otherwise the
admitted provider, through a registered completions driver. A self-hosted turn
makes no call.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from anthropic import AsyncAnthropic
from daimon.core.anthropic_guard import anthropic_configured, require_anthropic_allowed
from daimon.core.auxiliary_models import AUXILIARY_MODELS, side_call_priced
from daimon.core.errors import DaimonError
from daimon.core.provider_keys import DEFAULT_KEY_ENV
from mux.contracts.completions import CompletionEffort, Completions, CompletionUsage
from mux.contracts.ids import Provider, Scope
from mux.drivers.anthropic import AnthropicCompletions

type AuxiliaryPurpose = Literal["thread_naming", "classifier"]
type CompletionsFactory = Callable[[], Completions]

_FACTORIES: dict[Provider, CompletionsFactory] = {}


class AuxiliaryUnavailable(DaimonError):
    """No provider can serve a side call on this install."""


def register_completions(provider: Provider, factory: CompletionsFactory) -> None:
    """Make a provider's completions driver reachable for keyless installs."""
    _FACTORIES[provider] = factory


@dataclass(frozen=True)
class AuxiliaryCompletion:
    text: str
    usage: CompletionUsage
    refused: bool
    provider: Provider
    model_id: str
    """What the call ran on, for metering."""


def auxiliary_provider(anthropic: AsyncAnthropic, admitted: Provider | None) -> Provider:
    """Anthropic when keyed, else the admitted provider, else the first reachable one.

    "First" follows the deployment key registry, the order keyless records default to.
    A provider whose side-call model has no price today is skipped.
    """
    if anthropic_configured(anthropic):
        return "anthropic"
    for provider in (admitted, *DEFAULT_KEY_ENV):
        if provider is not None and provider in _FACTORIES and side_call_priced(provider):
            return provider
    raise AuxiliaryUnavailable("No provider is available for this call.")


async def auxiliary_complete(
    anthropic: AsyncAnthropic,
    *,
    provider: Provider | None,
    purpose: AuxiliaryPurpose,
    system: str,
    prompt: str,
    max_tokens: int,
    effort: CompletionEffort | None = None,
    anthropic_model: str | None = None,
) -> AuxiliaryCompletion:
    """One side call. Metering is the caller's (`usage_recording.record_auxiliary_usage`)."""
    require_anthropic_allowed()
    chosen = auxiliary_provider(anthropic, provider)
    model = (anthropic_model if chosen == "anthropic" else None) or AUXILIARY_MODELS[chosen]
    driver = AnthropicCompletions(anthropic) if chosen == "anthropic" else _FACTORIES[chosen]()
    scope = Scope.legacy_host_authorized(call_site=f"auxiliary_calls:{purpose}")
    result = await driver.complete(scope, model, system, prompt, max_tokens, effort)
    return AuxiliaryCompletion(result.text, result.usage, result.refused, chosen, model)
