"""Executable record of Teams' deliberate gaps against Discord and Slack.

Teams group chats have no thread to scope a session to, so the adapter refuses
them and the manifest does not offer the scope. Teams also cannot list a
conversation's messages, so unlike Discord's and Slack's bounded history
lookup its boot sweep retires a card intent with no message id without editing
anything; `packages/adapters/teams/tests/test_boot_sweep.py` asserts that.

No platform parametrization, no database -- this is a scope check.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from daimon.adapters.teams.identity import GROUP_CHAT_UNSUPPORTED, Refusal, parse_inbound
from microsoft_teams.api import MessageActivity

REPO_ROOT = Path(__file__).resolve().parents[2]
TENANT = "00000000-0000-0000-0000-000000000001"


def test_teams_refuses_group_chats() -> None:
    activity = MessageActivity.model_validate(
        {
            "type": "message",
            "id": "a-1",
            "channelId": "msteams",
            "serviceUrl": "https://smba.example.test",
            "from": {"id": "29:u", "aadObjectId": "00000000-0000-0000-0000-000000000002"},
            "recipient": {"id": "28:bot"},
            "conversation": {
                "id": "19:g@thread.v2",
                "conversationType": "groupChat",
                "tenantId": TENANT,
            },
            "channelData": {"tenant": {"id": TENANT}},
            "text": "hi",
        }
    )
    refusal = parse_inbound(activity, configured_tenant=TENANT, service_url=None)
    assert refusal == Refusal(GROUP_CHAT_UNSUPPORTED), (
        "Teams group chats are refused on purpose; if they gained support, replace this record"
    )


def test_teams_manifest_does_not_offer_group_chats() -> None:
    manifest = yaml.safe_load((REPO_ROOT / "docs/teams-app-manifest.yaml").read_text())
    assert all("groupChat" not in bot["scopes"] for bot in manifest["bots"]), (
        "the manifest must not offer a scope the adapter refuses"
    )
