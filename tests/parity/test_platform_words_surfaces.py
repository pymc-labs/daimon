"""No platform's card, panel, notice or refusal uses another platform's words.

`test_platform_words_refusals.py` guards the admission refusals, termination
notices and the here card. This renders the rest of the person-facing copy that
core builds for the adapters (the setup opener, routing notes, environment
notes, the shared refusals, the billing line, the button labels) and the
adapters' own fixed notices, per platform, against the same kind of forbidden
words, plus the plain-word swaps (`fork`, `turns`, `in flight`, `MCP token`).
"""

from __future__ import annotations

from typing import get_args

import pytest
from daimon.adapters.discord.agent_setup.routing_view import PRECEDENCE_LINE as DISCORD_PRECEDENCE
from daimon.adapters.discord.bot import TENANT_CAP_NOTICE as DISCORD_CAP_NOTICE
from daimon.adapters.discord.wizard_submit import (
    _OVER_CAP as DISCORD_WIZARD_CAP_NOTICE,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.slack.app import TENANT_CAP_NOTICE as SLACK_CAP_NOTICE
from daimon.adapters.teams.app import (
    _SHED as TEAMS_CAP_NOTICE,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core import channel_environments as environments
from daimon.core import routing_facts
from daimon.core.agent_pins import PIN_WRITE_REFUSAL, POLICY_UNREADABLE_REFUSAL
from daimon.core.billing_panel import turns_phrase
from daimon.core.channel_copies import ArchiveRefusal, ChannelCopyArchiveRefused
from daimon.core.channel_rules import RuleRefusal, render_rule_refusal
from daimon.core.channel_skills import REFUSALS as CHANNEL_SKILL_REFUSALS
from daimon.core.continuity.handoff import HandoffRefusalReason, HandoffRefused
from daimon.core.credential_requests import CredentialRequestKind, build_button_label
from daimon.core.github_panel import safe_github_error
from daimon.core.platform_words import NEUTRAL_ADMIN, NEUTRAL_SPACE, space_for, words_for
from daimon.core.posted_controls import WRONG_REQUESTER_MESSAGE
from daimon.core.setup_conversations import build_setup_opener
from daimon.core.stores.domain import ChatPlatform
from daimon.core.stores.github_access import SERVER_WIDE_GRANT_MESSAGE
from daimon.core.stores.github_connect import CHANNEL_LIMITED_REPO_MESSAGE
from daimon.core.thread_handoff import render_handoff_refused
from daimon.core.thread_participation import (
    ParticipationMode,
    ParticipationScope,
    ResolvedParticipation,
    build_participation_note,
)

_PLATFORMS: tuple[ChatPlatform, ...] = get_args(ChatPlatform)

#: Words that belong to one platform, so no other platform may show them.
_FOREIGN: dict[ChatPlatform, tuple[str, ...]] = {
    "teams": ("server", "workspace", "DM", "Manage Server", "slash", "guild"),
    "slack": ("server", "Manage Server", "guild", "organisation"),
    "discord": ("workspace", "organisation", "guild"),
}

#: Plain-word swaps no platform's copy may undo.
_JARGON = ("fork", "in flight", "MCP token")

#: Copy built without a platform: it must name no platform's own space word.
_NEUTRAL_FOREIGN = ("server", "workspace", "guild", "Manage Server", "organisation")


def _assert_native(platform: ChatPlatform, text: str) -> None:
    for word in (*_FOREIGN[platform], *_JARGON):
        assert word not in text, f"{platform} text shows {word!r}: {text!r}"


def _assert_neutral(text: str) -> None:
    for word in (*_NEUTRAL_FOREIGN, *_JARGON):
        assert word not in text, f"copy with no platform shows {word!r}: {text!r}"


@pytest.mark.parametrize("platform", _PLATFORMS)
@pytest.mark.parametrize("is_admin", [True, False])
def test_the_setup_opener_uses_the_platforms_words(platform: ChatPlatform, is_admin: bool) -> None:
    words = words_for(platform)
    opener = build_setup_opener(
        target_display="Daimon",
        bot_mention=None if platform == "teams" else "@daimon",
        is_admin=is_admin,
        admin_noun=words.admin,
        conversation_noun="chat" if platform == "teams" else "thread",
    )
    _assert_native(platform, opener)
    if is_admin:
        assert "make a copy and change the copy" in opener
    if platform == "teams":
        assert "thread" not in opener, "a Teams 1:1 chat has no threads"


@pytest.mark.parametrize("platform", _PLATFORMS)
def test_the_routing_notes_name_the_platforms_space(platform: ChatPlatform) -> None:
    space = words_for(platform).space
    texts = (
        routing_facts.precedence_line(space),
        routing_facts.build_set_default_note(agent_name="writer", scope_label=space, space=space),
        routing_facts.build_clear_default_note(scope_label=space, cleared=True, space=space),
        *(
            routing_facts.build_resolution_note(
                agent_name=agent, tier=tier, channel_id="C1", space=space
            )
            for agent in ("writer", None)
            for tier in ("channel", "tenant", "deployment")
        ),
    )
    for text in texts:
        _assert_native(platform, text)
        assert space in text or "default" not in text, text


def test_discord_calls_its_default_the_server_default() -> None:
    assert DISCORD_PRECEDENCE == "A channel's own choice beats the server default."


@pytest.mark.parametrize("platform", _PLATFORMS)
def test_the_environment_notes_use_the_platforms_words(platform: ChatPlatform) -> None:
    words = words_for(platform)
    for text in (
        environments.build_set_environment_note(
            environment_name="gpu", channel=None, space=words.space
        ),
        environments.build_clear_environment_note(channel=None, cleared=True, space=words.space),
        environments.build_clear_environment_note(channel=None, cleared=False, space=words.space),
        environments.build_archive_environment_note(
            environment_name="gpu", cleared=0, space=words.space
        ),
        environments.build_limited_network_refusal(environment_name="gpu", admin=words.admin),
        environments.build_limited_network_refusal(environment_name=None, admin=words.admin),
        *(
            environments.build_environment_resolution_note(
                environment_name=name,
                tier=tier,
                channel_id="C1",
                space=words.space,
                admin=words.admin,
            )
            for name in ("gpu", None)
            for tier in ("tenant", "deployment")
        ),
    ):
        _assert_native(platform, text)


@pytest.mark.parametrize("platform", _PLATFORMS)
def test_the_participation_notes_name_the_platforms_space(platform: ChatPlatform) -> None:
    space = words_for(platform).space
    note = build_participation_note(
        scope=ParticipationScope.WORKSPACE,
        scope_id=None,
        requested=ParticipationMode.ON,
        effective=ResolvedParticipation(mode=ParticipationMode.ON, tier="workspace"),
        platform=platform,
    )
    _assert_native(platform, note)
    assert f"the whole {space}" in note


def test_the_unknown_caller_gets_the_neutral_space() -> None:
    assert space_for(None) == NEUTRAL_SPACE
    assert space_for("cli") == NEUTRAL_SPACE
    assert [space_for(platform) for platform in _PLATFORMS] == [
        words_for(platform).space for platform in _PLATFORMS
    ]
    assert (
        environments.build_limited_network_refusal(environment_name=None).count(NEUTRAL_ADMIN) == 2
    )


def test_the_shared_core_copy_names_no_platforms_space() -> None:
    texts: list[str] = [
        PIN_WRITE_REFUSAL,
        POLICY_UNREADABLE_REFUSAL,
        SERVER_WIDE_GRANT_MESSAGE,
        CHANNEL_LIMITED_REPO_MESSAGE,
        WRONG_REQUESTER_MESSAGE,
        environments.LIMITED_OPEN_NETWORK_WARNING,
        environments.LIMITED_NETWORK_UNCHECKED_WARNING,
        *CHANNEL_SKILL_REFUSALS.values(),
        *(str(ChannelCopyArchiveRefused(reason)) for reason in get_args(ArchiveRefusal)),
        *(
            render_rule_refusal(reason, agent_name="writer", channel_id="C1")
            for reason in get_args(RuleRefusal)
        ),
        *(
            render_handoff_refused(
                HandoffRefused(reason=reason, destination_name="writer"), channel="#general"
            )
            for reason in get_args(HandoffRefusalReason)
        ),
        safe_github_error(ValueError("Only an admin can connect GitHub.")),
    ]
    for text in texts:
        _assert_neutral(text)
    assert "thread" not in WRONG_REQUESTER_MESSAGE, "a Teams 1:1 chat has no threads"


def test_the_billing_line_says_answers_not_turns() -> None:
    assert turns_phrase(1250) == "about 1,250 answers"


@pytest.mark.parametrize("kind", get_args(CredentialRequestKind))
def test_the_credential_buttons_say_connection_token_not_mcp(kind: CredentialRequestKind) -> None:
    label = build_button_label(kind, "linear")
    assert "MCP" not in label, label
    if kind == "mcp":
        assert label == "Add connection token: linear"


@pytest.mark.parametrize(
    ("platform", "text"),
    [
        ("discord", DISCORD_CAP_NOTICE),
        ("discord", DISCORD_WIZARD_CAP_NOTICE),
        ("slack", SLACK_CAP_NOTICE),
        ("teams", TEAMS_CAP_NOTICE),
    ],
)
def test_the_chat_cap_notices_say_chats_at_once(platform: ChatPlatform, text: str) -> None:
    _assert_native(platform, text)
    assert "chats at once" in text, text
