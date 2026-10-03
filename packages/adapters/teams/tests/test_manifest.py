"""The registration template validates against the published Teams v1.25 schema."""

import json
from pathlib import Path
from typing import Any

import yaml
from daimon.adapters.teams.help import COMMAND_HELP
from jsonschema import Draft4Validator

REPO_ROOT = Path(__file__).resolve().parents[4]
# Vendored for an offline test from
# https://developer.microsoft.com/json-schemas/teams/v1.25/MicrosoftTeams.schema.json
SCHEMA = Path(__file__).parent / "data/teams-manifest-v1.25.schema.json"
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


def test_manifest_validates_against_the_v125_schema() -> None:
    manifest = _manifest()
    validator = Draft4Validator(
        json.loads(SCHEMA.read_text()), format_checker=Draft4Validator.FORMAT_CHECKER
    )
    errors = [
        f"{'/'.join(map(str, e.absolute_path))}: {e.message}"
        for e in validator.iter_errors(manifest)
    ]
    assert errors == [], "the template must upload as is"
    assert manifest["manifestVersion"] == "1.25"


def test_manifest_can_be_added_to_private_and_shared_channels() -> None:
    """`supportedChannelTypes` blocks the upload from 1.25 on; the tier flag replaces it."""
    manifest = _manifest()
    assert manifest["supportsChannelFeatures"] == "tier1", "private and shared channels"
    assert "supportedChannelTypes" not in manifest, "rejected by Teams in manifest 1.25+"


def test_manifest_offers_what_the_adapter_answers() -> None:
    [bot] = _manifest()["bots"]
    assert bot["scopes"] == ["personal", "team"], "group chats are refused, so not offered"
    assert bot["supportsFiles"] is True
    [commands] = bot["commandLists"]
    assert {c["title"] for c in commands["commands"]} == set(COMMAND_HELP)


def test_manifest_requests_channel_message_consent_for_the_bot_app() -> None:
    """Thread replay reads Graph under the team owner's RSC consent, given at install."""
    manifest = _manifest()
    assert manifest["webApplicationInfo"]["id"] == manifest["id"], "RSC is granted to the bot app"
    assert manifest["webApplicationInfo"]["resource"], "Teams rejects RSC without a resource"
    assert manifest["authorization"]["permissions"]["resourceSpecific"] == [
        {"name": "ChannelMessage.Read.Group", "type": "Application"},
        {"name": "TeamMember.Read.Group", "type": "Application"},
        {"name": "ChannelMember.Read.Group", "type": "Application"},
    ], (
        "channel messages, the team's owners and a channel's members only; files need "
        "tenant-wide consent, which daimon does not ask"
    )
