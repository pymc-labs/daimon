"""Executable record of Teams' deliberate gaps against Discord and Slack.

Teams group chats have no thread to scope a session to, so the adapter refuses
them and the manifest does not offer the scope. Teams also cannot list a
conversation's messages, so unlike Discord's and Slack's bounded history
lookup its boot sweep retires a card intent with no message id without editing
anything; `packages/adapters/teams/tests/test_boot_sweep.py` asserts that.

The rest follows from what a Teams bot can do (see `docs/teams.md`): commands
answer only in the 1:1 chat, which has no threads, so a setup conversation is
keyed inside it; a turn replays its channel thread through Microsoft Graph,
but the agent's own channel-reading tools stay hidden; files in a channel
work only once a tenant admin grants the app the team's SharePoint site
(`Sites.Selected`), because no team-scoped permission reaches it; and a
dialog's password field cannot take a `.env` upload or a repository token.
Removing the app archives nothing. The `billing` card has no promo code surface: Teams
admins redeem with the MCP tool `redeem_promo_code`. Channel budgets are
Discord and Slack only, so the card shows no channel budget either. So are
channel admins: only the listed admins administer a Teams channel. A Teams
answer carries no usage line (agent, time, tokens, cost, balance) where the
finished Discord or Slack card has one; spend is on the `billing` card.

No platform parametrization, no database -- this is a scope check.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import httpx
import pytest
import yaml
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.tools.channel_admins import (
    _list_channel_admins_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.channel_budgets import (
    _get_channel_budget_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.channels import register_channel_tools
from daimon.adapters.mcp.tools.credential_requests import (
    _request_agent_key_impl,  # pyright: ignore[reportPrivateUsage]
    register_credential_request_tools,
)
from daimon.adapters.teams import card as teams_card
from daimon.adapters.teams.attachments import (
    ChannelMedia,
    InboundFile,
    SharedFile,
    prepare_attachments,
)
from daimon.adapters.teams.billing_panel import panel_card
from daimon.adapters.teams.http_service import create_teams_http_service
from daimon.adapters.teams.identity import GROUP_CHAT_UNSUPPORTED, Refusal, parse_inbound
from daimon.core.billing_panel import BillingPanelState
from daimon.core.channel_admins import ChannelAdminCaller, load_administered_channel_ids
from daimon.core.channel_budget import ChannelBudgetStatus
from daimon.core.config import TeamsSettings
from daimon.core.promo_credit import ActiveTimedCredit
from daimon.core.stores.domain import ChannelBudgetRow, Role
from daimon.core.teams_threads import conversation_of, new_setup_thread_id
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from microsoft_teams.api import MessageActivity
from microsoft_teams.api.activities.install_update import UninstalledActivity
from pydantic import SecretStr

REPO_ROOT = Path(__file__).resolve().parents[2]
TENANT = "00000000-0000-0000-0000-000000000001"
CONTENT_URL = "https://example.sharepoint.com/sites/team/Shared%20Documents/q3.xlsx"


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
    hidden |= {"send_direct_message"}
    assert hidden <= {tool.name for tool in tools}, "a renamed tool must be renamed here too"
    assert teams >= {"send_message", "create_thread", "request_agent_key"}
    assert not teams & hidden, (
        "these need tool-side Graph reads, a non-password input or Discord/Slack DMs"
    )


def test_teams_channel_files_need_a_site_grant_not_a_manifest_permission() -> None:
    manifest = yaml.safe_load((REPO_ROOT / "docs/teams-app-manifest.yaml").read_text())
    granted = manifest["authorization"]["permissions"]["resourceSpecific"]
    assert [p["name"] for p in granted] == ["ChannelMessage.Read.Group"], (
        "no team permission reaches SharePoint, so files need an admin's Sites.Selected grant"
    )


async def test_a_teams_channel_file_without_a_site_grant_is_named_never_fetched() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no request for a channel file: {request.url.host}")

    async def token() -> str:
        return "t"

    async with httpx.AsyncClient(transport=httpx.MockTransport(refuse)) as http:
        prepared = await prepare_attachments(
            http,
            [InboundFile("embedded_file", "file")],
            bot_token=token,
            service_url=None,
            channel_media=ChannelMedia(files=(SharedFile("q3.xlsx", CONTENT_URL),)),
            graph_token=token,
        )
    assert (
        prepared.notice == "I couldn't read `q3.xlsx` (files shared in channels need a 1:1 chat)."
    ), "unresolved (no site grant), a channel file is only named; Discord and Slack fetch it"


def test_the_teams_billing_card_has_no_promo_code_surface() -> None:
    """Even with a redeemable code and live timed credit, the card offers neither."""
    now = datetime(2026, 5, 14, tzinfo=UTC)
    state = BillingPanelState(
        is_admin=True,
        caller_user_id="u",
        caller_spend=0.0,
        caller_turns=0,
        caller_cap=None,
        guild_balance_usd=Decimal("10"),
        guild_spend=0.0,
        guild_turns=0,
        guild_distinct_members=0,
        member_rows=(),
        over_cap_count=0,
        timed_credit=(ActiveTimedCredit(remaining_usd=Decimal("5"), ends_at=now),),
        has_redeemable_promo_code=True,
    )
    card = json.dumps(panel_card(state, since=now).model_dump(by_alias=True, exclude_none=True))
    assert "redeem" not in card.lower() and "timed credit" not in card.lower(), (
        "Teams has no promo code UI on purpose; if it gains one, replace this record"
    )


async def test_teams_has_no_channel_budgets() -> None:
    """The budget tools refuse Teams, and the card renders no budget line even if given one."""
    auth = AuthIdentity(
        account_id=uuid.uuid4(), tenant_id=uuid.uuid4(), role=Role.USER, platform="teams"
    )
    with pytest.raises(ToolError, match="only for Discord servers and Slack workspaces"):
        await _get_channel_budget_impl(cast(Any, MagicMock()), auth, "19:chat")
    now = datetime(2026, 5, 14, tzinfo=UTC)
    budget = ChannelBudgetRow(
        id=uuid.uuid4(),
        tenant_id=auth.tenant_id,
        platform="teams",
        channel_id="19:chat",
        limit_usd=Decimal("5"),
        window="monthly",
        starts_at=None,
        ends_at=None,
        set_by_account_id=None,
        created_at=now,
        updated_at=now,
    )
    state = BillingPanelState(
        is_admin=False,
        caller_user_id="u",
        caller_spend=0.0,
        caller_turns=0,
        caller_cap=None,
        guild_balance_usd=Decimal("10"),
        guild_spend=0.0,
        guild_turns=0,
        guild_distinct_members=0,
        member_rows=(),
        over_cap_count=0,
        channel_budget=ChannelBudgetStatus(budget, Decimal("1"), True),
    )
    card = json.dumps(panel_card(state, since=now).model_dump(by_alias=True, exclude_none=True))
    assert "this channel" not in card.lower(), (
        "Teams has no channel budgets on purpose; if it gains them, replace this record"
    )


async def test_teams_has_no_channel_admins() -> None:
    """The grant tools refuse Teams, and no Teams caller administers a channel."""
    auth = AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        role=Role.ADMIN,
        platform="teams",
        is_admin=True,
    )
    with pytest.raises(ToolError, match="only on Discord and Slack"):
        await _list_channel_admins_impl(cast(Any, MagicMock()), auth)
    administered = await load_administered_channel_ids(
        cast(Any, MagicMock()),
        tenant_id=auth.tenant_id,
        platform="teams",
        caller=ChannelAdminCaller(platform_user_id="u", role_ids=frozenset({"r"})),
    )
    assert administered == frozenset(), (
        "Teams has no channel admins on purpose; if it gains them, replace this record"
    )


def test_removing_the_teams_app_archives_nothing() -> None:
    settings = TeamsSettings(client_id="bot", client_secret=SecretStr("s"), tenant_id=TENANT)
    service = create_teams_http_service(settings=settings, runtime=cast(Any, MagicMock()))
    removal = UninstalledActivity.model_validate(
        {
            "type": "installationUpdate",
            "action": "remove",
            "id": "a-1",
            "channelId": "msteams",
            "from": {"id": "29:u"},
            "recipient": {"id": "28:bot"},
            "conversation": {"id": "a:chat-1", "tenantId": TENANT},
        }
    )
    assert not service.teams_app.router.select_handlers(removal), (
        "Teams has no uninstall to archive on (docs/teams.md); if one lands, run the parity scenario"
    )


async def test_a_teams_key_request_cannot_ask_for_a_env_upload() -> None:
    auth = AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        role=Role.ADMIN,
        platform="teams",
        external_id=TENANT,
        platform_user_id="00000000-0000-0000-0000-000000000002",
        is_admin=True,
    )
    with pytest.raises(ToolError, match="cannot take a .env file upload"):
        await _request_agent_key_impl(
            cast(Any, MagicMock()),
            auth,
            agent_name="a",
            key=None,
            purpose="several keys",
            channel_id="19:c@thread.tacv2",
        )


def test_a_teams_answer_carries_no_usage_line() -> None:
    message = teams_card.answer_message("The posterior mean is 3.", is_last=True)
    assert message.text == "The posterior mean is 3.", "the answer alone, no usage footer"
    assert message.channel_data is not None and message.channel_data.feedback_loop is not None, (
        "the last part still asks for feedback"
    )
