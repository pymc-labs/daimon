"""Completions drivers: one request out, text and billable usage back."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import cast

import httpx
import pytest
from google.genai import Client
from mux.contracts.ids import Scope
from mux.drivers.anthropic.completions import AnthropicCompletions
from mux.drivers.gemini.completions import GeminiCompletions
from mux.drivers.openai.completions import OpenAICompletions
from mux.errors import ProviderError
from mux.testing.drivers.anthropic.ma import MARouter, build_fake_anthropic
from openai import AsyncOpenAI

SCOPE = Scope(tenant_id="t", account_id="a", principal_id="p", authorization_id="allowed")


async def test_anthropic_completion_sends_one_message_and_reports_usage() -> None:
    seen: list[dict[str, object]] = []

    def on_create(request: httpx.Request, match: object) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude-haiku-5-5",
                "content": [{"type": "text", "text": "A title"}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 12, "output_tokens": 3, "cache_read_input_tokens": 5},
            },
        )

    router = MARouter()
    router.add("POST", r"/v1/messages", on_create)
    drv = AnthropicCompletions(build_fake_anthropic(router.dispatch))
    result = await drv.complete(SCOPE, "claude-haiku-5-5", "sys", "hi", 60, "none")
    assert result.text == "A title", "the text blocks are joined"
    assert (result.usage.input_tokens, result.usage.output_tokens) == (12, 3), "usage is carried"
    assert result.usage.cache_read_input_tokens == 5, "cache reads are reported apart"
    assert seen[0]["thinking"] == {"type": "disabled"}, "effort none turns thinking off"
    assert seen[0]["system"] == "sys" and seen[0]["max_tokens"] == 60, "system and cap are sent"


async def test_anthropic_completion_errors_are_normalised() -> None:
    router = MARouter()
    router.add(
        "POST",
        r"/v1/messages",
        lambda request, match: httpx.Response(
            429, json={"type": "error", "error": {"type": "rate_limit_error", "message": "x"}}
        ),
    )
    drv = AnthropicCompletions(build_fake_anthropic(router.dispatch))
    with pytest.raises(ProviderError) as raised:
        await drv.complete(SCOPE, "claude-haiku-5-5", "s", "p", 10)
    assert raised.value.category == "rate_limited", "SDK errors surface as provider errors"


async def test_openai_completion_reads_output_text_and_cached_usage() -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "resp_1",
                "object": "response",
                "created_at": 1,
                "status": "completed",
                "model": "gpt-5-mini",
                "parallel_tool_calls": True,
                "tool_choice": "auto",
                "tools": [],
                "output": [
                    {
                        "id": "msg_1",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": "Title", "annotations": []}],
                    }
                ],
                "usage": {
                    "input_tokens": 20,
                    "output_tokens": 4,
                    "total_tokens": 24,
                    "input_tokens_details": {"cached_tokens": 8},
                    "output_tokens_details": {"reasoning_tokens": 0},
                },
            },
        )

    sdk = AsyncOpenAI(
        api_key="k", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    result = await OpenAICompletions(sdk).complete(SCOPE, "gpt-5-mini", "sys", "hi", 60, "low")
    assert result.text == "Title", "output text is returned"
    assert result.usage.input_tokens == 12, "cached tokens are split out of the input"
    assert result.usage.cache_read_input_tokens == 8, "cache reads are reported apart"
    assert bodies[0]["reasoning"] == {"effort": "low"}, "effort is forwarded"
    assert bodies[0]["store"] is False, "nothing is retained"


async def test_gemini_completion_bills_thinking_as_output() -> None:
    async def generate_content(**kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            text="Title",
            prompt_feedback=None,
            usage_metadata=SimpleNamespace(
                prompt_token_count=30,
                cached_content_token_count=10,
                candidates_token_count=4,
                thoughts_token_count=6,
            ),
        )

    client = SimpleNamespace(
        aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    )
    result = await GeminiCompletions(cast(Client, client)).complete(
        SCOPE, "gemini-3.8-flash", "sys", "hi", 60
    )
    assert result.text == "Title", "the response text is returned"
    assert result.usage.input_tokens == 20, "cached tokens are split out of the prompt"
    assert result.usage.output_tokens == 10, "thinking tokens bill as output"
