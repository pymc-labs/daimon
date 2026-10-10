"""Discord GitHub panels keep links and repo changes private."""

from __future__ import annotations

import dataclasses
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.discord.agent_setup import github_new_repo as notice_module
from daimon.adapters.discord.agent_setup import github_repos as repos_module
from daimon.adapters.discord.agent_setup import github_requests as request_module
from daimon.adapters.discord.agent_setup.github_home import GitHubHomeView
from daimon.adapters.discord.agent_setup.github_repos import GitHubReposView
from daimon.adapters.discord.agent_setup.github_waiting import GitHubWaitingView
from daimon.adapters.discord.agent_setup.state import PanelState
from daimon.adapters.discord.commands.github import GitHubCog
from daimon.core.github_panel import GrantsPanel, RepoChoice
from daimon.core.operation_policy import TargetFacts
from daimon.core.roster import RosterAgent
from daimon.core.stores.github_access import AuthorizedRepo
from daimon.core.stores.github_access_requests import AccessRequest
from daimon.core.stores.github_new_repo_notices import NewRepoNotice, NewRepoNoticeGroup


@pytest.mark.asyncio
async def test_new_repo_notice_defers_before_database_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = MagicMock()
    interaction.data = {"custom_id": f"github_notice:{uuid.uuid4()}:2026-10-08:connect"}
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    @asynccontextmanager
    async def sessionmaker():  # type: ignore[no-untyped-def]
        yield object()

    async def get_tenant(_session: object, _tenant_id: uuid.UUID) -> None:
        interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)

    monkeypatch.setattr(notice_module, "get_tenant", get_tenant)
    monkeypatch.setattr(notice_module, "notices_for_day", AsyncMock(return_value=None))
    runtime = SimpleNamespace(sessionmaker=sessionmaker)
    assert await notice_module.handle_dm_notice(interaction, runtime)  # type: ignore[arg-type]
    interaction.followup.send.assert_awaited_once_with("This card is unavailable.", ephemeral=True)


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


def _helper_state(**changes: object) -> PanelState:
    agent = RosterAgent(name="helper", ma_agent_id="ag_helper", model_id="model", is_built_in=False)
    values: dict[str, object] = {
        "roster": [],
        "selected": None,
        "account_id": uuid.uuid4(),
        "is_admin": True,
        "guild_id": 123,
        "channel_id": 456,
        "selected_agent": agent,
    }
    values.update(changes)
    return PanelState(**values)  # type: ignore[arg-type]


def _live_panel(*names: str, ceiling: str = "write") -> GrantsPanel:
    return GrantsPanel(
        mode="app",
        working_repo=None,
        has_pat=False,
        repos=tuple(
            RepoChoice(
                index,
                name,
                "write",
                ceiling,  # type: ignore[arg-type]
                ceiling,  # type: ignore[arg-type]
                False,
                False,
                live_baseline=ceiling,  # type: ignore[arg-type]
                live_ceiling=ceiling,  # type: ignore[arg-type]
            )
            for index, name in enumerate(names, start=1)
        ),
    )


def _labels(view: discord.ui.LayoutView) -> list[str]:
    return [
        item.label or "" for item in view.walk_children() if isinstance(item, discord.ui.Button)
    ]


def test_discord_github_panels_are_read_only() -> None:
    agent = RosterAgent(name="helper", ma_agent_id="ag_helper", model_id="model", is_built_in=False)
    home = GitHubHomeView(
        _helper_state(),
        runtime=MagicMock(),
        allowed_user_id=7,
        agent_counts=((agent, 2),),
        linked_login="carlos",
    )
    assert "helper: 2 repos" in _embed_text(home)
    assert _labels(home) == ["Connect GitHub", "◀ Back"]
    panel = dataclasses.replace(_live_panel("ana/thesis", "ben/scraper"), working_repo="ana/thesis")
    view = GitHubReposView(
        _helper_state(),
        runtime=MagicMock(),
        allowed_user_id=7,
        agent=agent,
        panel=panel,
        connect_url="https://example.invalid/oauth/github/connect/token",
    )
    text = _embed_text(view)
    assert "ana/thesis\nben/scraper" in text
    assert "Working repo: ana/thesis" in text
    assert "To change repos, ask helper in chat." in text
    assert " · " not in text
    assert _labels(view) == ["Connect GitHub", "◀ Back"]
    empty = GitHubReposView(
        _helper_state(),
        runtime=MagicMock(),
        allowed_user_id=7,
        agent=agent,
        panel=GrantsPanel(mode="app", repos=(), working_repo=None, has_pat=False),
    )
    assert "No working repo" in _embed_text(empty)
    for stale in ("_on_remove", "_on_confirm", "_on_deactivate", "_setter", "_on_activate"):
        assert not hasattr(view, stale)


@asynccontextmanager
async def _fake_begin():  # type: ignore[no-untyped-def]
    yield MagicMock()


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
