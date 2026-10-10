"""Plain wording and spacing in the Teams channel settings card."""

from daimon.adapters.teams.channel_settings_card import ChannelSettings, channel_settings_form
from microsoft_teams.cards import TextBlock


def test_channel_admin_explainer_and_id_hint_are_spaced_blocks() -> None:
    card = channel_settings_form(
        ChannelSettings(
            channel_id="analytics",
            label="#analytics",
            picker=None,
            rule=None,
            admin_user_ids=(),
            skills=(),
        )
    )
    blocks = [item for item in card.body if isinstance(item, TextBlock)]
    texts = [item.text for item in blocks]
    assert (
        "Channel admins choose their channels' environment and agent, and edit agents "
        "limited to those channels.\n\n"
        "Starting agents and the server default stay with server admins."
    ) in texts
    assert (
        "Enter Entra object IDs, separated by commas.\n\nLeave it empty to remove them."
    ) in texts
    assert (
        "These skills apply only here, from the next message.\n\nOnly server admins can change them."
        in texts
    )
    hint = next(item for item in blocks if item.text.startswith("Enter Entra object IDs"))
    assert hint.spacing == "Medium"
