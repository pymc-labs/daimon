"""Private GitHub cards use native Discord and Slack layouts."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from daimon.adapters.mcp.tools import github_request_delivery as delivery_module
from daimon.adapters.mcp.tools.github_request_delivery import (
    _discord_embed,
    _discord_view,
    _slack_blocks,
)
from daimon.core.github_request_cards import RequestCard
from daimon.core.stores.github_access_requests import request_access
from daimon.testing.factories import make_account, make_tenant
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def test_request_card_has_state_edge_fields_and_separate_actions() -> None:
    card = RequestCard(
        "Let ResearchBot use a repo that isn't connected yet?\nCan: read only.",
        "Connect and add",
        ("Decline", "Hide for me"),
    )
    embed = _discord_embed(card)
    assert embed.title == "Let ResearchBot use a repo that isn't connected yet?"
    assert [(field.name, field.value) for field in embed.fields] == [("Can", "read only.")]
    assert embed.footer.text == "GitHub on Daimon"
    assert embed.color is not None
    view = _discord_view(card, request_id=uuid.uuid4(), link_url=None)
    assert len(view.to_components()) == 1

    blocks = _slack_blocks(card, request_id=uuid.uuid4(), link_url=None)
    assert [block["type"] for block in blocks] == [
        "section",
        "section",
        "divider",
        "actions",
        "context",
    ]
    assert "repo that isn't connected yet" in str(blocks)


def test_personal_link_is_private_on_discord_and_keeps_its_label() -> None:
    request_id = uuid.uuid4()
    card = RequestCard("Your GitHub link expired.", "Get a new link", ())
    view = _discord_view(card, request_id=request_id, link_url="https://private.example/link")
    button = view.children[0]
    assert button.label == "Get a new link"
    assert button.url is None
    assert button.custom_id == f"github_request:{request_id}:link"
    blocks = _slack_blocks(card, request_id=request_id, link_url="https://private.example/link")
    action = next(block for block in blocks if block["type"] == "actions")
    assert action["elements"][0]["text"]["text"] == "Get a new link"
    assert action["elements"][0]["url"] == "https://private.example/link"


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["discord", "slack"])
async def test_request_card_stays_at_origin_without_dm(
    platform: str,
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with committing_sessionmaker.begin() as session:
        tenant = await make_tenant(session, platform=platform, workspace_id="workspace")
        account = await make_account(session, tenant=tenant)
        request = await request_access(
            session,
            tenant_id=tenant.id,
            requester_account_id=account.id,
            requester_platform_user_id="person",
            platform=platform,
            parent_channel_id="100" if platform == "discord" else "C1",
            thread_id="200" if platform == "discord" else "123.456",
            agent_id=uuid.uuid4(),
            ma_agent_id="agent_1",
            agent_name="ResearchBot",
            requested_work="Continue the task",
            repo_name="example/repo",
            required_ability="read",
            is_admin=False,
            now=datetime.now(UTC),
        )
    runtime = SimpleNamespace(
        session_factory=committing_sessionmaker,
        settings=SimpleNamespace(discord=SimpleNamespace(bot_token=SecretStr("token"))),
    )
    card = RequestCard("GitHub request", "Connect and add", ())
    if platform == "discord":

        class Thread:
            send = AsyncMock(return_value=SimpleNamespace(id=42))
            get_partial_message = AsyncMock()

        client = SimpleNamespace(
            fetch_channel=AsyncMock(return_value=Thread()), fetch_user=AsyncMock()
        )

        @asynccontextmanager
        async def rest_client(_token: str):  # type: ignore[no-untyped-def]
            yield client

        monkeypatch.setattr(delivery_module.discord, "Thread", Thread)
        monkeypatch.setattr(delivery_module, "rest_client", rest_client)
    else:
        client = SimpleNamespace(
            chat_postEphemeral=AsyncMock(return_value={"message_ts": "124.567"}),
            conversations_open=AsyncMock(),
            chat_postMessage=AsyncMock(),
        )
        monkeypatch.setattr(delivery_module, "slack_web_client", AsyncMock(return_value=client))
    assert await delivery_module.deliver_private_request_card(
        runtime,  # type: ignore[arg-type]
        tenant_id=tenant.id,
        platform=platform,
        workspace_id="workspace",
        request_id=request.id,
        recipient_account_id=account.id,
        platform_user_id="person",
        card=card,
    )
    if platform == "discord":
        client.fetch_user.assert_not_awaited()
    else:
        client.chat_postEphemeral.assert_awaited_once()
        client.conversations_open.assert_not_awaited()
        client.chat_postMessage.assert_not_awaited()
