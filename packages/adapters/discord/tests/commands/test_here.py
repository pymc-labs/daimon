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
        (
            "no_view",
            "Daimon can't see this channel",
            0xED4245,
            "A server admin can let Daimon's role see it.",
        ),
        (
            "no_replies",
            "Replies are off here",
            0xED4245,
            "An admin can turn them on in /agent-setup.",
        ),
        ("no_agent", "No agent answers here", 0x95A5A6, "An admin can pick one in /agent-setup."),
        (
            "blocked",
            "ResearchBot can't answer here",
            0xF0B429,
            "An admin can change where it answers in /agent-setup.",
        ),
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


@pytest.mark.parametrize("target", ["channel", "thread"])
async def test_approval_card_checks_live_admin_before_write(
    monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    import uuid
    from unittest.mock import AsyncMock, MagicMock

    from daimon.adapters.discord.commands import here
    from daimon.core.authz import Subject

    tenant_id = uuid.uuid4()
    runtime = MagicMock()
    runtime.sessionmaker = MagicMock()
    monkeypatch.setattr(here, "resolve_tenant_for_interaction", AsyncMock(return_value=tenant_id))
    monkeypatch.setattr(
        here, "approval_admin", AsyncMock(return_value=Subject(platform_user_id="requester"))
    )
    interaction = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    interaction.data = {"values": ["off"]}
    view = here.ApprovalGateView(runtime, tenant_id, target)
    await view.change(interaction)
    runtime.sessionmaker.assert_not_called()
    assert "admin" in interaction.followup.send.await_args.args[0]
