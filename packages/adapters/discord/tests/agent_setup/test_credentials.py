"""Tests for the /agent-setup Credentials sub-view V2 (Phase 51, SC1/SC3, D-08..D-12).

Hygiene assertions are first-class here: no test secret VALUE may appear in any
container TextDisplay content or in any captured log line.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
import structlog
from anthropic.types.beta import BetaManagedAgentsAgent, BetaManagedAgentsModelConfig
from daimon.adapters.discord.agent_setup import edit_view as edit_view_mod
from daimon.adapters.discord.agent_setup.credentials import (
    CredentialsSubView,
    PasteSecretModal,
    build_credentials_container,
)
from daimon.adapters.discord.agent_setup.edit_view import EditView
from daimon.adapters.discord.agent_setup.state import PanelState, RosterEntry
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core._models import Tenant
from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import DeploymentDefault
from daimon.core.specs import AgentSpec
from daimon.core.stores.agent_files import get_agent_file, list_agent_files, put_agent_file
from daimon.testing.ma import build_stub_anthropic
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_SECRET_VALUE = "super-secret-token-value-do-not-leak"


def _entry(name: str, *, is_system: bool = False) -> RosterEntry:
    return RosterEntry(
        name=name,
        model="claude-sonnet-4-6",
        spec=AgentSpec(name=name, model="claude-sonnet-4-6", system=None),
        is_system=is_system,
    )


def _state(entry: RosterEntry, account_id: uuid.UUID) -> PanelState:
    return PanelState(roster=[entry], selected=entry, account_id=account_id)


def _walk_buttons(view: discord.ui.LayoutView) -> list[discord.ui.Button[Any]]:
    """Walk the full LayoutView tree and collect all Button items."""
    return [item for item in view.walk_children() if isinstance(item, discord.ui.Button)]


def _walk_selects(view: discord.ui.LayoutView) -> list[discord.ui.Select[Any]]:
    """Walk the full LayoutView tree and collect all Select items."""
    return [item for item in view.walk_children() if isinstance(item, discord.ui.Select)]


def _walk_text_displays(view: discord.ui.LayoutView) -> list[discord.ui.TextDisplay[Any]]:
    """Walk the full LayoutView tree and collect all TextDisplay items."""
    return [item for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay)]


def _container_all_text(view: discord.ui.LayoutView) -> str:
    """Join all TextDisplay content strings from the view."""
    return "\n".join(td.content for td in _walk_text_displays(view))


def _find_buttons(view: discord.ui.LayoutView) -> list[discord.ui.Button[Any]]:
    return _walk_buttons(view)


def _button_by_label(view: discord.ui.LayoutView, label: str) -> discord.ui.Button[Any]:
    for btn in _walk_buttons(view):
        if btn.label == label:
            return btn
    raise AssertionError(f"no button labeled {label!r}")


def _remove_select(view: discord.ui.LayoutView) -> discord.ui.Select[Any]:
    selects = _walk_selects(view)
    assert len(selects) == 1, "the sub-view carries exactly one remove-select"
    return selects[0]


# --- build_credentials_container (pure) ------------------------------------


def test_container_header_and_subtext() -> None:
    view_container = build_credentials_container(
        agent_name="bot", secret_names=["XERO_API_KEY", "TOGGL_TOKEN"], is_system=False
    )
    texts = [
        child.content
        for child in view_container.children
        if isinstance(child, discord.ui.TextDisplay)
    ]
    # First TextDisplay is the header from layout.header()
    assert len(texts) >= 1, "at least one TextDisplay in container"
    header_text = texts[0]
    assert header_text.startswith("## 🔑 Secrets — "), f"header mismatch: {header_text!r}"
    assert "bot" in header_text, "agent name in header"
    assert "-# values are write-only; only key names are shown" in header_text, "subtext present"


def test_container_chips_on_one_line() -> None:
    container = build_credentials_container(
        agent_name="bot", secret_names=["A_KEY", "B_KEY"], is_system=False
    )
    # Collect text displays (skip the header which is first)
    displays = [
        child.content for child in container.children if isinstance(child, discord.ui.TextDisplay)
    ]
    # Second TextDisplay is the chips line (header is first display)
    chips_lines = [d for d in displays if "`A_KEY`" in d]
    assert len(chips_lines) == 1, "chips must be on exactly one TextDisplay line"
    chips = chips_lines[0]
    assert "`A_KEY`" in chips, "A_KEY chip present"
    assert "`B_KEY`" in chips, "B_KEY chip present"


def test_container_d09_values_never_reach_tree() -> None:
    """D-09: build_credentials_container takes names only; no secret value can appear."""
    container = build_credentials_container(
        agent_name="bot", secret_names=["XERO_API_KEY", "TOGGL_TOKEN"], is_system=False
    )
    all_text = " ".join(
        child.content for child in container.children if isinstance(child, discord.ui.TextDisplay)
    )
    assert _SECRET_VALUE not in all_text, "no secret value may appear in container"


def test_container_empty_state_shows_hint() -> None:
    container = build_credentials_container(agent_name="bot", secret_names=[], is_system=False)
    displays = [
        child.content for child in container.children if isinstance(child, discord.ui.TextDisplay)
    ]
    # The hint line should be a dim -# line
    hint_lines = [d for d in displays if "add your first secret" in d]
    assert len(hint_lines) == 1, "empty state has a hint line"
    assert hint_lines[0].startswith("-#"), "empty hint uses dim -# prefix"


def test_container_no_none_copy_in_empty_state() -> None:
    container = build_credentials_container(agent_name="bot", secret_names=[], is_system=False)
    displays = [
        child.content for child in container.children if isinstance(child, discord.ui.TextDisplay)
    ]
    for d in displays:
        assert "(none)" not in d, "empty state must not say (none)"


# --- CredentialsSubView item construction ----------------------------------


def test_subview_renders_remove_select_add_and_back(account_id: uuid.UUID) -> None:
    entry = _entry("bot")
    view = CredentialsSubView(
        runtime=MagicMock(spec=DiscordRuntime),
        state=_state(entry, account_id),
        allowed_user_id=42,
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        secret_names=["A", "B"],
        is_system=False,
    )
    select = _remove_select(view)
    assert select.placeholder == "✕ Remove a secret…", "single remove-select with house placeholder"
    assert [o.label for o in select.options] == ["✕ A", "✕ B"], "one option per secret"
    assert [o.value for o in select.options] == ["A", "B"], "option value is the key name"
    labels = [b.label for b in _find_buttons(view)]
    assert "+ Add secrets" in labels, "add button present (plural label)"
    assert "← Back" in labels, "back button present"
    add_btn = _button_by_label(view, "+ Add secrets")
    assert add_btn.disabled is False, "add enabled for a user agent under cap"


def test_subview_header_and_subtext(account_id: uuid.UUID) -> None:
    entry = _entry("my-bot")
    view = CredentialsSubView(
        runtime=MagicMock(spec=DiscordRuntime),
        state=_state(entry, account_id),
        allowed_user_id=42,
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        secret_names=["A"],
        is_system=False,
    )
    text = _container_all_text(view)
    assert "## 🔑 Secrets — " in text, "container header present"
    assert "my-bot" in text, "agent name in header"
    assert "-# values are write-only; only key names are shown" in text, "subtext present"


def test_subview_chips_on_one_line(account_id: uuid.UUID) -> None:
    entry = _entry("bot")
    view = CredentialsSubView(
        runtime=MagicMock(spec=DiscordRuntime),
        state=_state(entry, account_id),
        allowed_user_id=42,
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        secret_names=["A_KEY", "B_KEY"],
        is_system=False,
    )
    text_displays = _walk_text_displays(view)
    chips_displays = [td for td in text_displays if "`A_KEY`" in td.content]
    assert len(chips_displays) == 1, "chips must be on exactly one line"
    assert "`B_KEY`" in chips_displays[0].content, "both keys on the same line"


def test_subview_d09_values_never_reach_tree(account_id: uuid.UUID) -> None:
    """D-09 ported: constructor takes names only; no value string can appear in the view."""
    entry = _entry("bot")
    view = CredentialsSubView(
        runtime=MagicMock(spec=DiscordRuntime),
        state=_state(entry, account_id),
        allowed_user_id=42,
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        secret_names=["XERO_API_KEY"],
        is_system=False,
    )
    all_text = _container_all_text(view)
    assert _SECRET_VALUE not in all_text, "no secret value may appear anywhere in the view"


def test_subview_remove_select_option_carries_key_name_never_value(account_id: uuid.UUID) -> None:
    entry = _entry("bot")
    view = CredentialsSubView(
        runtime=MagicMock(spec=DiscordRuntime),
        state=_state(entry, account_id),
        allowed_user_id=42,
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        secret_names=["XERO_API_KEY"],
        is_system=False,
    )
    select = _remove_select(view)
    option = select.options[0]
    assert option.value == "XERO_API_KEY", "option value is the key name"
    assert "XERO_API_KEY" in (option.label or ""), "option label shows the key name"
    # The secret VALUE sentinel must appear in NO option value or label (D-09).
    for o in select.options:
        assert _SECRET_VALUE not in o.value, "option value never carries a secret value"
        assert _SECRET_VALUE not in (o.label or ""), "option label never carries a secret value"
    # No per-key custom_id leak: the key name is never persisted in a custom_id.
    assert select.custom_id is not None
    assert "XERO_API_KEY" not in select.custom_id, "key name never persisted in a custom_id"


def test_subview_system_agent_disables_mutations_but_not_back(account_id: uuid.UUID) -> None:
    entry = _entry("sys", is_system=True)
    view = CredentialsSubView(
        runtime=MagicMock(spec=DiscordRuntime),
        state=_state(entry, account_id),
        allowed_user_id=42,
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        secret_names=["A", "B"],
        is_system=True,
    )
    assert _remove_select(view).disabled is True, "system agents cannot remove secrets"
    assert _button_by_label(view, "+ Add secrets").disabled is True, "system add disabled"
    assert _button_by_label(view, "← Back").disabled is False, "back stays enabled (read-only)"


def test_subview_empty_state_disables_select(account_id: uuid.UUID) -> None:
    entry = _entry("bot")
    view = CredentialsSubView(
        runtime=MagicMock(spec=DiscordRuntime),
        state=_state(entry, account_id),
        allowed_user_id=42,
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        secret_names=[],
        is_system=False,
    )
    select = _remove_select(view)
    assert select.disabled is True, "no-secrets select is disabled"
    assert "no secrets" in (select.placeholder or "").lower(), "empty-state placeholder"


# --- PasteSecretModal: parse + validate + store (real DB) ------------------


@pytest.mark.asyncio
async def test_paste_modal_stores_each_pair_and_never_logs_value(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    account_id: uuid.UUID,
) -> None:
    _tid = derive_tenant_uuid(platform="discord", workspace_id="test-guild")
    tenant = Tenant(id=_tid, platform="discord", external_id="test-guild")
    db_session.add(tenant)
    await db_session.flush()
    agent_id = uuid.uuid4()

    runtime = DiscordRuntime(
        settings=MagicMock(),
        anthropic=build_stub_anthropic(),
        sessionmaker=db_session_factory,
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
    )
    on_added = AsyncMock()
    modal = PasteSecretModal(
        runtime=runtime, tenant_id=tenant.id, agent_id=agent_id, on_added=on_added
    )
    modal.content_input._value = (  # pyright: ignore[reportPrivateUsage]
        f"# a comment\nXERO_API_KEY={_SECRET_VALUE}\n\nTOGGL_TOKEN=second-value\n"
    )

    interaction = MagicMock()
    interaction.user.id = 42
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    cap = structlog.testing.LogCapture()
    structlog.configure(processors=[cap])
    try:
        await modal.on_submit(interaction)
    finally:
        structlog.reset_defaults()

    stored_xero = await get_agent_file(
        db_session, tenant_id=tenant.id, agent_id=agent_id, key="XERO_API_KEY"
    )
    stored_toggl = await get_agent_file(
        db_session, tenant_id=tenant.id, agent_id=agent_id, key="TOGGL_TOKEN"
    )
    assert stored_xero is not None and stored_xero.content == _SECRET_VALUE, (
        "first pair stored with its value"
    )
    assert stored_toggl is not None and stored_toggl.content == "second-value", (
        "second pair stored; blank + comment lines skipped"
    )

    toast = interaction.followup.send.call_args.args[0]
    assert "Added 2 secrets" in toast, "multi-key success copy"
    assert _SECRET_VALUE not in toast, "toast never echoes a value"
    on_added.assert_awaited_once()  # re-render callback fired after a successful paste

    for entry in cap.entries:
        assert _SECRET_VALUE not in repr(entry), "no log line may contain a secret value"


@pytest.mark.asyncio
async def test_paste_modal_rejects_invalid_key_and_writes_nothing(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _tid = derive_tenant_uuid(platform="discord", workspace_id="test-guild")
    tenant = Tenant(id=_tid, platform="discord", external_id="test-guild")
    db_session.add(tenant)
    await db_session.flush()
    agent_id = uuid.uuid4()

    runtime = DiscordRuntime(
        settings=MagicMock(),
        anthropic=build_stub_anthropic(),
        sessionmaker=db_session_factory,
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
    )
    modal = PasteSecretModal(
        runtime=runtime, tenant_id=tenant.id, agent_id=agent_id, on_added=AsyncMock()
    )
    modal.content_input._value = "123BAD=value\nGOOD_KEY=val"  # pyright: ignore[reportPrivateUsage]

    interaction = MagicMock()
    interaction.user.id = 42
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    await modal.on_submit(interaction)

    rows = await list_agent_files(db_session, tenant_id=tenant.id, agent_id=agent_id)
    assert rows == [], "fail-fast on an invalid key writes nothing"
    msg = interaction.followup.send.call_args.args[0]
    assert "Secret name must match" in msg, "invalid-key toast shown"


# --- ✕ remove (real DB) -----------------------------------------------------


@pytest.mark.asyncio
async def test_remove_deletes_the_key_and_rerenders(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    account_id: uuid.UUID,
) -> None:
    _tid = derive_tenant_uuid(platform="discord", workspace_id="test-guild")
    tenant = Tenant(id=_tid, platform="discord", external_id="test-guild")
    db_session.add(tenant)
    await db_session.flush()
    agent_id = uuid.uuid4()
    async with db_session_factory() as s, s.begin():
        await put_agent_file(
            s, tenant_id=tenant.id, agent_id=agent_id, key="XERO_API_KEY", content=_SECRET_VALUE
        )
        await put_agent_file(
            s, tenant_id=tenant.id, agent_id=agent_id, key="KEEP_ME", content="keep"
        )

    entry = _entry("bot")
    runtime = DiscordRuntime(
        settings=MagicMock(),
        anthropic=build_stub_anthropic(),
        sessionmaker=db_session_factory,
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
    )
    view = CredentialsSubView(
        runtime=runtime,
        state=_state(entry, account_id),
        allowed_user_id=42,
        tenant_id=tenant.id,
        agent_id=agent_id,
        secret_names=["XERO_API_KEY", "KEEP_ME"],
        is_system=False,
    )

    interaction = MagicMock()
    interaction.user.id = 42
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    interaction.edit_original_response = AsyncMock()

    # Drive the remove through the select callback (the new entry point).
    select = _remove_select(view)
    select._values = ["XERO_API_KEY"]  # pyright: ignore[reportPrivateUsage]  # simulate a user pick
    assert select.callback is not None
    await select.callback(interaction)

    rows = await list_agent_files(db_session, tenant_id=tenant.id, agent_id=agent_id)
    keys = [r.key for r in rows]
    assert keys == ["KEEP_ME"], "the targeted key is deleted; others remain"
    interaction.edit_original_response.assert_awaited()  # view re-rendered in place
    # The re-render view must not leak the surviving value (D-09).
    rerender_kwargs = interaction.edit_original_response.call_args.kwargs
    assert _SECRET_VALUE not in str(rerender_kwargs), "no value in re-render call kwargs"


# --- back navigation --------------------------------------------------------


@pytest.mark.asyncio
async def test_back_replaces_with_editview_in_place(account_id: uuid.UUID) -> None:
    entry = _entry("bot")
    settings = MagicMock()
    settings.mcp.public_url = None
    runtime = DiscordRuntime(
        settings=settings,
        anthropic=build_stub_anthropic(),
        sessionmaker=MagicMock(),
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
    )
    view = CredentialsSubView(
        runtime=runtime,
        state=_state(entry, account_id),
        allowed_user_id=42,
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        secret_names=["A"],
        is_system=False,
    )

    interaction = MagicMock()
    interaction.user.id = 42
    interaction.response.edit_message = AsyncMock()
    interaction.delete_original_response = AsyncMock()

    await view._on_back(interaction)  # pyright: ignore[reportPrivateUsage]

    interaction.response.edit_message.assert_awaited_once()  # back edits in place
    interaction.delete_original_response.assert_not_called()  # back must NOT delete (D-08)
    sent_view = interaction.response.edit_message.call_args.kwargs["view"]
    assert isinstance(sent_view, EditView), "back returns to the unified EditView"


# --- EditView._on_secrets opens the sub-view -------------------------------


@pytest.mark.asyncio
async def test_editview_secrets_button_opens_subview(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    account_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _tid = derive_tenant_uuid(platform="discord", workspace_id="test-guild")
    tenant = Tenant(id=_tid, platform="discord", external_id="test-guild")
    db_session.add(tenant)
    await db_session.flush()

    ma_agent_id = "agent_017abc"
    agent_id = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=ma_agent_id)
    async with db_session_factory() as s, s.begin():
        await put_agent_file(
            s, tenant_id=tenant.id, agent_id=agent_id, key="XERO_API_KEY", content=_SECRET_VALUE
        )

    now = dt.datetime.now(dt.UTC)
    real_agent = BetaManagedAgentsAgent(
        id=ma_agent_id,
        type="agent",
        name="bot",
        model=BetaManagedAgentsModelConfig(id="claude-sonnet-4-6"),
        metadata={},
        description=None,
        created_at=now,
        updated_at=now,
        version=1,
        mcp_servers=[],
        skills=[],
        tools=[],
        system=None,
    )

    async def fake_find(*_a: Any, **_k: Any) -> BetaManagedAgentsAgent:
        return real_agent

    monkeypatch.setattr(edit_view_mod, "find_agent_by_daimon_tag", fake_find)
    monkeypatch.setattr(edit_view_mod, "_resolve_tenant", AsyncMock(return_value=tenant.id))

    settings = MagicMock()
    settings.mcp.public_url = None
    runtime = DiscordRuntime(
        settings=settings,
        anthropic=build_stub_anthropic(),
        sessionmaker=db_session_factory,
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
    )
    entry = _entry("bot")
    edit_view = EditView(_state(entry, account_id), runtime=runtime, allowed_user_id=42)

    interaction = MagicMock()
    interaction.user.id = 42
    interaction.response.edit_message = AsyncMock()
    interaction.guild_id = 123

    await edit_view._on_secrets(interaction)  # pyright: ignore[reportPrivateUsage]

    interaction.response.edit_message.assert_awaited_once()  # secrets opens the sub-view in place
    kwargs = interaction.response.edit_message.call_args.kwargs
    assert isinstance(kwargs["view"], CredentialsSubView), "view is the CredentialsSubView"
    # D-09: the sub-view's container must not contain the secret value
    all_text = _container_all_text(kwargs["view"])
    assert _SECRET_VALUE not in all_text, "the opened view lists the key masked, never its value"


def test_editview_has_secrets_button_disabled_for_system_agent(account_id: uuid.UUID) -> None:
    settings = MagicMock()
    settings.mcp.public_url = None
    runtime = DiscordRuntime(
        settings=settings,
        anthropic=build_stub_anthropic(),
        sessionmaker=MagicMock(),
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
    )
    sys_entry = RosterEntry(
        name="sys",
        model="claude-sonnet-4-6",
        spec=AgentSpec(name="sys", model="claude-sonnet-4-6", system=None),
        is_system=True,
    )
    sys_view = EditView(_state(sys_entry, account_id), runtime=runtime, allowed_user_id=42)
    sys_buttons = {b.label: b for b in _walk_buttons(sys_view) if b.label is not None}
    assert sys_buttons["Secrets"].disabled is True, (
        "system agents see the Secrets button disabled (defensive, D-11)"
    )

    user_entry = RosterEntry(
        name="bot",
        model="claude-sonnet-4-6",
        spec=AgentSpec(name="bot", model="claude-sonnet-4-6", system=None),
        is_system=False,
    )
    user_view = EditView(_state(user_entry, account_id), runtime=runtime, allowed_user_id=42)
    user_buttons = {b.label: b for b in _walk_buttons(user_view) if b.label is not None}
    assert user_buttons["Secrets"].disabled is False, "user agents can open Secrets"
