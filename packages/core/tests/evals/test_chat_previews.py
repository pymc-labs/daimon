"""An answer is previewed in each platform's markdown dialect and message size."""

from __future__ import annotations

from daimon.core.evals.chat.previews import DISCORD_LIMIT, message_payloads

_ANSWER = "It costs $5 and $10: a < b & <@U1> `<raw>`"


def _messages(platform: str, text: str = _ANSWER) -> list[str]:
    payload = getattr(message_payloads(text), platform)
    return payload["messages"]


def test_discord_when_answer_is_long_then_chunks_fit_and_fences_are_repaired() -> None:
    chunks = _messages("discord", "```py\n" + "x = 1\n" * 600 + "```")

    assert len(chunks) > 1
    assert all(len(c) <= DISCORD_LIMIT for c in chunks)
    assert all(c.count("```") % 2 == 0 for c in chunks)


def test_discord_when_answer_has_angle_brackets_then_markdown_is_left_as_written() -> None:
    assert _messages("discord") == [_ANSWER]


def test_slack_when_answer_has_entities_then_prose_is_escaped_and_code_and_mentions_are_not() -> (
    None
):
    (chunk,) = _messages("slack")

    assert chunk == "It costs $5 and $10: a &lt; b &amp; <@U1> `<raw>`"
    assert message_payloads(_ANSWER).slack["blocks"] == [{"type": "markdown", "text": chunk}]  # type: ignore[index]


def test_teams_when_answer_has_dollar_signs_then_they_are_escaped_for_extended_markdown() -> None:
    assert _messages("teams") == ["It costs \\$5 and \\$10: a < b & <@U1> `<raw>`"]
    assert message_payloads(_ANSWER).teams["text_format"] == "extendedmarkdown"  # type: ignore[index]
