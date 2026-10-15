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
from anthropic.types import Message, StopReason, TextBlock, Usage
from daimon.core.thread_naming import (
    _SYSTEM_PROMPT,  # pyright: ignore[reportPrivateUsage]
    THREAD_NAME_MAX_CHARS,
    THREAD_NAMING_MODEL,
    fallback_thread_name,
    is_generic_thread_name,
    naming_text,
    parse_thread_name,
    strip_mentions,
    suggest_thread_name,
)
from daimon.testing.ma import build_fake_anthropic

# ---------------------------------------------------------------------------
# strip_mentions (pure)
# ---------------------------------------------------------------------------


def test_strip_mentions_removes_user_role_and_channel_tokens() -> None:
    assert strip_mentions("<@999> ask <@!42> and <@&7> in <#123>  please") == "ask and in please", (
        "every mention token must go and the gaps must collapse to single spaces"
    )


def test_strip_mentions_leaves_only_empty_for_attachment_only_mention() -> None:
    assert strip_mentions("<@999>") == "", (
        "a bare bot mention has no text to title; the caller must skip the metered call"
    )


def test_strip_mentions_drops_a_plain_text_bot_name_when_names_are_given() -> None:
    """A turn started by a typed "@Daimon" must not carry the name into the title."""
    assert strip_mentions("@Daimon summarise the Q3 report", ["Daimon"]) == (
        "summarise the Q3 report"
    ), "the plain-text name must go so the fallback title starts with the request"
    assert strip_mentions("@Daimon summarise", []) == "@Daimon summarise", (
        "without names the text is left alone"
    )


def test_naming_text_puts_file_names_before_the_text() -> None:
    assert naming_text("shorten this", ["brief.pdf", "notes.md"]) == (
        "Attached: brief.pdf, notes.md\nshorten this"
    ), "file names lead, ahead of a long message tail that max_input_chars may cut"


def test_naming_text_names_a_file_only_mention_and_leaves_plain_text_alone() -> None:
    assert naming_text("", ["q3_sales.csv"]) == "Attached: q3_sales.csv", (
        "a mention with only a file is titled from its name"
    )
    assert naming_text("hello", []) == "hello", "no files, no prefix"


def test_prompt_asks_for_short_sentence_case_titles() -> None:
    assert "sentence case" in _SYSTEM_PROMPT and "title case" not in _SYSTEM_PROMPT, (
        "titles read like something a person would type, not a headline"
    )
    assert THREAD_NAME_MAX_CHARS == 50, "short enough to scan in the thread list"


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


@pytest.mark.parametrize("raw", ["", "   ", "\n\n", '""'])
def test_parse_thread_name_returns_none_when_model_answers_blank(raw: str) -> None:
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


# ---------------------------------------------------------------------------
# fallback_thread_name / is_generic_thread_name (pure)
# ---------------------------------------------------------------------------


def test_fallback_thread_name_quotes_the_opening_message_not_an_agent_name() -> None:
    title = fallback_thread_name("hi! what can you do? I'm new here.", default="Chat with daimon")
    assert title == "Hi! what can you do? I'm new here.", (
        "a greeting is titled from its own words, capitalised, never from the agent slug"
    )
    assert not is_generic_thread_name(title)


def test_fallback_thread_name_uses_first_line_and_fits_the_cap() -> None:
    title = fallback_thread_name(
        "\n  please   look at " + " ".join(["word"] * 30) + "\nsecond line",
        default="Chat with daimon",
    )
    assert title.startswith("Please look at word"), "first non-empty line, whitespace collapsed"
    assert len(title) <= THREAD_NAME_MAX_CHARS, "the fallback obeys the same cap"
    assert title.split(" ")[-1] == "word", "cut lands on a word boundary"


@pytest.mark.parametrize("text", ["", "   \n ", "hi", "Hello!"])
def test_fallback_thread_name_uses_default_when_message_says_nothing(text: str) -> None:
    assert fallback_thread_name(text, default="Chat with daimon") == "Chat with daimon", (
        "nothing to quote, or only a bare greeting, falls back to the bot's presented name"
    )
    assert is_generic_thread_name("Chat with daimon"), "the default is a generic placeholder"


@pytest.mark.parametrize(
    "title",
    [
        "Greeting",
        "hello",
        "Help request",
        "Hi!",
        "Introduction",
        "Chat with Daimon",
        "chat with you",
        "CHAT WITH qa-haiku-canary-20261010",
        "Chat with ...",
        "Chat with ?",
        "CHAT WITH !!!",
        "chat with",
        "Chat with:",
    ],
)
def test_is_generic_thread_name_flags_titles_that_name_nothing(title: str) -> None:
    assert is_generic_thread_name(title), f"{title!r} says nothing about the thread"


def test_cut_keeps_a_word_that_ends_exactly_at_the_cap() -> None:
    text = "Check " + "x" * 44 + " extra"  # the 50th character ends the x-word
    assert parse_thread_name(text) == "Check " + "x" * 44, "a whole word at the cap is kept"
    assert fallback_thread_name(text, default="d") == "Check " + "x" * 44, (
        "the fallback cuts the same way"
    )


def test_cut_drops_a_word_that_crosses_the_cap() -> None:
    text = "Check " + "x" * 45 + " extra"  # the x-word runs past character 50
    assert parse_thread_name(text) == "Check", "a word split by the cap is dropped"


@pytest.mark.parametrize("title", ["Greeting card design", "Hi-res chart export", "Fix the model"])
def test_is_generic_thread_name_keeps_real_titles(title: str) -> None:
    assert not is_generic_thread_name(title), f"{title!r} is a real title"


def test_parse_thread_name_hard_cuts_when_no_space_before_cap() -> None:
    title = parse_thread_name("x" * 120)
    assert title == "x" * THREAD_NAME_MAX_CHARS, (
        "a single over-long token has no word boundary, so it is cut at the cap"
    )


# ---------------------------------------------------------------------------
# suggest_thread_name (shell, transport-level fake)
# ---------------------------------------------------------------------------


def _message_payload(text: str, stop_reason: StopReason = "end_turn") -> dict[str, Any]:
    return Message(
        id="msg_thread_name",
        type="message",
        role="assistant",
        model=THREAD_NAMING_MODEL,
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


async def test_suggest_thread_name_wraps_truncated_message_as_data_and_maps_usage() -> None:
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
    assert seen[0]["thinking"] == {"type": "disabled"}, (
        "thinking counts toward the 60-token cap, so it must be off or no title text fits"
    )
    assert seen[0]["messages"][0]["content"] == f"<message>\n{message_text[:40]}\n</message>", (
        "the opening message must be cut at max_input_chars and wrapped in <message> tags, "
        "so the model summarizes it instead of answering it"
    )
    assert "NONE" not in seen[0]["system"] and "Always produce a title" in seen[0]["system"], (
        "the prompt must offer no escape hatch: every message gets a title"
    )


async def test_suggest_thread_name_returns_none_name_but_usage_when_model_answers_blank() -> None:
    suggestion = await suggest_thread_name(
        build_fake_anthropic(lambda _r: httpx.Response(200, json=_message_payload(" \n"))),
        message_text="hey",
        max_input_chars=2000,
    )
    assert suggestion.name is None, "a blank answer must map to no title"
    assert suggestion.usage.output_tokens == 9, (
        "a blank answer still cost tokens; usage must be reported so the caller meters it"
    )


async def test_suggest_thread_name_returns_none_name_but_usage_when_model_refuses() -> None:
    payload = _message_payload("Partial title", stop_reason="refusal")
    suggestion = await suggest_thread_name(
        build_fake_anthropic(lambda _r: httpx.Response(200, json=payload)),
        message_text="hey",
        max_input_chars=2000,
    )
    assert suggestion.name is None, "a refusal must map to no title, even with partial text"
    assert suggestion.usage.output_tokens == 9, "a refused call still cost tokens and is metered"


async def test_suggest_thread_name_propagates_api_errors() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"type": "error", "error": {"message": "bad"}})

    with pytest.raises(BadRequestError):
        await suggest_thread_name(
            build_fake_anthropic(handler), message_text="hello there", max_input_chars=2000
        )
