"""Tests for code-fence-aware Discord message splitting."""

from __future__ import annotations

from daimon.adapters.discord.split import split_for_discord_safe


class TestSplitForDiscordSafe:
    def test_short_text_returns_single_element(self) -> None:
        result = split_for_discord_safe("hello world")
        assert result == ["hello world"], "text under limit should return as-is in a list"

    def test_exactly_at_limit_returns_single_element(self) -> None:
        text = "a" * 1900
        result = split_for_discord_safe(text)
        assert result == [text], "text exactly at limit should not be split"

    def test_splits_at_paragraph_boundary(self) -> None:
        para1 = "a" * 1000
        para2 = "b" * 1000
        text = f"{para1}\n\n{para2}"
        result = split_for_discord_safe(text)
        assert len(result) == 2, "should split into two chunks at paragraph boundary"
        assert result[0] == para1, "first chunk should be the first paragraph"
        assert result[1] == para2, "second chunk should be the second paragraph"

    def test_splits_at_line_boundary_when_no_paragraph_break(self) -> None:
        line1 = "a" * 1000
        line2 = "b" * 1000
        text = f"{line1}\n{line2}"
        result = split_for_discord_safe(text)
        assert len(result) == 2, "should split into two chunks at line boundary"
        assert result[0] == line1, "first chunk should be the first line"
        assert result[1] == line2, "second chunk should be the second line"

    def test_hard_cut_when_no_breaks(self) -> None:
        text = "a" * 3800
        result = split_for_discord_safe(text)
        assert len(result) == 2, "should hard-cut into two chunks"
        assert result[0] == "a" * 1900, "first chunk should be exactly limit chars"
        assert result[1] == "a" * 1900, "second chunk should be the remainder"

    def test_code_fence_repair_across_split(self) -> None:
        code = "x\n" * 950
        text = f"```python\n{code}```"
        result = split_for_discord_safe(text)
        assert len(result) >= 2, "long fenced block should split"
        assert result[0].endswith("```"), "first chunk should close the fence"
        assert result[1].startswith("```python"), "second chunk should re-open with language"

    def test_blockquote_mode(self) -> None:
        code = "x\n" * 950
        text = f"```python\n{code}```"
        result = split_for_discord_safe(text, blockquote=True)
        assert len(result) >= 2, "long fenced block should split in blockquote mode"
        assert result[0].endswith("> ```"), "first chunk should close fence with blockquote prefix"
        assert result[1].startswith("> ```python"), (
            "second chunk should re-open fence with blockquote prefix"
        )

    def test_multiple_chunks_for_very_long_text(self) -> None:
        text = "a" * 5700
        result = split_for_discord_safe(text)
        assert len(result) == 3, "5700 chars should produce 3 chunks at 1900 limit"
