"""Each chat platform's session runs a system prompt that names no other platform.

The shipped prompt covers all three, so every session runs its platform's variant,
sent as the session's own override. A prompt that drifted or was edited still never
shows another platform's name: the variant drops the lines that do.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import structlog
import yaml
from daimon.core.agent_guidance import apply_credential_guidance
from daimon.core.context_prompt import (
    DEFAULT_FRAGMENTS,
    FRAGMENT_MARKER,
    ContextFragment,
    TurnContext,
    context_prompt,
    encode_fragments,
)
from daimon.core.platform_system import SHIPPED_PASSAGES, system_for_platform
from daimon.core.session_snapshot import hash_system

_DEFAULTS = Path(__file__).resolve().parents[3] / "defaults"
_PLATFORMS = ("discord", "slack", "teams")


def _others(platform: str) -> re.Pattern[str]:
    return re.compile("|".join(p for p in _PLATFORMS if p != platform), re.IGNORECASE)


def _shipped_system() -> str:
    spec = yaml.safe_load((_DEFAULTS / "agents" / "daimon.yaml").read_text())
    return apply_credential_guidance(spec["system"])


def test_every_shipped_passage_is_in_the_shipped_prompt() -> None:
    system = _shipped_system()

    for shipped in SHIPPED_PASSAGES:
        assert shipped in system, f"the shipped prompt no longer carries: {shipped[:60]!r}"


@pytest.mark.parametrize("platform", _PLATFORMS)
def test_a_platforms_prompt_is_its_own_variant_of_the_shipped_one(platform: str) -> None:
    system = _shipped_system()
    variant = system_for_platform(system, platform)

    assert variant is not None
    assert hash_system(variant) != hash_system(system)
    assert _others(platform).search(variant) is None, _others(platform).search(variant)
    assert platform in variant.lower(), "its own formatting rules stay"


def test_each_platform_gets_only_its_own_formatting_and_file_rules() -> None:
    system = _shipped_system()
    discord, slack, teams = (system_for_platform(system, p) for p in _PLATFORMS)

    assert discord and slack and teams
    assert "# / ## / ###" in discord and "mrkdwn" not in discord
    assert "mrkdwn" in slack and "# / ## / ###" not in slack
    assert "Tables do\nnot render" in teams and "mrkdwn" not in teams
    assert "images unless the tenant opts out" in discord
    assert "native tables unless the tenant opts out" in slack
    # The `files` attribute is Teams-only, so only Teams is told of it.
    assert "`files` attribute" in teams
    assert "`files` attribute" not in discord and "`files` attribute" not in slack
    for variant in (discord, slack, teams):
        assert "every chat platform" not in variant and "assume a platform" not in variant


def test_the_teams_prompt_defers_to_the_files_hint_for_a_person_with_no_one_to_one_chat() -> None:
    teams = system_for_platform(_shipped_system(), "teams")

    assert teams is not None
    assert "suggest a 1:1 chat\nwith the bot, a separate conversation where the file would be " in (
        teams
    )
    assert "again, unless the files hint says this person can't open one." in teams


#: What Discord's and Slack's prompts hash to. A new hash replaces their sessions, so
#: it moves only with a deliberate change to the shipped prompt.
_PINNED_ON_MAIN = {
    "discord": "992ba978cd97139cde0998fa31b2ee728a0702ee13aaa0bdae2b30d60074a7fe",
    "slack": "4604e1218ef98bd38b41504f3be4e7c05b2e34452b9cdff12f7ea149d00aa0be",
}


@pytest.mark.parametrize("platform", sorted(_PINNED_ON_MAIN))
def test_the_discord_and_slack_prompts_are_byte_identical_to_main(platform: str) -> None:
    variant = system_for_platform(_shipped_system(), platform)

    assert hash_system(variant) == _PINNED_ON_MAIN[platform]


@pytest.mark.parametrize("platform", [None, "cli", ""])
def test_a_prompt_without_a_chat_platform_stays_the_agents_own(platform: str | None) -> None:
    assert system_for_platform(_shipped_system(), platform) is None


@pytest.mark.parametrize("platform", _PLATFORMS)
def test_a_drifted_formatting_passage_still_names_no_other_platform(platform: str) -> None:
    system = _shipped_system()
    drifted = system.replace("a CommonMark subset", "a CommonMark subsets", 1)
    assert drifted != system

    with structlog.testing.capture_logs() as logs:
        variant = system_for_platform(drifted, platform)

    assert variant is not None
    assert _others(platform).search(variant) is None
    assert [log["platform"] for log in logs if log["event"] == "system_prompt.lines_dropped"], (
        "the lines it dropped are logged"
    )
    for entry in logs:
        assert set(entry) <= {"event", "log_level", "platform", "count"}, "a count, never text"


@pytest.mark.parametrize("platform", _PLATFORMS)
def test_a_custom_line_naming_another_platform_is_dropped_and_nothing_else(platform: str) -> None:
    other = next(p for p in _PLATFORMS if p != platform)
    own = platform.capitalize()
    custom = f"You are a research agent.\nAnswer {other.capitalize()} questions first.\nBe brief on {own}.\n"

    variant = system_for_platform(custom, platform)

    assert variant == f"You are a research agent.\nBe brief on {own}.\n"


@pytest.mark.parametrize("platform", _PLATFORMS)
def test_a_custom_prompt_naming_no_other_platform_stays_the_agents_own(platform: str) -> None:
    assert system_for_platform("You are a custom agent.\nBe brief.", platform) is None
    assert system_for_platform(None, platform) is None


_PLACES = {
    "discord": "In Discord, Daimon works in other servers, channels, threads and DMs.",
    "slack": "In Slack, Daimon works in other channels, DMs and workspaces it's installed in.",
    "teams": (
        "In Teams, Daimon works wherever it has been added: other teams, channels, "
        "group chats and 1:1 chats."
    ),
}


@pytest.mark.parametrize("platform", _PLATFORMS)
def test_each_variant_carries_the_rule_with_only_its_own_places(platform: str) -> None:
    variant = system_for_platform(_shipped_system(), platform)

    assert variant is not None
    assert "Never name or hint at another chat app, and never say Daimon works in one." in variant
    assert _PLACES[platform] in variant
    assert "only place" not in variant.lower()
    for other in _PLATFORMS:
        if other != platform:
            assert _PLACES[other] not in variant


@pytest.mark.parametrize("platform", _PLATFORMS)
def test_tenant_context_fragments_survive_whatever_they_name(platform: str) -> None:
    """The fragment JSON is one line after the marker; dropping it for naming a platform
    would silently lose every fragment on that platform."""
    other = next(p for p in _PLATFORMS if p != platform).capitalize()
    fragments: dict[TurnContext, ContextFragment] = {
        "routine": ContextFragment(text=f"Post the summary to {other} first.", mode="extend"),
        "handoff": ContextFragment(text="Resume from the notes.", mode="replace"),
    }
    system = encode_fragments(_shipped_system(), fragments)

    variant = system_for_platform(system, platform)

    assert variant is not None
    assert variant.endswith(
        system.partition(FRAGMENT_MARKER)[1] + system.partition(FRAGMENT_MARKER)[2]
    )
    assert _others(platform).search(variant.partition(FRAGMENT_MARKER)[0]) is None
    routine = context_prompt("routine", system=variant)
    assert f"Post the summary to {other} first." in routine
    assert DEFAULT_FRAGMENTS["routine"] in routine, "an extend fragment keeps the default"
    assert "Resume from the notes." in context_prompt("handoff", system=variant)
    assert context_prompt("routine", system=variant) == context_prompt("routine", system=system)
