"""Executable record of Teams' deliberate gaps against Discord and Slack.

Teams group chats have no thread to scope a session to, so the adapter refuses
them and the manifest does not offer the scope. Teams also cannot list a
conversation's messages, so unlike Discord's and Slack's bounded history
lookup its boot sweep retires a card intent with no message id without editing
anything; `packages/adapters/teams/tests/test_boot_sweep.py` asserts that.

The rest follows from what a Teams bot can do (see `docs/teams.md`): commands
answer only in the 1:1 chat, which has no threads, so a setup conversation is
keyed inside it; channel history and files need Microsoft Graph; and a dialog's
password field cannot take a `.env` upload or a repository token.

No platform parametrization, no database -- this is a scope check.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import yaml
from daimon.adapters.mcp.tools.channels import register_channel_tools
from daimon.adapters.mcp.tools.credential_requests import register_credential_request_tools
from daimon.adapters.teams.identity import GROUP_CHAT_UNSUPPORTED, Refusal, parse_inbound
from daimon.core.teams_threads import conversation_of, new_setup_thread_id
from fastmcp import FastMCP
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


def test_teams_commands_are_offered_only_in_the_one_to_one_chat() -> None:
    manifest = yaml.safe_load((REPO_ROOT / "docs/teams-app-manifest.yaml").read_text())
    assert all(cl["scopes"] == ["personal"] for cl in manifest["bots"][0]["commandLists"]), (
        "command replies can carry account details; channels get a pointer to the 1:1 chat"
    )


def test_a_teams_setup_conversation_lives_inside_its_chat() -> None:
    chat = "a:chat-1"
    assert conversation_of(new_setup_thread_id(chat)) == chat, (
        "a 1:1 chat has no threads, so its setup conversation is a key within the chat"
    )


async def test_teams_turns_lack_the_graph_and_non_password_tools() -> None:
    mcp = FastMCP(name="t")
    runtime = cast(Any, MagicMock())
    register_channel_tools(mcp, runtime)
    register_credential_request_tools(mcp, runtime)
    tools = await mcp.list_tools()
    teams = {tool.name for tool in tools if "teams" in tool.tags}
    hidden = {"read_channel", "read_thread", "search_messages", "get_message", "list_channels"}
    hidden |= {"parse_link", "request_repo_binding", "request_skill_repo_token"}
    assert hidden <= {tool.name for tool in tools}, "a renamed tool must be renamed here too"
    assert teams >= {"send_message", "create_thread", "request_agent_key"}
    assert not teams & hidden, "these need Microsoft Graph or a non-password input on Teams"
