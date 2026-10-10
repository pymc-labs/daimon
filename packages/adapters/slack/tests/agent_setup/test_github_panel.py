"""Slack GitHub repo modal and new-repo delivery."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from daimon.adapters.slack.agent_setup import actions as panel_actions
from daimon.adapters.slack.agent_setup import github_new_repo as notice_module
from daimon.adapters.slack.agent_setup import (
    github_repos,
)
from daimon.adapters.slack.agent_setup import github_repos_actions as actions_module
from daimon.adapters.slack.agent_setup.github_link import send_link
from daimon.adapters.slack.agent_setup.github_repos import build_view
from daimon.adapters.slack.agent_setup.github_waiting import build_view as build_waiting_view
from daimon.adapters.slack.agent_setup.panel_views import build_github_home_view
from daimon.adapters.slack.agent_setup.state import (
    PanelMetadata,
)
from daimon.core.github_panel import GrantsPanel, RepoChoice
from daimon.core.stores.github_access_requests import AccessRequest
from daimon.core.stores.github_new_repo_notices import NewRepoNotice, NewRepoNoticeGroup


def test_github_panel_registers_only_read_actions() -> None:
    assert github_repos.ACTION_OPEN in panel_actions.PANEL_ACTION_IDS
    assert github_repos.ACTION_BACK in panel_actions.PANEL_ACTION_IDS
    assert github_repos.ACTION_CONNECT_LINK in panel_actions.PANEL_ACTION_IDS
    assert actions_module._ACTIONS <= panel_actions.PANEL_ACTION_IDS  # pyright: ignore[reportPrivateUsage]


def _request() -> AccessRequest:
    now = datetime.now(UTC)
    return AccessRequest(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        requester_account_id=uuid.uuid4(),
        requester_platform_user_id="U123",
        platform="slack",
        parent_channel_id="C123",
        thread_id="1700.0001",
        agent_id=uuid.uuid4(),
        ma_agent_id="ag_helper",
        agent_name="Helper",
        repo_names=["example/private"],
        required_ability="write",
        approved_by_account_id=None,
        requested_work="Finish the report",
        status="open",
        created_at=now,
        updated_at=now,
        expires_at=now,
        admin_notified_at=None,
        resumed_at=None,
    )


def test_slack_waiting_review_hides_unconnected_repo_name() -> None:
    request = _request()
    view = build_waiting_view(
        PanelMetadata(team_id="T", channel_id="C", view="github_waiting"),
        requests=(request,),
        own=(),
        connected_names=frozenset(),
        selected=request,
    )
    serialized = str(view["blocks"])
    assert "example/private" not in serialized
    assert "Connect and add" in serialized
    assert "Decline" in serialized and "Hide for me" in serialized
    named = build_waiting_view(
        PanelMetadata(team_id="T", channel_id="C", view="github_waiting"),
        requests=(request,),
        own=(),
        connected_names=frozenset({"example/private"}),
        selected=request,
    )
    assert "example/private" in str(named["blocks"])
    assert "Add repo" in str(named["blocks"])


@pytest.mark.asyncio
async def test_slack_connect_link_uses_ephemeral_actions() -> None:
    client = MagicMock()
    client.chat_postMessage = AsyncMock()
    client.chat_postEphemeral = AsyncMock()
    await send_link(
        client,
        channel_id="C123",
        thread_id="123.456",
        user_id="U123",
        url="https://example.test/link",
        app_root_url="https://mcp.test",
    )
    client.chat_postMessage.assert_not_awaited()
    client.chat_postEphemeral.assert_awaited_once()
    posted = client.chat_postEphemeral.await_args.kwargs
    assert posted["channel"] == "C123"
    assert posted["thread_ts"] == "123.456"
    assert posted["text"] == "Connect GitHub"
    assert "https://example.test/link" not in posted["text"]
    attachment = posted["attachments"][0]
    assert attachment["color"] == "#0C1F40"
    assert attachment["blocks"][0]["elements"][0]["image_url"] == (
        "https://mcp.test/web/daimon-face.png"
    )
    assert attachment["blocks"][2]["accessory"]["image_url"] == (
        "https://mcp.test/web/github-mark.png"
    )
    assert [block["type"] for block in attachment["blocks"]] == [
        "context",
        "header",
        "section",
        "actions",
    ]
    actions = next(block for block in attachment["blocks"] if block["type"] == "actions")
    assert [button["text"] for button in actions["elements"]] == [
        {"type": "plain_text", "text": "🔗 Connect GitHub", "emoji": True}
    ]
    assert actions["elements"][0]["url"] == "https://example.test/link"


def _section_text(view: dict[str, Any]) -> str:
    return "\n".join(
        block["text"]["text"]
        for block in view["blocks"]
        if block["type"] == "section" and "text" in block
    )


def _button_labels(view: dict[str, Any]) -> list[str]:
    return [
        element["text"]["text"]
        for block in view["blocks"]
        if block["type"] == "actions"
        for element in block["elements"]
        if element["type"] == "button"
    ]


def test_slack_github_panels_are_read_only() -> None:
    panel = GrantsPanel(
        mode="app",
        working_repo="example/work",
        has_pat=False,
        repos=(
            RepoChoice(
                repo_id=1,
                full_name="example/work",
                max_access="write",
                baseline="write",
                ceiling="write",
                staged=False,
                working=True,
                live_baseline="write",
                live_ceiling="write",
            ),
        ),
    )
    meta = PanelMetadata(team_id="T", channel_id="C", view="github_repos", agent_name="helper")
    view = build_view(meta, panel, connect_url="https://example.invalid/connect")
    text = _section_text(view)
    assert "helper's repos" in text and "example/work" in text
    assert "Working repo: example/work" in text
    assert "To change repos, ask helper in chat." in text
    assert " · " not in text
    assert _button_labels(view) == ["Connect GitHub", "◀ Back"]
    assert "https://example.invalid/connect" in str(view)
    empty = build_view(meta, GrantsPanel(mode="app", repos=(), working_repo=None, has_pat=False))
    assert "No working repo" in _section_text(empty)
    home = build_github_home_view(meta, agent_counts=(("helper", 1),), can_choose_agent=True)
    assert "helper: 1 repo" in _section_text(home)
    assert "Add repos" not in str(home) and "Unlink" not in str(home)


@pytest.mark.asyncio
async def test_stale_slack_edit_action_does_not_reach_a_handler() -> None:
    runtime = MagicMock()
    client = MagicMock()
    for action_id in (
        "agent_setup__github_remove",
        "agent_setup__github_deactivate",
        "agent_setup__github_add_commit",
        "agent_setup__github_manage_write",
        "agent_setup__github_confirm_remove",
    ):
        assert action_id not in panel_actions.PANEL_ACTION_IDS
        assert not await actions_module.handle(
            runtime,
            client,
            {},
            action={"action_id": action_id},
            meta=PanelMetadata(team_id="T", channel_id="C", view="github_repos"),
            team_id="T",
            user_id="U",
        )
    runtime.sessionmaker.assert_not_called()
    client.views_update.assert_not_called()
    client.views_push.assert_not_called()


@pytest.mark.asyncio
async def test_slack_new_repo_card_claim_is_released_on_post_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notice = NewRepoNotice(
        tenant_id=uuid.uuid4(),
        installation_id=9,
        repo_full_name="example/new",
        queued_at=datetime.now(UTC),
        claimed_at=datetime.now(UTC),
    )
    monkeypatch.setattr(
        notice_module,
        "claim_notice_group",
        AsyncMock(return_value=NewRepoNoticeGroup(notices=(notice,))),
    )
    finish = AsyncMock(return_value=True)
    monkeypatch.setattr(notice_module, "finish_notice", finish)
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=object())
    session.__aexit__ = AsyncMock(return_value=None)
    runtime = SimpleNamespace(
        sessionmaker=SimpleNamespace(begin=lambda: session),
        settings=SimpleNamespace(crypto=SimpleNamespace(keys=())),
    )
    client = MagicMock()
    client.chat_postEphemeral = AsyncMock(side_effect=RuntimeError("post failed"))
    await notice_module.send_pending_notice(
        runtime, client, team_id="T", channel_id="C", user_id="U"
    )
    assert client.chat_postEphemeral.await_args.kwargs["user"] == "U"
    assert finish.await_args.kwargs["delivered"] is False


@pytest.mark.asyncio
async def test_generic_new_repo_card_does_not_send_name_in_slack_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notice = NewRepoNotice(
        tenant_id=uuid.uuid4(),
        installation_id=9,
        repo_full_name="example/private",
        queued_at=datetime.now(UTC),
        claimed_at=datetime.now(UTC),
    )
    monkeypatch.setattr(
        notice_module,
        "claim_notice_group",
        AsyncMock(return_value=NewRepoNoticeGroup(notices=(notice,))),
    )
    monkeypatch.setattr(notice_module, "finish_notice", AsyncMock(return_value=True))
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=object())
    session.__aexit__ = AsyncMock(return_value=None)
    runtime = SimpleNamespace(
        sessionmaker=SimpleNamespace(begin=lambda: session),
        settings=SimpleNamespace(crypto=SimpleNamespace(keys=())),
    )
    client = MagicMock()
    client.chat_postEphemeral = AsyncMock(return_value={"ok": True})
    await notice_module.send_pending_notice(
        runtime, client, team_id="T", channel_id="C", user_id="U"
    )
    posted = client.chat_postEphemeral.await_args.kwargs
    assert posted["text"] == "New repos are available on GitHub."
    assert "example/private" not in str(posted["blocks"])
    actions = next(block for block in posted["blocks"] if block["type"] == "actions")
    assert actions["elements"][0]["text"]["text"] == "Connect more repos"
    assert len(actions["elements"]) == 1
