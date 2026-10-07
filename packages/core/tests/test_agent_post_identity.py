"""Rules shared by the Discord bot and MCP process."""

from daimon.core.agent_post_identity import (
    discord_username,
    fallback_name_prefix,
    is_our_discord_webhook,
)


def test_discord_username_removes_reserved_words_and_fences() -> None:
    name = discord_username("  Clyde Discord ``` Helper  ")
    assert name == "Helper"
    assert len(discord_username("x" * 100)) == 80
    assert discord_username("discord") == "Agent"


def test_fallback_prefix_labels_only_the_passed_chunk() -> None:
    assert fallback_name_prefix("Research", "First answer") == "**Research** First answer"
    assert fallback_name_prefix("A* @everyone", "x") == "**A\\* @\u200beveryone** x"


def test_webhook_must_match_application_and_channel() -> None:
    match = dict(our_application_id=10, target_channel_id=20)
    assert is_our_discord_webhook(application_id=10, channel_id=20, **match)
    assert not is_our_discord_webhook(application_id=11, channel_id=20, **match)
    assert not is_our_discord_webhook(application_id=10, channel_id=21, **match)
