"""Single-turn Messages completions."""

from __future__ import annotations

from anthropic import AsyncAnthropic, omit
from anthropic.types import TextBlock

from mux.contracts.completions import CompletionEffort, CompletionResult, CompletionUsage
from mux.contracts.ids import Scope
from mux.drivers.anthropic.resources._errors import provider_call


class AnthropicCompletions:
    def __init__(self, client: AsyncAnthropic) -> None:
        self._client = client

    async def complete(
        self,
        scope: Scope,
        model: str,
        system: str,
        prompt: str,
        max_tokens: int,
        effort: CompletionEffort | None = None,
    ) -> CompletionResult:
        thinking = {"type": "disabled"} if effort == "none" else omit
        output_config = {"effort": effort} if effort not in (None, "none") else omit
        response = await provider_call(
            self._client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                thinking=thinking,  # pyright: ignore[reportArgumentType]
                output_config=output_config,  # pyright: ignore[reportArgumentType]
            )
        )
        refused = response.stop_reason == "refusal"
        text = "".join(block.text for block in response.content if isinstance(block, TextBlock))
        return CompletionResult(
            text="" if refused else text,
            usage=CompletionUsage(
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                cache_read_input_tokens=response.usage.cache_read_input_tokens or 0,
            ),
            refused=refused,
        )
