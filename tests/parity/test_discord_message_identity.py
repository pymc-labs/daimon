"""Discord parity requests have real, distinct identities across driver instances."""

from .drivers.discord_driver import DiscordDriver


def test_identical_requests_from_separate_drivers_have_distinct_ids() -> None:
    kwargs = dict(workspace_id="123", channel_id="900", user_id="456", text="same request")
    first = DiscordDriver()._make_message(**kwargs)
    second = DiscordDriver()._make_message(**kwargs)

    assert isinstance(first.id, int)
    assert isinstance(second.id, int)
    assert first.id != second.id
    assert first.content == second.content
    assert first.channel.id == second.channel.id == 900
