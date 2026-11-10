"""Single-turn generate-content completions."""

from __future__ import annotations

from google.genai import Client, types

from mux.contracts.completions import CompletionEffort, CompletionResult, CompletionUsage
from mux.contracts.ids import Scope
from mux.drivers.gemini.transport import provider_call


class GeminiCompletions:
    def __init__(self, client: Client) -> None:
        self._models = client.aio.models

    async def complete(
        self,
        scope: Scope,
        model: str,
        system: str,
        prompt: str,
        max_tokens: int,
        effort: CompletionEffort | None = None,
    ) -> CompletionResult:
        response = await provider_call(
            self._models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system, max_output_tokens=max_tokens
                ),
            )
        )
        usage = response.usage_metadata
        cached = (usage.cached_content_token_count or 0) if usage else 0
        prompt_tokens = (usage.prompt_token_count or 0) if usage else 0
        # Thinking tokens bill as output.
        output = (
            ((usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0))
            if usage
            else 0
        )
        blocked = response.prompt_feedback is not None and response.prompt_feedback.block_reason
        return CompletionResult(
            text="" if blocked else (response.text or ""),
            usage=CompletionUsage(
                input_tokens=max(prompt_tokens - cached, 0),
                output_tokens=output,
                cache_read_input_tokens=cached,
            ),
            refused=bool(blocked),
        )
