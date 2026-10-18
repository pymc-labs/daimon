"""The registration template validates against the published Teams v1.30 schema."""

import json
from pathlib import Path
from typing import Any

import yaml
from daimon.adapters.teams.commands import parse_command
from daimon.adapters.teams.help import COMMAND_HELP
from daimon.adapters.teams.private_replies import IN_THREAD
from jsonschema import Draft4Validator

REPO_ROOT = Path(__file__).resolve().parents[4]
# Vendored for an offline test from
# https://developer.microsoft.com/json-schemas/teams/v1.30/MicrosoftTeams.schema.json
SCHEMA = Path(__file__).parent / "data/teams-manifest-v1.30.schema.json"
# What an operator substitutes before zipping the package.
PLACEHOLDERS = {
    "${DAIMON_TEAMS__CLIENT_ID}": "00000000-0000-4000-8000-000000000001",
    "DAIMON_HOST": "daimon.example.com",
}


def _manifest() -> Any:
    text = (REPO_ROOT / "docs/teams-app-manifest.yaml").read_text()
    for placeholder, value in PLACEHOLDERS.items():
        text = text.replace(placeholder, value)
    return yaml.safe_load(text)


def test_manifest_validates_against_the_v130_schema() -> None:
    manifest = _manifest()
    validator = Draft4Validator(
        json.loads(SCHEMA.read_text()), format_checker=Draft4Validator.FORMAT_CHECKER
    )
    errors = [
        f"{'/'.join(map(str, e.absolute_path))}: {e.message}"
        for e in validator.iter_errors(manifest)
    ]
    assert errors == [], "the template must upload as is"
    assert manifest["manifestVersion"] == "1.30", "1.30 has targeted messages and prompts"


def test_manifest_can_be_added_to_private_and_shared_channels() -> None:
    """`supportedChannelTypes` blocks the upload from 1.25 on; the tier flag replaces it."""
    manifest = _manifest()
    assert manifest["supportsChannelFeatures"] == "tier1", "private and shared channels"
    assert "supportedChannelTypes" not in manifest, "rejected by Teams in manifest 1.25+"


def _lists() -> dict[tuple[str, str], list[dict[str, str]]]:
    """Each command list by (scope, trigger)."""
    [bot] = _manifest()["bots"]
    return {
        (scope, trigger): entry["commands"]
        for entry in bot["commandLists"]
        for scope in entry["scopes"]
        for trigger in entry.get("triggers", ["mention"])
    }


def _basic(commands: list[dict[str, str]]) -> set[str]:
    return {c["title"] for c in commands if c.get("type", "basic") == "basic"}


def test_manifest_offers_what_the_adapter_answers() -> None:
    [bot] = _manifest()["bots"]
    assert bot["scopes"] == ["personal", "team", "groupChat"]
    assert bot["supportsFiles"] is True
    assert bot["supportsTargetedMessages"] is True, "slash commands need targeted messages"
    lists = _lists()
    assert _basic(lists[("personal", "mention")]) == set(COMMAND_HELP), "every command in 1:1"
    slash = _basic(lists[("team", "slash")])
    assert slash == IN_THREAD, "a channel's slash menu offers what is answered privately there"
    assert _basic(lists[("groupChat", "slash")]) == slash, "a group chat's too"
    for title in slash:
        assert parse_command(title, dict.fromkeys(COMMAND_HELP, _noop)) == (title, ""), title


async def _noop(_: object) -> None:
    return None


def test_manifest_prompt_starters_are_ready_to_send() -> None:
    """Teams sends a prompt as typed, so each must read as a request, never a command word."""
    lists = _lists()
    for key in (("personal", "mention"), ("team", "mention")):
        prompts = [c for c in lists[key] if c.get("type") == "prompt"]
        assert len(prompts) >= 3 and len(lists[key]) <= 12, f"{key}: Teams shows at most 12"
        for prompt in prompts:
            text = prompt["prompt"]
            assert len(text) <= 4000 and text.endswith("."), prompt["title"]
            assert parse_command(text, dict.fromkeys(COMMAND_HELP, _noop)) is None, text


def test_manifest_requests_channel_message_consent_for_the_bot_app() -> None:
    """Thread replay reads Graph under the team owner's RSC consent, given at install."""
    manifest = _manifest()
    assert manifest["webApplicationInfo"]["id"] == manifest["id"], "RSC is granted to the bot app"
    assert manifest["webApplicationInfo"]["resource"], "Teams rejects RSC without a resource"
    assert manifest["authorization"]["permissions"]["resourceSpecific"] == [
        {"name": "ChannelMessage.Read.Group", "type": "Application"},
        {"name": "TeamMember.Read.Group", "type": "Application"},
        {"name": "ChatMessage.Read.Chat", "type": "Application"},
        {"name": "TeamsActivity.Send.Group", "type": "Application"},
    ], (
        "channel and group chat messages, the team's owners and feed notices only; "
        "files need tenant-wide consent, which daimon does not ask"
    )
