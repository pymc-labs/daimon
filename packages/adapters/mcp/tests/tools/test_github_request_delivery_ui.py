"""Private GitHub cards use native Discord and Slack layouts."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from daimon.adapters.mcp.tools import github_request_delivery as delivery_module
from daimon.adapters.mcp.tools import github_requests as requests_module
from daimon.adapters.mcp.tools.github_request_delivery import (
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
    view = _discord_view(card, request_id=uuid.uuid4(), link_url=None)
    assert len(view.to_components()) == 1

    blocks = _slack_blocks(card, request_id=uuid.uuid4(), link_url=None)
    assert [block["type"] for block in blocks] == [
        "section",
        "section",
        "divider",
        "actions",
    ]
    assert "repo that isn't connected yet" in str(blocks)
    assert "not connected yet)" not in str(blocks)


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
async def test_slack_admin_visibility_requires_private_channel_membership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = SimpleNamespace(
        conversations_info=AsyncMock(return_value={"channel": {"is_private": True}}),
        conversations_members=AsyncMock(return_value={"members": ["U-visible"]}),
    )
    monkeypatch.setattr(requests_module, "slack_web_client", AsyncMock(return_value=client))
    runtime = SimpleNamespace()
    kwargs = dict(
        platform="slack",
        workspace_id="T1",
        channel_id="G1",
    )
    assert await requests_module._can_see_origin(  # type: ignore[attr-defined]
        runtime, platform_user_id="U-visible", **kwargs
    )
    assert not await requests_module._can_see_origin(  # type: ignore[attr-defined]
        runtime, platform_user_id="U-hidden", **kwargs
    )
    assert not await requests_module._can_see_origin(  # type: ignore[attr-defined]
        runtime,
        platform_user_id="U-visible",
        platform="slack",
        workspace_id="T1",
        channel_id="D1",
    )


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
    card = RequestCard(
        "Let ResearchBot use private/repo?\nCan: Read and write.",
        "Connect and add",
        (),
        names_repos=True,
    )
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
        assert "private/repo" not in str(Thread.send.await_args.kwargs)
    else:
        client.chat_postEphemeral.assert_awaited_once()
        client.conversations_open.assert_not_awaited()
        client.chat_postMessage.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("card_text", "names_repos", "expected"),
    [
        ("An admin has been asked to allow GitHub access.", False, "An admin has been asked"),
        ("✓ Linked as @person", False, "✓ Linked as @person"),
        ("Admin unavailable. Ask an admin to open /github.", False, "Admin unavailable"),
        ("Let Bot use private/repo?", True, "GitHub request"),
        ("Let Bot use private/repo?", False, "Let Bot use private/repo?"),
    ],
)
async def test_discord_requester_status_only_neutralizes_repo_names(
    card_text: str,
    names_repos: bool,
    expected: str,
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with committing_sessionmaker.begin() as session:
        tenant = await make_tenant(session, platform="discord", workspace_id="workspace")
        account = await make_account(session, tenant=tenant)
        request = await request_access(
            session,
            tenant_id=tenant.id,
            requester_account_id=account.id,
            requester_platform_user_id="person",
            platform="discord",
            parent_channel_id="100",
            thread_id="200",
            agent_id=uuid.uuid4(),
            ma_agent_id="agent_1",
            agent_name="Bot",
            requested_work="Continue task",
            repo_name="private/repo",
            required_ability="read",
            is_admin=False,
        )

    class Thread:
        send = AsyncMock(return_value=SimpleNamespace(id=42))

    client = SimpleNamespace(fetch_channel=AsyncMock(return_value=Thread()))

    @asynccontextmanager
    async def rest_client(_token: str):  # type: ignore[no-untyped-def]
        yield client

    monkeypatch.setattr(delivery_module.discord, "Thread", Thread)
    monkeypatch.setattr(delivery_module, "rest_client", rest_client)
    runtime = SimpleNamespace(
        session_factory=committing_sessionmaker,
        settings=SimpleNamespace(discord=SimpleNamespace(bot_token=SecretStr("token"))),
    )
    assert await delivery_module.deliver_private_request_card(
        runtime,
        tenant_id=tenant.id,
        platform="discord",
        workspace_id="workspace",
        request_id=request.id,
        recipient_account_id=account.id,
        platform_user_id="person",
        card=RequestCard(card_text, None, names_repos=names_repos),
    )
    assert expected in Thread.send.await_args.kwargs["embed"].title
    if names_repos:
        assert "private/repo" not in str(Thread.send.await_args.kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["discord", "slack"])
async def test_shared_admin_card_is_neutral_and_updates_once(
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
            repo_name="private/secret",
            required_ability="write",
            is_admin=False,
        )
    runtime = SimpleNamespace(
        session_factory=committing_sessionmaker,
        settings=SimpleNamespace(discord=SimpleNamespace(bot_token=SecretStr("token"))),
    )
    if platform == "discord":

        class Thread:
            send = AsyncMock(return_value=SimpleNamespace(id=42))
            edit = AsyncMock()

            def get_partial_message(self, _id: int) -> Thread:
                return self

        thread = Thread()
        client = SimpleNamespace(fetch_channel=AsyncMock(return_value=thread))

        @asynccontextmanager
        async def rest_client(_token: str):  # type: ignore[no-untyped-def]
            yield client

        monkeypatch.setattr(delivery_module.discord, "Thread", Thread)
        monkeypatch.setattr(delivery_module, "rest_client", rest_client)
    else:
        client = SimpleNamespace(
            chat_postMessage=AsyncMock(return_value={"ts": "123.999"}),
            chat_update=AsyncMock(),
            chat_postEphemeral=AsyncMock(),
        )
        monkeypatch.setattr(delivery_module, "slack_web_client", AsyncMock(return_value=client))
    kwargs = dict(
        tenant_id=tenant.id,
        request_id=request.id,
        platform=platform,
        workspace_id="workspace",
        admin_user_ids=["123" if platform == "discord" else "U123"],
    )
    assert await delivery_module.deliver_shared_admin_card(runtime, **kwargs)  # type: ignore[arg-type]
    assert await delivery_module.deliver_shared_admin_card(runtime, **kwargs)  # type: ignore[arg-type]
    if platform == "discord":
        thread.send.assert_awaited_once()
        assert "private/secret" not in str(thread.send.await_args.kwargs)
        assert "Review" in str(thread.send.await_args.kwargs["view"].children[0].label)
        thread.edit.assert_awaited_once()
    else:
        client.chat_postMessage.assert_awaited_once()
        assert "private/secret" not in str(client.chat_postMessage.await_args.kwargs)
        assert "<@U123>" in str(client.chat_postMessage.await_args.kwargs)
        client.chat_update.assert_awaited_once()
        client.chat_postEphemeral.assert_not_awaited()
