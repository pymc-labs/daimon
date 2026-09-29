"""Discord's wrapper keeps every chunk under the 2000-char message limit."""

from __future__ import annotations

from daimon.adapters.discord.split import split_for_discord_safe


def test_fence_repaired_chunks_stay_under_discords_limit() -> None:
    text = "```python\n" + "x = 1\n" * 2000 + "```"
    chunks = split_for_discord_safe(text, blockquote=True)
    assert len(chunks) > 1 and all(len(c) <= 2000 for c in chunks)
