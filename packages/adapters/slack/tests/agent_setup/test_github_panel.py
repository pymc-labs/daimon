"""Slack GitHub repo modal and new-repo delivery."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from daimon.adapters.slack.agent_setup import github_new_repo as notice_module
from daimon.adapters.slack.agent_setup import github_repos_actions as actions_module
from daimon.adapters.slack.agent_setup.github_add_repos import build_view as build_add_view
from daimon.adapters.slack.agent_setup.github_repos import build_confirm_view, build_view
from daimon.adapters.slack.agent_setup.panel_views import build_github_home_view
from daimon.adapters.slack.agent_setup.state import (
    PanelMetadata,
    decode_panel_metadata,
    encode_panel_metadata,
)
from daimon.core.github_panel import GrantsPanel, RepoChoice
from daimon.core.stores.github_new_repo_notices import NewRepoNotice


def test_slack_grants_view_shows_staged_and_live_values() -> None:
    panel = GrantsPanel(
        mode="app",
        working_repo="example/work",
        has_pat=False,
        has_pending=True,
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
            RepoChoice(
                repo_id=2,
                full_name="example/other",
                max_access="read",
                baseline="read",
                ceiling="read",
                staged=True,
                working=False,
            ),
        ),
    )
    view = build_view(
        PanelMetadata(
            team_id="T",
            channel_id="C",
            view="github_repos",
            agent_name="helper",
        ),
        panel,
    )
    text = "\n".join(
        block["text"]["text"] for block in view["blocks"] if block["type"] == "section"
    )
    actions = [
        element["text"]["text"]
        for block in view["blocks"]
        if block["type"] == "actions"
        for element in block["elements"]
        if element["type"] == "button"
    ]
    assert "example/other · waiting: read only" in text
    assert "Save changes" in actions and "Turn off GitHub" in actions


def test_slack_github_home_and_confirmations() -> None:
    meta = PanelMetadata(team_id="T", channel_id="C", view="agents", agent_name="helper")
    home = build_github_home_view(meta, connected_count=2)
    home_text = str(home["blocks"])
    assert "Choose agent" in home_text and "Connect more repos" in home_text
    no_admin = build_github_home_view(meta, connected_count=0, is_admin=False)
    assert "Workspace admins connect repos here" in str(no_admin["blocks"])
    panel = GrantsPanel(mode="legacy", repos=(), working_repo=None, has_pat=True)
    confirmed = build_confirm_view(meta, panel, choice="update_key")
    assert "Update and restart chats" in str(confirmed["blocks"])
    assert "Unsaved work" in str(confirmed["blocks"])


def test_slack_add_repos_keeps_selection_through_review() -> None:
    panel = GrantsPanel(
        mode="legacy",
        working_repo="example/work",
        has_pat=True,
        repos=(
            RepoChoice(1, "example/work", "write", None, None, False, True),
            RepoChoice(2, "example/readme", "read", None, None, False, False),
        ),
    )
    meta = PanelMetadata(team_id="T", channel_id="C", view="github_add", agent_name="helper")
    pick = build_add_view(meta, panel)
    selector = next(
        item
        for block in pick["blocks"]
        if block["type"] == "actions"
        for item in block["elements"]
        if item["type"] == "multi_static_select"
    )
    assert [option["value"] for option in selector["initial_options"]] == ["1"]
    reviewed = PanelMetadata(
        team_id="T",
        channel_id="C",
        view="github_add",
        agent_name="helper",
        selected_repo_ids=(1, 2),
        github_step="review",
    )
    saved = decode_panel_metadata(encode_panel_metadata(reviewed))
    assert saved == reviewed
    review = build_add_view(saved, panel)
    assert "example/readme — read only" in str(review["blocks"])
    assert "Update and restart chats" in str(
        build_add_view(
            PanelMetadata(
                team_id="T",
                channel_id="C",
                view="github_add",
                agent_name="helper",
                selected_repo_ids=(1,),
                github_step="confirm_key",
            ),
            panel,
        )["blocks"]
    )


@pytest.mark.asyncio
async def test_slack_repo_open_refuses_unauthorized_viewer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=object())
    context.__aexit__ = AsyncMock(return_value=None)
    runtime = SimpleNamespace(
        sessionmaker=lambda: context, anthropic=object(), deployment_default=MagicMock()
    )
    client = MagicMock()
    client.views_push = AsyncMock()
    monkeypatch.setattr(actions_module, "resolve_is_admin", AsyncMock(return_value=False))
    monkeypatch.setattr(
        actions_module,
        "load_panel_roster",
        AsyncMock(
            return_value=SimpleNamespace(
                rows=[SimpleNamespace(name="helper", ma_agent_id="ag_helper")]
            )
        ),
    )
    refusal = AsyncMock(return_value=True)
    monkeypatch.setattr(actions_module, "refuse_unless_allowed_for_agent_name", refusal)
    handled = await actions_module.handle(
        runtime,
        client,
        {"trigger_id": "trigger"},
        action={"action_id": "agent_setup__github_repos", "value": "helper"},
        meta=PanelMetadata(team_id="T", channel_id="C", view="details"),
        team_id="T",
        user_id="U",
    )
    assert handled and refusal.await_count == 1
    client.views_push.assert_not_awaited()


@pytest.mark.asyncio
async def test_slack_new_repo_card_claim_is_released_on_post_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notice = NewRepoNotice(
        tenant_id=uuid.uuid4(),
        installation_id=9,
        repo_full_name="example/new",
        claimed_at=datetime.now(UTC),
    )
    monkeypatch.setattr(notice_module, "claim_next_notice", AsyncMock(return_value=notice))
    finish = AsyncMock(return_value=True)
    monkeypatch.setattr(notice_module, "finish_notice", finish)
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=object())
    session.__aexit__ = AsyncMock(return_value=None)
    runtime = SimpleNamespace(sessionmaker=SimpleNamespace(begin=lambda: session))
    client = MagicMock()
    client.chat_postEphemeral = AsyncMock(side_effect=RuntimeError("post failed"))
    await notice_module.send_pending_notice(
        runtime, client, team_id="T", channel_id="C", user_id="U"
    )
    assert client.chat_postEphemeral.await_args.kwargs["user"] == "U"
    assert finish.await_args.kwargs["delivered"] is False
