"""Single-turn Responses completions."""

from __future__ import annotations

import httpx
from openai import APIError, AsyncOpenAI, omit

from mux.contracts.completions import CompletionEffort, CompletionResult, CompletionUsage
from mux.contracts.ids import Scope
from mux.drivers.openai.transport import provider_error


class OpenAICompletions:
    def __init__(self, client: AsyncOpenAI) -> None:
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
        reasoning = {"effort": effort} if effort is not None else omit
        try:
            response = await self._client.responses.create(
                model=model,
                instructions=system,
                input=prompt,
                max_output_tokens=max_tokens,
                store=False,
                reasoning=reasoning,  # pyright: ignore[reportArgumentType]
            )
        except (APIError, httpx.HTTPError) as error:
            raise provider_error(error) from error
        usage = response.usage
        cached = usage.input_tokens_details.cached_tokens if usage else 0
        return CompletionResult(
            text=response.output_text,
            usage=CompletionUsage(
                input_tokens=(usage.input_tokens - cached) if usage else 0,
                output_tokens=usage.output_tokens if usage else 0,
                cache_read_input_tokens=cached,
            ),
        )
