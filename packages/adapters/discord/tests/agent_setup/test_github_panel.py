"""Discord GitHub panels keep links and repo changes private."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.discord.agent_setup import github_new_repo as notice_module
from daimon.adapters.discord.agent_setup import github_repos as repos_module
from daimon.adapters.discord.agent_setup.github_home import GitHubHomeView, GitHubLinkView
from daimon.adapters.discord.agent_setup.github_repos import GitHubReposView
from daimon.adapters.discord.agent_setup.state import PanelState
from daimon.core.github_panel import GrantsPanel, RepoChoice
from daimon.core.operation_policy import TargetFacts
from daimon.core.roster import RosterAgent
from daimon.core.stores.github_new_repo_notices import NewRepoNotice


def _panel() -> GrantsPanel:
    return GrantsPanel(
        mode="legacy",
        working_repo="example/work",
        has_pat=True,
        repos=(
            RepoChoice(
                repo_id=1,
                full_name="example/work",
                max_access="write",
                baseline="write",
                ceiling="write",
                staged=True,
                working=True,
            ),
        ),
        has_pending=True,
    )


def test_discord_grants_view_shows_staged_state_and_switch() -> None:
    agent = RosterAgent(name="helper", ma_agent_id="ag_helper", model_id="model", is_built_in=False)
    state = PanelState(
        roster=[],
        selected=None,
        account_id=uuid.uuid4(),
        guild_id=123,
        channel_id=456,
        selected_agent=agent,
    )
    view = GitHubReposView(
        state, runtime=MagicMock(), allowed_user_id=7, agent=agent, panel=_panel()
    )
    labels = [item.label for item in view.walk_children() if isinstance(item, discord.ui.Button)]
    text = "\n".join(
        item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay)
    )
    assert "Update GitHub connection" in labels and "Add repos" in labels
    assert "example/work · waiting: read and open issues and pull requests" in text
    assert "GitHub: uses a saved key (old way)" in text


def test_discord_github_home_and_destructive_confirmations() -> None:
    agent = RosterAgent(name="helper", ma_agent_id="ag_helper", model_id="model", is_built_in=False)
    state = PanelState(
        roster=[],
        selected=None,
        account_id=uuid.uuid4(),
        guild_id=123,
        channel_id=456,
        selected_agent=agent,
    )
    home = GitHubHomeView(state, runtime=MagicMock(), allowed_user_id=7, connected_count=2)
    labels = [item.label for item in home.walk_children() if isinstance(item, discord.ui.Button)]
    assert "Choose agent" in labels and "Connect more repos" in labels
    link = GitHubLinkView("https://example.invalid/connect", user_id=7)
    assert {item.label for item in link.children if isinstance(item, discord.ui.Button)} == {
        "Open GitHub ↗",
        "Copy link",
    }
    confirm = GitHubReposView(
        state,
        runtime=MagicMock(),
        allowed_user_id=7,
        agent=agent,
        panel=_panel(),
        confirmation="update_key",
    )
    confirm_labels = [
        item.label for item in confirm.walk_children() if isinstance(item, discord.ui.Button)
    ]
    assert "Update and restart chats" in confirm_labels
    assert any(
        "Unsaved work" in item.content
        for item in confirm.walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    )


@pytest.mark.asyncio
async def test_discord_repo_read_rechecks_external_and_managed_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = RosterAgent(name="helper", ma_agent_id="ag_helper", model_id="model", is_built_in=False)
    account_id = uuid.uuid4()
    state = PanelState(
        roster=[],
        selected=None,
        account_id=account_id,
        guild_id=123,
        channel_id=456,
        selected_agent=agent,
    )
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=object())
    context.__aexit__ = AsyncMock(return_value=None)
    runtime = SimpleNamespace(
        anthropic=object(), sessionmaker=lambda: context, deployment_default=MagicMock()
    )
    view = GitHubReposView(state, runtime=runtime, allowed_user_id=7, agent=agent, panel=_panel())
    interaction = MagicMock()
    interaction.guild_id = 123
    interaction.response.defer = AsyncMock()
    interaction.user.id = 7
    monkeypatch.setattr(
        repos_module,
        "find_agent_by_derived_uuid",
        AsyncMock(
            return_value=SimpleNamespace(
                id="ag_helper", name="helper", metadata={"managed": "true"}
            )
        ),
    )
    monkeypatch.setattr(repos_module, "is_guild_admin", lambda _: False)
    monkeypatch.setattr(
        repos_module,
        "get_account",
        AsyncMock(
            return_value=SimpleNamespace(id=account_id, tenant_id=view._ids()[0], is_external=True)
        ),
    )
    assert not await view.allowed(interaction)
    monkeypatch.setattr(
        repos_module,
        "get_account",
        AsyncMock(
            return_value=SimpleNamespace(id=account_id, tenant_id=view._ids()[0], is_external=False)
        ),
    )
    monkeypatch.setattr(repos_module, "channel_admin_caller", lambda _: MagicMock())
    monkeypatch.setattr(
        repos_module,
        "load_target_facts",
        AsyncMock(return_value=TargetFacts(is_daimon_managed=True, is_reachable_in_tenant=True)),
    )
    assert not await view.allowed(interaction)


@pytest.mark.asyncio
async def test_discord_new_repo_card_releases_failed_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notice = NewRepoNotice(
        tenant_id=uuid.uuid4(),
        installation_id=9,
        repo_full_name="example/new",
        claimed_at=datetime.now(UTC),
    )
    claim = AsyncMock(return_value=notice)
    finish = AsyncMock(return_value=True)
    monkeypatch.setattr(notice_module, "claim_next_notice", claim)
    monkeypatch.setattr(notice_module, "finish_notice", finish)
    monkeypatch.setattr(notice_module, "is_guild_admin", lambda _: True)
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=object())
    session.__aexit__ = AsyncMock(return_value=None)
    runtime = SimpleNamespace(sessionmaker=SimpleNamespace(begin=lambda: session))
    interaction = MagicMock()
    interaction.user.id = 7
    interaction.followup.send = AsyncMock(
        side_effect=discord.HTTPException(MagicMock(status=500, reason="error"), "failed")
    )
    await notice_module.send_pending_notice(runtime, interaction, tenant_id=notice.tenant_id)
    assert finish.await_args.kwargs["delivered"] is False
    assert interaction.followup.send.await_args.kwargs["ephemeral"] is True
