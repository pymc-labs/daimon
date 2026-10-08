"""Slack GitHub repo modal and new-repo delivery."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from daimon.adapters.slack.agent_setup import actions as panel_actions
from daimon.adapters.slack.agent_setup import github_add_repos as add_module
from daimon.adapters.slack.agent_setup import (
    github_manage,
    github_repos,
    github_waiting,
)
from daimon.adapters.slack.agent_setup import github_new_repo as notice_module
from daimon.adapters.slack.agent_setup import github_repos_actions as actions_module
from daimon.adapters.slack.agent_setup.github_add_repos import build_view as build_add_view
from daimon.adapters.slack.agent_setup.github_link import send_link
from daimon.adapters.slack.agent_setup.github_manage import build_view as build_manage_view
from daimon.adapters.slack.agent_setup.github_repos import build_view
from daimon.adapters.slack.agent_setup.github_waiting import build_view as build_waiting_view
from daimon.adapters.slack.agent_setup.panel_views import build_github_home_view
from daimon.adapters.slack.agent_setup.state import (
    PanelMetadata,
    decode_panel_metadata,
    encode_panel_metadata,
)
from daimon.core.github_panel import GrantsPanel, RepoChoice
from daimon.core.stores.github_access import AuthorizedRepo
from daimon.core.stores.github_access_requests import AccessRequest
from daimon.core.stores.github_new_repo_notices import NewRepoNotice, NewRepoNoticeGroup


def test_github_panel_actions_and_steps_are_registered() -> None:
    emitted = (
        add_module.ACTIONS
        | github_manage.ACTIONS
        | github_waiting.ACTIONS
        | actions_module._ACTIONS  # pyright: ignore[reportPrivateUsage]
    )
    assert emitted <= panel_actions.PANEL_ACTION_IDS
    assert github_repos.ACTION_SETTINGS in emitted
    assert github_repos.ACTION_SETTINGS_CHOICE in emitted
    root = Path(panel_actions.__file__).parent
    written: set[str] = set()
    for path in [root / "actions.py", *root.glob("github_*.py")]:
        written.update(re.findall(r"github_step\s*=\s*['\"]([a-z_]+)", path.read_text()))
    for step in written:
        meta = PanelMetadata(
            team_id="T",
            channel_id="C",
            view="github_home",
            github_step=cast(Any, step),
        )
        assert decode_panel_metadata(encode_panel_metadata(meta)) == meta, step


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
async def test_slack_connect_link_in_dm_has_private_actions() -> None:
    client = MagicMock()
    client.chat_postMessage = AsyncMock()
    client.chat_postEphemeral = AsyncMock()
    await send_link(client, channel_id="D123", user_id="U123", url="https://example.test/link")
    client.chat_postMessage.assert_awaited_once()
    client.chat_postEphemeral.assert_not_awaited()
    posted = client.chat_postMessage.await_args.kwargs
    assert posted["channel"] == "D123"
    assert [block["type"] for block in posted["blocks"]] == [
        "section",
        "section",
        "divider",
        "actions",
        "context",
    ]
    actions = next(block for block in posted["blocks"] if block["type"] == "actions")
    assert [button["text"]["text"] for button in actions["elements"]] == [
        "Open GitHub ↗",
        "Copy link",
        "◀ Back",
    ]


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
    assert "example/work — Read and write" in text
    assert "Settings" in actions and "Save changes" not in actions
    settings = build_view(
        PanelMetadata(
            team_id="T",
            channel_id="C",
            view="github_repos",
            agent_name="helper",
            github_settings=True,
        ),
        panel,
    )
    assert "Save changes" in str(settings["blocks"])
    assert "Turn off GitHub for helper" in str(settings["blocks"])
    assert "Change what it can do" in str(settings["blocks"])
    assert "Read only" not in str(settings["blocks"])
    for choice, expected in (
        ("settings_ability", "Read only"),
        ("settings_remove", "Remove repos"),
    ):
        chosen = build_view(
            PanelMetadata(
                team_id="T",
                channel_id="C",
                view="github_repos",
                agent_name="helper",
                github_settings=True,
                github_step=choice,  # type: ignore[arg-type]
            ),
            panel,
        )
        assert expected in str(chosen["blocks"])
    assert "Change who can use these" not in str(settings["blocks"])
    assert "Used by:" not in str(settings["blocks"])


def test_slack_github_home_and_confirmations() -> None:
    meta = PanelMetadata(team_id="T", channel_id="C", view="agents", agent_name="helper")
    home = build_github_home_view(meta, connected_count=2)
    home_text = str(home["blocks"])
    assert "Choose agent" in home_text and "Connect more repos" in home_text
    assert "Manage connected repos" in home_text
    pending = build_github_home_view(
        meta, connected_count=2, pending_url="https://example.invalid/connect"
    )
    pending_text = str(pending["blocks"])
    assert "Continue" in pending_text and "Start over" in pending_text
    assert "Choose agent" not in pending_text
    no_admin = build_github_home_view(meta, connected_count=0, is_admin=False)
    assert "Workspace admins connect repos here" in str(no_admin["blocks"])
    panel = GrantsPanel(mode="legacy", repos=(), working_repo=None, has_pat=True, saved_state=True)
    refused = build_view(meta, panel)
    assert "This agent uses a saved GitHub key" in str(refused["blocks"])


def test_slack_manage_connected_repos_requires_confirmation() -> None:
    repo = AuthorizedRepo(
        tenant_id=uuid.uuid4(),
        repo_id=101,
        owner_id=1,
        installation_id=9,
        repo_full_name="example/work",
        max_access="write",
        authorized_by_github_user_id=2,
        authorized_by_account_id=None,
        authorized_at=datetime.now(UTC),
        status="active",
        status_reason=None,
        version=1,
    )
    meta = PanelMetadata(team_id="T", channel_id="C", view="github_manage")
    listing = build_manage_view(meta, [repo])
    assert "Change" in str(listing["blocks"])
    assert "Disconnect" in str(listing["blocks"])
    confirm = build_manage_view(
        PanelMetadata(
            team_id="T",
            channel_id="C",
            view="github_manage",
            repo_id=101,
            github_step="manage_disconnect",
        ),
        [repo],
    )
    assert "Disconnect example/work? Agents using it lose it." in str(confirm["blocks"])
    assert "◀ Back" in str(confirm["blocks"])


def test_slack_member_github_home_shows_only_personal_link() -> None:
    from daimon.adapters.slack.agent_setup.panel_views import build_github_home_view

    view = build_github_home_view(
        PanelMetadata(team_id="T", channel_id="C", view="github_home"),
        connected_count=0,
        is_admin=False,
        linked_login="carlos",
    )
    blocks = str(view["blocks"])
    assert "Linked as @carlos" in blocks
    assert "Use another account" in blocks and "Unlink" in blocks
    assert "Connect GitHub" not in blocks
    admin = build_github_home_view(
        PanelMetadata(team_id="T", channel_id="C", view="github_home"),
        connected_count=0,
        is_admin=True,
    )
    assert "Connect GitHub" in str(admin["blocks"])


@pytest.mark.asyncio
async def test_slack_agent_setup_shows_saved_key_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = "This agent uses a saved GitHub key. Ask your Daimon operator to switch it."
    monkeypatch.setattr(actions_module, "sync_connect_admin", AsyncMock())
    monkeypatch.setattr(actions_module, "connect_link", AsyncMock(side_effect=ValueError(message)))
    post = AsyncMock()
    monkeypatch.setattr(actions_module, "post_ephemeral", post)
    await actions_module._handle_add(
        MagicMock(),
        MagicMock(),
        {},
        action={},
        action_id=add_module.ACTION_CONNECT_MORE,
        meta=PanelMetadata(team_id="T", channel_id="C", view="github_add", agent_name="Helper"),
        panel=GrantsPanel(mode="legacy", repos=(), working_repo=None, has_pat=False),
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        user_id="U123",
        channel_id="C",
        push=False,
        is_admin=True,
    )
    assert post.await_args.kwargs["text"] == message


def test_slack_add_repos_keeps_selection_on_one_screen() -> None:
    panel = GrantsPanel(
        mode="legacy",
        working_repo="example/work",
        has_pat=False,
        repos=(
            RepoChoice(1, "example/work", "write", None, None, False, True),
            RepoChoice(2, "example/readme", "read", None, None, False, False),
        ),
    )
    meta = PanelMetadata(
        team_id="T",
        channel_id="C",
        view="github_add",
        agent_name="helper",
        channel_name="readme",
    )
    pick = build_add_view(meta, panel)
    selector = next(
        item
        for block in pick["blocks"]
        if block["type"] == "actions"
        for item in block["elements"]
        if item["type"] == "multi_static_select"
    )
    assert selector["initial_options"] == []
    assert "Add repos" in str(pick["blocks"])
    assert "Review repos" not in str(pick["blocks"])
    assert "Suggested: example/readme" in str(pick["blocks"])
    chosen = PanelMetadata(
        team_id="T",
        channel_id="C",
        view="github_add",
        agent_name="helper",
        selected_repo_ids=(1, 2),
    )
    saved = decode_panel_metadata(encode_panel_metadata(chosen))
    assert saved == chosen
    assert "Add repos" in str(build_add_view(saved, panel)["blocks"])
    empty = build_add_view(
        meta, GrantsPanel(mode="legacy", repos=(), working_repo=None, has_pat=False), is_admin=True
    )
    empty_actions = [
        element["text"]["text"]
        for block in empty["blocks"]
        if block["type"] == "actions"
        for element in block["elements"]
        if element["type"] == "button"
    ]
    assert "Connect GitHub" in empty_actions
    assert "Add repos" not in empty_actions
    saved_key = build_add_view(
        PanelMetadata(team_id="T", channel_id="C", view="github_add", agent_name="helper"),
        GrantsPanel(mode="legacy", repos=(), working_repo=None, has_pat=True, saved_state=True),
    )
    assert "This agent uses a saved GitHub key" in str(saved_key["blocks"])


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
