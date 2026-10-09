"""Rules shared by the Discord bot and MCP process."""

from daimon.core.agent_post_identity import (
    discord_username,
    fallback_name_prefix,
    is_our_discord_webhook,
    select_discord_webhook_id,
)


def test_discord_username_removes_reserved_words_and_fences() -> None:
    name = discord_username("  Clyde Discord ``` Helper  ")
    assert name == "Helper"
    assert len(discord_username("x" * 100)) == 80
    assert discord_username("discord") == "Agent"


def test_fallback_prefix_is_one_subtext_line_above_the_chunk() -> None:
    assert fallback_name_prefix("Research", "First answer") == "-# Research\nFirst answer"
    assert fallback_name_prefix("Research", "") == "-# Research\n"


def test_fallback_prefix_escapes_markdown_mentions_and_backticks() -> None:
    assert fallback_name_prefix("A* @everyone", "x") == "-# A\\* @\u200beveryone\nx"
    assert fallback_name_prefix("ag_acme `x` ||y||", "x") == "-# ag\\_acme \\`x\\` \\|\\|y\\|\\|\nx"
    assert fallback_name_prefix("back\\slash ~~s~~", "x") == "-# back\\\\slash \\~\\~s\\~\\~\nx"


def test_fallback_prefix_keeps_the_name_on_one_line() -> None:
    assert fallback_name_prefix("two\nlines", "x") == "-# two lines\nx"


def test_webhook_must_match_application_and_channel() -> None:
    match = dict(our_application_id=10, target_channel_id=20)
    assert is_our_discord_webhook(application_id=10, channel_id=20, **match)
    assert not is_our_discord_webhook(application_id=11, channel_id=20, **match)
    assert not is_our_discord_webhook(application_id=10, channel_id=21, **match)


def test_thread_webhook_selection_is_stable_over_sorted_ids() -> None:
    assert select_discord_webhook_id([33, 31, 32], 4) == 32
    assert select_discord_webhook_id([33, 31], 4) == 31
    assert select_discord_webhook_id([], 4) is None
