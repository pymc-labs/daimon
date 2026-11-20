"""The judge's model client: one side call per request, through the same drivers the
install's other side calls use."""

from __future__ import annotations

from typing import Final

from anthropic import AsyncAnthropic
from daimon.core.anthropic_guard import anthropic_configured, require_anthropic_allowed
from daimon.core.evals.format import ModelChoice
from daimon.core.evals.scoring.judges import Completion
from daimon.core.turn.runtimes import provider_completions
from mux.contracts.completions import CompletionEffort, Completions
from mux.contracts.config import DEFAULT_BACKEND
from mux.contracts.ids import PROVIDERS, Scope
from mux.drivers.anthropic import AnthropicCompletions

_EFFORT: Final[dict[str, CompletionEffort]] = {
    "low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high",
}  # fmt: skip


class JudgeUnavailable(Exception):
    """No provider with a key can serve this judge model."""


class MuxJudgeClient:
    def __init__(self, anthropic: AsyncAnthropic | None) -> None:
        self._anthropic = anthropic

    def _driver(self, model: ModelChoice) -> Completions:
        backend = model.backend or DEFAULT_BACKEND
        if backend == DEFAULT_BACKEND:
            if self._anthropic is None or not anthropic_configured(self._anthropic):
                raise JudgeUnavailable(f"No Anthropic key for judge model {model.model}.")
            require_anthropic_allowed()
            return AnthropicCompletions(self._anthropic)
        factory = next((provider_completions(p) for p in PROVIDERS if p == backend), None)
        if factory is None:
            raise JudgeUnavailable(f"No {backend} key for judge model {model.model}.")
        return factory()

    async def complete(
        self, model: ModelChoice, system: str, prompt: str, max_tokens: int
    ) -> Completion:
        driver = self._driver(model)
        scope = Scope.legacy_host_authorized(call_site="evals:judge")
        effort = None if model.effort is None else _EFFORT.get(model.effort)
        done = await driver.complete(scope, model.model, system, prompt, max_tokens, effort)
        usage = done.usage
        return Completion(
            text=done.text, input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_input_tokens, refused=done.refused,
        )  # fmt: skip
