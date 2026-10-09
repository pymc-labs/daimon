"""Discord GitHub panels keep links and repo changes private."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.discord.agent_setup import github_add_repos as add_module
from daimon.adapters.discord.agent_setup import github_new_repo as notice_module
from daimon.adapters.discord.agent_setup import github_repos as repos_module
from daimon.adapters.discord.agent_setup import github_requests as request_module
from daimon.adapters.discord.agent_setup.github_add_repos import GitHubAddReposView
from daimon.adapters.discord.agent_setup.github_home import GitHubHomeView, GitHubLinkView
from daimon.adapters.discord.agent_setup.github_manage import GitHubManageView
from daimon.adapters.discord.agent_setup.github_repos import GitHubReposView
from daimon.adapters.discord.agent_setup.github_waiting import GitHubWaitingView
from daimon.adapters.discord.agent_setup.navigation import PanelViewBase
from daimon.adapters.discord.agent_setup.state import PanelState
from daimon.adapters.discord.commands.github import GitHubCog
from daimon.core.github_panel import GrantsPanel, RepoChoice
from daimon.core.operation_policy import TargetFacts
from daimon.core.roster import RosterAgent
from daimon.core.stores.github_access import AuthorizedRepo
from daimon.core.stores.github_access_requests import AccessRequest
from daimon.core.stores.github_new_repo_notices import NewRepoNotice, NewRepoNoticeGroup


@pytest.mark.asyncio
async def test_discord_github_card_ids_reach_registered_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notice_handler = AsyncMock(return_value=False)
    request_handler = AsyncMock(return_value=True)
    monkeypatch.setattr(notice_module, "handle_dm_notice", notice_handler)
    monkeypatch.setattr(request_module, "handle_request_card", request_handler)
    runtime = object()
    interaction = MagicMock()
    interaction.client.runtime = runtime
    interaction.data = {"custom_id": f"github_request:{uuid.uuid4()}:approve"}
    await GitHubCog.on_interaction(object(), interaction)
    notice_handler.assert_awaited_once_with(interaction, runtime)
    request_handler.assert_awaited_once_with(interaction, runtime)

    notice_handler.reset_mock()
    notice_handler.return_value = True
    request_handler.reset_mock()
    interaction.data = {"custom_id": f"github_notice:{uuid.uuid4()}:2026-10-08:connect"}
    await GitHubCog.on_interaction(object(), interaction)
    notice_handler.assert_awaited_once_with(interaction, runtime)
    request_handler.assert_not_awaited()


def _embed_text(view: object) -> str:
    embed = view.embed
    return "\n".join(
        [embed.title or "", embed.description or ""]
        + [f"{field.name}\n{field.value}" for field in embed.fields]
    )


def _connected_repo() -> AuthorizedRepo:
    return AuthorizedRepo(
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


def test_discord_waiting_review_hides_unconnected_repo_name() -> None:
    now = datetime.now(UTC)
    request = AccessRequest(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        requester_account_id=uuid.uuid4(),
        requester_platform_user_id="12345",
        platform="discord",
        parent_channel_id="456",
        thread_id="789",
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
    state = PanelState(roster=[], selected=None, account_id=uuid.uuid4(), is_admin=True, guild_id=1)
    hidden = GitHubWaitingView(
        state,
        runtime=MagicMock(),
        allowed_user_id=7,
        requests=(request,),
        own_requests=(),
        repos=(),
        selected_id=request.id,
    )
    text = _embed_text(hidden)
    assert "example/private" not in text
    assert "Connect and add" in {
        item.label for item in hidden.walk_children() if isinstance(item, discord.ui.Button)
    }
    visible = GitHubWaitingView(
        state,
        runtime=MagicMock(),
        allowed_user_id=7,
        requests=(request,),
        own_requests=(),
        repos=(_connected_repo().model_copy(update={"repo_full_name": "example/private"}),),
        selected_id=request.id,
    )
    named = _embed_text(visible)
    assert "example/private" in named


def test_discord_waiting_list_pages_after_twenty_requests() -> None:
    now = datetime.now(UTC)
    first = AccessRequest(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        requester_account_id=uuid.uuid4(),
        requester_platform_user_id="12345",
        platform="discord",
        parent_channel_id="456",
        thread_id="789",
        agent_id=uuid.uuid4(),
        ma_agent_id="ag_helper",
        agent_name="Helper",
        repo_names=["example/private"],
        required_ability="read",
        approved_by_account_id=None,
        requested_work="Continue",
        status="open",
        created_at=now,
        updated_at=now,
        expires_at=now,
        admin_notified_at=None,
        resumed_at=None,
    )
    requests = tuple(first.model_copy(update={"id": uuid.uuid4()}) for _ in range(21))
    state = PanelState(roster=[], selected=None, account_id=uuid.uuid4(), is_admin=True, guild_id=1)
    view = GitHubWaitingView(
        state,
        runtime=MagicMock(),
        allowed_user_id=7,
        requests=requests,
        own_requests=(),
        repos=(),
        page=1,
    )
    select = next(item for item in view.walk_children() if isinstance(item, discord.ui.Select))
    assert len(select.options) == 1
    labels = {
        item.label: item.disabled
        for item in view.walk_children()
        if isinstance(item, discord.ui.Button)
    }
    assert labels["Previous requests"] is False
    assert labels["Next requests"] is True

    own = GitHubWaitingView(
        state,
        runtime=MagicMock(),
        allowed_user_id=7,
        requests=(),
        own_requests=requests,
        repos=(),
        own_page=1,
    )
    own_select = next(item for item in own.walk_children() if isinstance(item, discord.ui.Select))
    assert len(own_select.options) == 1
    assert own_select.options[0].value == str(requests[20].id)
    own_labels = {
        item.label: item.disabled
        for item in own.walk_children()
        if isinstance(item, discord.ui.Button)
    }
    assert own_labels["Previous your requests"] is False
    assert own_labels["Next your requests"] is True


def _panel() -> GrantsPanel:
    return GrantsPanel(
        mode="legacy",
        working_repo="example/work",
        has_pat=True,
        saved_state=True,
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
        is_admin=True,
        guild_id=123,
        channel_id=456,
        selected_agent=agent,
    )
    view = GitHubReposView(
        state, runtime=MagicMock(), allowed_user_id=7, agent=agent, panel=_panel()
    )
    labels = [item.label for item in view.walk_children() if isinstance(item, discord.ui.Button)]
    text = _embed_text(view)
    assert labels == ["◀ Back"]
    assert "This agent uses a saved GitHub key" in text


def test_discord_github_home_and_destructive_confirmations() -> None:
    agent = RosterAgent(name="helper", ma_agent_id="ag_helper", model_id="model", is_built_in=False)
    state = PanelState(
        roster=[],
        selected=None,
        account_id=uuid.uuid4(),
        is_admin=True,
        guild_id=123,
        channel_id=456,
        selected_agent=agent,
    )
    home = GitHubHomeView(state, runtime=MagicMock(), allowed_user_id=7, connected_count=2)
    assert isinstance(home, discord.ui.View)
    assert home.embed.title == "GitHub"
    assert home.embed.footer.text == "GitHub on Daimon"
    assert any(field.name == "Personal link" for field in home.embed.fields)
    assert home.to_components()
    labels = [item.label for item in home.walk_children() if isinstance(item, discord.ui.Button)]
    assert "Choose agent" in labels and "Connect more repos" in labels
    assert "Manage connected repos" in labels
    empty_home = GitHubHomeView(state, runtime=MagicMock(), allowed_user_id=7, connected_count=0)
    empty_labels = {
        item.label for item in empty_home.walk_children() if isinstance(item, discord.ui.Button)
    }
    assert "Connect GitHub" in empty_labels
    pending = GitHubHomeView(
        state,
        runtime=MagicMock(),
        allowed_user_id=7,
        connected_count=2,
        pending_url="https://example.invalid/connect",
    )
    pending_labels = [
        item.label for item in pending.walk_children() if isinstance(item, discord.ui.Button)
    ]
    assert "Connect GitHub" in pending_labels and "Start over" in pending_labels
    assert "Choose agent" not in pending_labels
    link = GitHubLinkView("https://example.invalid/connect")
    assert {item.label for item in link.children if isinstance(item, discord.ui.Button)} == {
        "Connect GitHub",
    }


@pytest.mark.asyncio
async def test_discord_setup_opens_embed_in_separate_private_message() -> None:
    state = PanelState(roster=[], selected=None, account_id=uuid.uuid4(), guild_id=123)
    runtime = MagicMock()
    source = PanelViewBase(state, runtime=runtime, allowed_user_id=7)
    target = GitHubHomeView(state, runtime=runtime, allowed_user_id=7, connected_count=0)
    interaction = MagicMock()
    interaction.response.is_done.return_value = True
    message = MagicMock()
    message.edit = AsyncMock()
    interaction.followup.send = AsyncMock(return_value=message)

    await source.swap_to(interaction, target)

    sent = interaction.followup.send.await_args.kwargs
    assert sent["embed"] is target.embed
    assert sent["ephemeral"] is True
    assert sent["wait"] is True
    await target.on_timeout()
    assert message.edit.await_args.kwargs["view"] is None


def test_discord_member_github_home_shows_only_personal_link() -> None:
    state = PanelState(roster=[], selected=None, account_id=uuid.uuid4(), guild_id=123)
    home = GitHubHomeView(
        state,
        runtime=MagicMock(),
        allowed_user_id=7,
        connected_count=0,
        linked_login="carlos",
    )
    labels = {item.label for item in home.walk_children() if isinstance(item, discord.ui.Button)}
    assert {"Use another account", "Unlink", "◀ Back"} <= labels
    assert "Connect GitHub" not in labels
    assert "Manage connected repos" not in labels


def test_discord_manage_connected_repos_requires_confirmation() -> None:
    state = PanelState(
        roster=[],
        selected=None,
        account_id=uuid.uuid4(),
        guild_id=123,
        channel_id=456,
    )
    repo = _connected_repo()
    listing = GitHubManageView(state, runtime=MagicMock(), allowed_user_id=7, repos=(repo,))
    labels = [item.label for item in listing.walk_children() if isinstance(item, discord.ui.Button)]
    assert "Change" in labels and "Disconnect" in labels
    confirm = GitHubManageView(
        state,
        runtime=MagicMock(),
        allowed_user_id=7,
        repos=(repo,),
        selected_id=repo.repo_id,
        step="disconnect",
    )
    text = _embed_text(confirm)
    assert "Disconnect example/work?\nAgents using it lose it." in text
    assert "◀ Back" in [
        item.label for item in confirm.walk_children() if isinstance(item, discord.ui.Button)
    ]


def test_discord_agent_settings_show_one_choice_at_a_time() -> None:
    agent = RosterAgent(name="helper", ma_agent_id="ag_helper", model_id="model", is_built_in=False)
    state = PanelState(
        roster=[], selected=None, account_id=uuid.uuid4(), guild_id=123, channel_id=456
    )
    panel = GrantsPanel(
        mode="app",
        working_repo="example/work",
        has_pat=False,
        repos=(
            RepoChoice(
                1,
                "example/work",
                "write",
                "write",
                "write",
                False,
                True,
                live_baseline="write",
                live_ceiling="write",
            ),
        ),
    )

    def labels(choice: str | None) -> set[str]:
        view = GitHubReposView(
            state,
            runtime=MagicMock(),
            allowed_user_id=7,
            agent=agent,
            panel=panel,
            settings=True,
            settings_choice=choice,  # type: ignore[arg-type]
        )
        return {item.label for item in view.walk_children() if isinstance(item, discord.ui.Button)}

    menu = labels(None)
    assert {
        "Change what it can do",
        "Remove repos",
        "Turn off GitHub for helper",
    } <= menu
    assert "Read only" not in menu
    assert "Read only" in labels("ability")
    assert "Change who can use these" not in menu
    assert "Remove repos" in labels("remove")


def test_discord_add_repos_defaults_working_repo_and_uses_one_screen() -> None:
    agent = RosterAgent(name="helper", ma_agent_id="ag_helper", model_id="model", is_built_in=False)
    state = PanelState(
        roster=[],
        selected=None,
        account_id=uuid.uuid4(),
        guild_id=123,
        channel_id=456,
        channel_name="readme",
        selected_agent=agent,
        is_admin=True,
    )
    panel = GrantsPanel(
        mode="legacy",
        working_repo="example/work",
        has_pat=False,
        repos=(
            RepoChoice(1, "example/work", "write", None, None, False, True),
            RepoChoice(2, "example/readme", "read", None, None, False, False),
        ),
    )
    pick = GitHubAddReposView(
        state, runtime=MagicMock(), allowed_user_id=7, agent=agent, panel=panel
    )
    selector = next(item for item in pick.walk_children() if isinstance(item, discord.ui.Select))
    assert [option.value for option in selector.options if option.default] == []
    labels = [item.label for item in pick.walk_children() if isinstance(item, discord.ui.Button)]
    assert "Add repos" in labels and "Change" in labels and "Review repos" not in labels
    text = _embed_text(pick)
    assert "Can: Read and write" in text
    assert "Used by:" not in text
    assert "Suggested: example/readme" in text
    assert "Add" in labels
    empty = GitHubAddReposView(
        state,
        runtime=MagicMock(),
        allowed_user_id=7,
        agent=agent,
        panel=GrantsPanel(mode="legacy", repos=(), working_repo=None, has_pat=False),
    )
    empty_labels = [
        item.label for item in empty.walk_children() if isinstance(item, discord.ui.Button)
    ]
    assert "Connect GitHub" in empty_labels
    assert "Add repos" not in empty_labels


@pytest.mark.asyncio
async def test_discord_agent_setup_shows_saved_key_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = "This agent uses a saved GitHub key. Ask your Daimon operator to switch it."
    monkeypatch.setattr(add_module, "is_guild_admin", lambda _interaction: True)
    monkeypatch.setattr(add_module, "sync_connect_admin", AsyncMock())
    monkeypatch.setattr(add_module, "connect_link", AsyncMock(side_effect=ValueError(message)))
    agent = RosterAgent(name="helper", ma_agent_id="ag_helper", model_id="model", is_built_in=False)
    state = PanelState(
        roster=[], selected=None, account_id=uuid.uuid4(), guild_id=123, is_admin=True
    )
    view = GitHubAddReposView(
        state,
        runtime=MagicMock(),
        allowed_user_id=7,
        agent=agent,
        panel=GrantsPanel(mode="legacy", repos=(), working_repo=None, has_pat=False),
    )
    interaction = MagicMock()
    interaction.guild_id = 123
    interaction.user.id = 7
    interaction.user.display_name = "Carlos"
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    await view._on_connect_more(interaction)
    assert interaction.followup.send.await_args.args[0] == message
    assert interaction.followup.send.await_args.kwargs["ephemeral"] is True


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
        queued_at=datetime.now(UTC),
        claimed_at=datetime.now(UTC),
    )
    claim = AsyncMock(return_value=NewRepoNoticeGroup(notices=(notice,)))
    finish = AsyncMock(return_value=True)
    monkeypatch.setattr(notice_module, "claim_notice_group", claim)
    monkeypatch.setattr(notice_module, "finish_notice", finish)
    monkeypatch.setattr(notice_module, "is_guild_admin", lambda _: True)
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=object())
    session.__aexit__ = AsyncMock(return_value=None)
    runtime = SimpleNamespace(
        sessionmaker=SimpleNamespace(begin=lambda: session),
        settings=SimpleNamespace(crypto=SimpleNamespace(keys=())),
    )
    interaction = MagicMock()
    interaction.user.id = 7
    interaction.followup.send = AsyncMock(
        side_effect=discord.HTTPException(MagicMock(status=500, reason="error"), "failed")
    )
    await notice_module.send_pending_notice(runtime, interaction, tenant_id=notice.tenant_id)
    assert finish.await_args.kwargs["delivered"] is False
    assert interaction.followup.send.await_args.kwargs["ephemeral"] is True


def test_discord_new_repo_card_hides_unverified_name() -> None:
    notice = NewRepoNotice(
        tenant_id=uuid.uuid4(),
        installation_id=9,
        repo_full_name="example/private",
        queued_at=datetime.now(UTC),
        claimed_at=datetime.now(UTC),
    )
    group = NewRepoNoticeGroup(notices=(notice,))
    generic = notice_module.NewRepoCard(MagicMock(), group, 7)
    assert [item.label for item in generic.children if isinstance(item, discord.ui.Button)] == [
        "Connect more repos",
        "Back",
    ]
    named = notice_module.NewRepoCard(MagicMock(), group, 7, visible_names=("example/private",))
    assert [item.label for item in named.children if isinstance(item, discord.ui.Button)] == [
        "Connect repo",
        "Not now",
        "Back",
    ]
