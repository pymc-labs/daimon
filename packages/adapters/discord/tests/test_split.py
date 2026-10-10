"""Discord's wrapper keeps every chunk under the 2000-char message limit."""

from __future__ import annotations

from daimon.adapters.discord.split import split_discord_answer, split_for_discord_safe


def test_fence_repaired_chunks_stay_under_discords_limit() -> None:
    text = "```python\n" + "x = 1\n" * 2000 + "```"
    chunks = split_for_discord_safe(text, blockquote=True)
    assert len(chunks) > 1 and all(len(c) <= 2000 for c in chunks)


def test_numbered_answer_keeps_code_fences_balanced() -> None:
    chunks = split_discord_answer("```python\n" + "x = 1\n" * 2000 + "```")
    assert len(chunks) > 1
    for index, chunk in enumerate(chunks, start=1):
        marker, code = chunk.split("\n", 1)
        assert marker == f"({index}/{len(chunks)})"
        assert len(chunk) <= 1900
        assert code.startswith("```python\n")
        assert code.endswith("```")


def test_short_answer_has_no_continuation_label() -> None:
    assert split_discord_answer("The result is ready.") == ["The result is ready."]
