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


@pytest.mark.parametrize("readers", ["any", "inside", "own"])
def test_outsider_warning_on_here(readers: str) -> None:
    card = HereCard(
        agent_name=None,
        tier=None,
        channel_rule=ChannelRule(),
        effective_readers=readers,
        effective_writers="any",
        who_may_answer="",
        reads_kept_inside=False,
        outsiders_can_view=True,
        text="",
    )
    assert ("outside your organisation" in str(build_here_embed(card).to_dict())) is (
        readers != "own"
    )


def test_unconfigured_internal_roles_hint_on_here() -> None:
    from daimon.core.outsiders import INTERNAL_ROLES_HINT

    card = HereCard(
        agent_name=None,
        tier=None,
        channel_rule=ChannelRule(),
        effective_readers="any",
        effective_writers="any",
        who_may_answer="",
        reads_kept_inside=False,
        outsider_note=INTERNAL_ROLES_HINT,
        text="",
    )
    assert INTERNAL_ROLES_HINT in str(build_here_embed(card).to_dict())


@pytest.mark.parametrize("target", ["server", "channel", "thread"])
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
    interaction.data = {"values": [f"{target}:off"]}
    view = here.ApprovalGateView(runtime, tenant_id, "channel", "thread")
    await view.change(interaction)
    runtime.sessionmaker.assert_not_called()
    assert "admin" in interaction.followup.send.await_args.args[0]


@pytest.mark.parametrize("scope", ["server", "channel", "thread"])
@pytest.mark.parametrize("value", ["on", "off", "inherit"])
async def test_native_admin_can_set_every_approval_scope(
    db_session_factory,
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
    value: str,
) -> None:
    from unittest.mock import AsyncMock, MagicMock

    from daimon.adapters.discord.commands import here
    from daimon.core.authz import Subject
    from daimon.core.stores.access_policy import load_access_policy
    from daimon.core.stores.security_audit import list_events
    from daimon.core.tool_safety import ToolSafetyPolicy
    from daimon.testing.factories import make_tenant

    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session)
    runtime = MagicMock()
    runtime.sessionmaker = db_session_factory
    runtime.settings.tool_safety = ToolSafetyPolicy(enabled=True, allow_gate_overrides=True)
    fresh = AsyncMock(return_value=Subject(is_admin=True, platform_user_id="native-admin"))
    monkeypatch.setattr(here, "approval_admin", fresh)
    monkeypatch.setattr(here, "resolve_tenant_for_interaction", AsyncMock(return_value=tenant.id))
    interaction = MagicMock()
    interaction.user.id = "native-admin"
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    interaction.data = {"values": [f"{scope}:{value}"]}
    view = here.ApprovalGateView(runtime, tenant.id, "channel", "thread")
    await view.change(interaction)
    async with db_session_factory() as session:
        policy = await load_access_policy(session, tenant_id=tenant.id)
        events = await list_events(session, tenant_id=tenant.id)
    target = {"server": None, "channel": "channel", "thread": "thread"}[scope]
    stored = (
        policy.approvals
        if target is None
        else policy.channel_rules.get(target, ChannelRule()).approvals
    )
    assert stored == value
    assert len(events) == 1 and events[0].platform_user_id == "native-admin"
    fresh.assert_awaited_once()


async def test_approval_controls_preserve_confidential_action_and_its_admin_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import uuid
    from unittest.mock import AsyncMock, MagicMock

    import discord
    from daimon.adapters.discord import confidential
    from daimon.adapters.discord.commands.here import ApprovalGateView

    refuse = AsyncMock(return_value=True)
    monkeypatch.setattr(confidential, "refuse_if_not_admin", refuse)
    runtime = MagicMock()
    view = ApprovalGateView(runtime, uuid.uuid4(), "123", "456", include_confidential=True)
    assert len(view.children) == 2
    assert isinstance(view.children[0], discord.ui.Select)
    button = view.children[1]
    assert isinstance(button, discord.ui.Button)
    assert button.label == "Make confidential"
    interaction = MagicMock()
    await button.callback(interaction)
    refuse.assert_awaited_once_with(interaction)
    runtime.sessionmaker.assert_not_called()
