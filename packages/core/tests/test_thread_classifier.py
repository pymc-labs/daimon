"""Tests for daimon.core.thread_classifier against a transport-level fake (guideline:testing)."""

from __future__ import annotations

import json
from typing import Any

import httpx
from anthropic.types import Message, StopReason, TextBlock, Usage
from daimon.core.thread_classifier import classify
from daimon.core.thread_participation import SILENCE_ON_ERROR, ClassifierMessage
from mux.testing.drivers.anthropic.ma import build_fake_anthropic

_MODEL = "claude-haiku-5-5"
_RESPOND = '{"decision": "respond", "reason": "asked the bot"}'


def _reply(text: str, stop_reason: StopReason = "end_turn") -> dict[str, Any]:
    return Message(
        id="msg_classifier",
        type="message",
        role="assistant",
        model=_MODEL,
        content=[TextBlock(type="text", text=text)],
        stop_reason=stop_reason,
        stop_sequence=None,
        usage=Usage(
            input_tokens=120,
            output_tokens=9,
            cache_creation_input_tokens=None,
            cache_read_input_tokens=None,
        ),
    ).model_dump(mode="json")


async def _classify(payload: dict[str, Any], seen: list[dict[str, Any]] | None = None) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(json.loads(request.content))
        return httpx.Response(200, json=payload)

    return await classify(
        build_fake_anthropic(handler),
        model=_MODEL,
        bot_display_name="Daimon",
        recent=[],
        candidates=[ClassifierMessage("user-1", "daimon, what is the prior?", is_bot=False)],
    )


async def test_classify_sends_low_effort_and_large_cap_and_returns_verdict_with_usage() -> None:
    seen: list[dict[str, Any]] = []
    outcome = await _classify(_reply(_RESPOND), seen)

    assert outcome.verdict.decision == "respond", "a well-formed verdict is returned"
    assert outcome.usage is not None and outcome.usage.output_tokens == 9, "usage is reported"
    assert seen[0]["output_config"] == {"effort": "low"}, (
        "low effort keeps thinking rare and was the most accurate setting"
    )
    assert "thinking" not in seen[0], "thinking stays adaptive (unset)"
    assert seen[0]["max_tokens"] == 1024, "a 100-token cap truncated the JSON verdict"


async def test_classify_fails_closed_to_silence_but_meters_when_model_refuses() -> None:
    outcome = await _classify(_reply(_RESPOND, stop_reason="refusal"))

    assert outcome.verdict == SILENCE_ON_ERROR, "a refusal must never turn into a reply"
    assert outcome.usage is not None and outcome.usage.input_tokens == 120, (
        "a refused call still cost tokens, so its usage must reach the meter"
    )
