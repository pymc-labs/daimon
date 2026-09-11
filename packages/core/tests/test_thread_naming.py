"""Tests for daimon.core.thread_naming.

The pure half (``parse_thread_name``) is exercised directly. ``suggest_thread_name`` runs against a real ``AsyncAnthropic`` over
``httpx.MockTransport`` with an inline ``anthropic.types.Message`` payload,
per guideline:testing — the SDK's request validation and response parsing
run for real.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from anthropic import BadRequestError
from anthropic.types import Message, TextBlock, Usage
from daimon.core.thread_naming import (
    THREAD_NAME_MAX_CHARS,
    THREAD_NAMING_MODEL,
    parse_thread_name,
    suggest_thread_name,
)
from daimon.testing.ma import build_fake_anthropic

# ---------------------------------------------------------------------------
# parse_thread_name (pure)
# ---------------------------------------------------------------------------


def test_parse_thread_name_returns_stripped_title_when_model_answers_plainly() -> None:
    assert parse_thread_name("  PyMC Error on M2 Mac \n") == "PyMC Error on M2 Mac", (
        "surrounding whitespace and the trailing newline must be stripped"
    )


def test_parse_thread_name_drops_wrapping_quotes_when_model_quotes_title() -> None:
    assert parse_thread_name('"NUTS vs Metropolis Samplers"') == "NUTS vs Metropolis Samplers", (
        "a quoted title must be unwrapped, not stored with its quotes"
    )


@pytest.mark.parametrize("raw", ["NONE", "none\n", "", "   ", "\n\n"])
def test_parse_thread_name_returns_none_when_model_declines_or_answers_blank(raw: str) -> None:
    assert parse_thread_name(raw) is None, f"{raw!r} carries no title and must map to None"


def test_parse_thread_name_uses_first_non_empty_line_when_model_rambles() -> None:
    assert parse_thread_name("\nBayesian A/B Test Setup\nHere is why...") == (
        "Bayesian A/B Test Setup"
    ), "only the first non-empty line is the title; any trailing explanation is dropped"


def test_parse_thread_name_truncates_at_word_boundary_when_title_is_too_long() -> None:
    long_title = " ".join(["word"] * 40)  # 199 chars
    title = parse_thread_name(long_title)
    assert title is not None, "a long title is still a title"
    assert len(title) <= THREAD_NAME_MAX_CHARS, "title must fit the cap"
    assert not title.endswith(" ") and title.split(" ")[-1] == "word", (
        "truncation must land on a word boundary, never mid-word"
    )


def test_parse_thread_name_hard_cuts_when_no_space_before_cap() -> None:
    title = parse_thread_name("x" * 120)
    assert title == "x" * THREAD_NAME_MAX_CHARS, (
        "a single over-long token has no word boundary, so it is cut at the cap"
    )


# ---------------------------------------------------------------------------
# suggest_thread_name (shell, transport-level fake)
# ---------------------------------------------------------------------------


def _message_payload(text: str) -> dict[str, Any]:
    return Message(
        id="msg_thread_name",
        type="message",
        role="assistant",
        model=THREAD_NAMING_MODEL,
        content=[TextBlock(type="text", text=text)],
        stop_reason="end_turn",
        stop_sequence=None,
        usage=Usage(
            input_tokens=120,
            output_tokens=9,
            cache_creation_input_tokens=None,
            cache_read_input_tokens=None,
        ),
    ).model_dump(mode="json")


async def test_suggest_thread_name_calls_haiku_with_truncated_message_and_maps_usage() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/messages", "naming must use the plain Messages API"
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=_message_payload("PyMC Error on M2 Mac"))

    message_text = "I get a weird error running PyMC on my M2 Mac " + "x" * 100
    suggestion = await suggest_thread_name(
        build_fake_anthropic(handler), message_text=message_text, max_input_chars=40
    )

    assert suggestion.name == "PyMC Error on M2 Mac", "the model's title is returned"
    assert (suggestion.usage.input_tokens, suggestion.usage.cache_read_input_tokens) == (
        120,
        0,
    ), "usage must come from the response, with an absent cache count mapped to 0"
    assert len(seen) == 1, "exactly one model call per suggestion"
    assert seen[0]["model"] == THREAD_NAMING_MODEL, "the pinned, priced model must be requested"
    assert seen[0]["messages"][0]["content"] == message_text[:40], (
        "the opening message must be cut at max_input_chars before it is sent"
    )


async def test_suggest_thread_name_returns_none_name_but_usage_when_model_says_none() -> None:
    suggestion = await suggest_thread_name(
        build_fake_anthropic(lambda _r: httpx.Response(200, json=_message_payload("NONE"))),
        message_text="hey",
        max_input_chars=2000,
    )
    assert suggestion.name is None, "NONE must map to no title"
    assert suggestion.usage.output_tokens == 9, (
        "a declined title still cost tokens; usage must be reported so the caller meters it"
    )


async def test_suggest_thread_name_propagates_api_errors() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"type": "error", "error": {"message": "bad"}})

    with pytest.raises(BadRequestError):
        await suggest_thread_name(
            build_fake_anthropic(handler), message_text="hello there", max_input_chars=2000
        )
