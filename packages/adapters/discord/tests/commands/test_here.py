"""Discord /here renders the shared facts as an ephemeral embed."""

from __future__ import annotations

import pytest
from daimon.adapters.discord.commands.here import build_here_embed
from daimon.core.access_policy import ChannelRule
from daimon.core.here_card import CredentialStatus, HereCard

CREDENTIALS = (
    CredentialStatus(name="OPENAI_API_KEY", kind="agent key", configured=True),
    CredentialStatus(name="sk-live-fake-value", kind="MCP token", configured=True),
    CredentialStatus(
        name="GitHub (https://github.com/acme/private-repo)",
        kind="GitHub installation token",
        configured=True,
    ),
)
ANSWERING = {"blocked", "channel", "thread"}


def _card(state: str) -> HereCard:
    return HereCard(
        channel_rule=ChannelRule(),
        who_may_answer="",
        reads_kept_inside=False,
        agent_name=None if state == "no_agent" else "ResearchBot",
        tier="thread" if state == "thread" else "channel",
        bot_can_view=state != "no_view",
        effective_writers="none" if state == "no_replies" else "any",
        agent_can_answer_here=state != "blocked",
        effective_readers="any",
        publishing_needs_approval=False,
        bot_can_read_history=True,
        credentials=CREDENTIALS,
        text="",
    )


@pytest.mark.parametrize(
    ("state", "title", "colour", "subline"),
    [
        ("no_view", "No channel access", 0xED4245, "Ask an admin to check Daimon's access."),
        ("no_replies", "Replies disabled here", 0xED4245, None),
        ("no_agent", "No agent selected", 0x95A5A6, "Ask an admin: /agent-setup"),
        ("blocked", "ResearchBot can't answer here", 0xF0B429, None),
        ("channel", "ResearchBot answers here", 0x2ECC71, None),
        ("thread", "ResearchBot answers in this thread", 0x2ECC71, None),
    ],
)
def test_discord_embed_states(state: str, title: str, colour: int, subline: str | None) -> None:
    embed = build_here_embed(_card(state)).to_dict()
    assert embed["title"] == title
    assert embed["color"] == colour
    assert embed.get("description") == subline
    assert embed.get("fields", []) == (
        [
            {"name": "Reading", "value": "Any conversation", "inline": True},
            {"name": "Publishing", "value": "No approval", "inline": True},
        ]
        if state in ANSWERING
        else []
    )
    assert "footer" not in embed
    for credential in CREDENTIALS:
        assert credential.name not in str(embed)
        assert credential.kind not in str(embed)


def test_discord_embed_extra_lines_and_no_credentials() -> None:
    card = _card("channel").model_copy(
        update={"effective_writers": "own", "bot_can_read_history": False}
    )
    embed = build_here_embed(card).to_dict()
    assert embed["fields"][2] == {
        "name": "\u200b",
        "value": "Only own agents answer here\nNo access to earlier messages",
        "inline": False,
    }
    for credential in CREDENTIALS:
        assert credential.name not in str(embed)


def test_discord_unknown_publishing_and_history_drop_their_fields() -> None:
    card = _card("channel").model_copy(
        update={"publishing_needs_approval": None, "bot_can_read_history": None}
    )
    embed = build_here_embed(card).to_dict()
    assert embed["fields"] == [{"name": "Reading", "value": "Any conversation", "inline": True}]
