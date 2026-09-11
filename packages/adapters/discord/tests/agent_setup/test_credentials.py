"""Tests for the /agent-setup Credentials sub-view V2.

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
from daimon.adapters.discord.agent_setup import credentials as credentials_mod
from daimon.adapters.discord.agent_setup import edit_view as edit_view_mod
from daimon.adapters.discord.agent_setup.credentials import (
    CredentialsSubView,
    PasteSecretModal,
    build_credentials_container,
    format_paste_result,
)
from daimon.adapters.discord.agent_setup.edit_view import EditView
from daimon.adapters.discord.agent_setup.state import PanelState, RosterEntry
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import DeploymentDefault
from daimon.core.specs import AgentSpec
from daimon.core.stores.agent_files import get_agent_file, list_agent_files, put_agent_file
from daimon.testing.factories import make_tenant
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


def _interaction(user_id: int = 42, *, is_admin: bool = True, guild_id: int = 12345) -> MagicMock:
    """A discord.Interaction stand-in, a live guild admin by default so the
    env-var write gate short-circuits without a DB read. Tests proving the
    shared-agent refusal pass ``is_admin=False`` and register a tenant for the
    guild, because that path reads reachability from Postgres."""
    interaction = MagicMock()
    interaction.guild_id = guild_id
    interaction.user = MagicMock(spec=discord.Member)
    interaction.user.id = user_id
    interaction.user.guild_permissions.administrator = is_admin
    interaction.user.guild_permissions.manage_guild = False
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.send_modal = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    interaction.response.is_done = MagicMock(return_value=False)
    interaction.followup.send = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    return interaction


def _runtime(
    sessionmaker: Any,
    *,
    deployment_default: DeploymentDefault | None = None,
) -> DiscordRuntime:
    settings = MagicMock()
    settings.mcp.public_url = None
    return DiscordRuntime(
        settings=settings,
        anthropic=build_stub_anthropic(),
        sessionmaker=sessionmaker,
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=deployment_default or DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
        turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]  # never runs a turn
    )


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
        agent_name="bot", secret_names=["XERO_API_KEY", "TOGGL_TOKEN"]
    )
    texts = [
        child.content
        for child in view_container.children
        if isinstance(child, discord.ui.TextDisplay)
    ]
    # First TextDisplay is the header from layout.header()
    assert len(texts) >= 1, "at least one TextDisplay in container"
    header_text = texts[0]
    assert header_text.startswith("## 🔑 Keys — "), f"header mismatch: {header_text!r}"
    assert "bot" in header_text, "agent name in header"
    assert "-# values are write-only; only key names are shown" in header_text, "subtext present"


def test_container_chips_on_one_line() -> None:
    container = build_credentials_container(agent_name="bot", secret_names=["A_KEY", "B_KEY"])
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
    """build_credentials_container takes names only; no secret value can appear."""
    container = build_credentials_container(
        agent_name="bot", secret_names=["XERO_API_KEY", "TOGGL_TOKEN"]
    )
    all_text = " ".join(
        child.content for child in container.children if isinstance(child, discord.ui.TextDisplay)
    )
    assert _SECRET_VALUE not in all_text, "no secret value may appear in container"


def test_container_empty_state_shows_hint() -> None:
    container = build_credentials_container(agent_name="bot", secret_names=[])
    displays = [
        child.content for child in container.children if isinstance(child, discord.ui.TextDisplay)
    ]
    # The hint line should be a dim -# line
    hint_lines = [d for d in displays if "add your first key" in d]
    assert len(hint_lines) == 1, "empty state has a hint line"
    assert hint_lines[0].startswith("-#"), "empty hint uses dim -# prefix"


def test_container_no_none_copy_in_empty_state() -> None:
    container = build_credentials_container(agent_name="bot", secret_names=[])
    displays = [
        child.content for child in container.children if isinstance(child, discord.ui.TextDisplay)
    ]
    for d in displays:
        assert "(none)" not in d, "empty state must not say (none)"


# --- the post-paste result line (pure) -------------------------------------


def test_format_paste_result_is_singular_for_one_key_and_plural_above_one() -> None:
    assert format_paste_result(key_count=1) == "Saved ✓ — 1 key set", (
        "one key reads as a singular key"
    )
    assert format_paste_result(key_count=3) == "Saved ✓ — 3 keys set", (
        "more than one key pluralises"
    )


def test_container_appends_the_result_line_after_the_chips() -> None:
    container = build_credentials_container(
        agent_name="bot",
        secret_names=["A_KEY", "B_KEY"],
        result_line=format_paste_result(key_count=2),
    )
    displays = [
        child.content for child in container.children if isinstance(child, discord.ui.TextDisplay)
    ]
    chips_index = next(i for i, d in enumerate(displays) if "`A_KEY`" in d)
    result_index = next(i for i, d in enumerate(displays) if d == "Saved ✓ — 2 keys set")
    assert result_index > chips_index, "the result line renders after the chips, not before them"


def test_container_result_line_never_carries_a_value() -> None:
    """The result line is built from a count, so no value can ride along."""
    container = build_credentials_container(
        agent_name="bot",
        secret_names=["XERO_API_KEY"],
        result_line=format_paste_result(key_count=1),
    )
    all_text = " ".join(
        child.content for child in container.children if isinstance(child, discord.ui.TextDisplay)
    )
    assert _SECRET_VALUE not in all_text, "no secret value may appear in the collapsed container"
    assert "XERO_API_KEY" in all_text, "the key name is what the collapsed container shows"


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
    )
    select = _remove_select(view)
    assert select.placeholder == "✕ Remove a key…", "single remove-select with house placeholder"
    assert [o.label for o in select.options] == ["✕ A", "✕ B"], "one option per secret"
    assert [o.value for o in select.options] == ["A", "B"], "option value is the key name"
    labels = [b.label for b in _find_buttons(view)]
    assert "+ Add keys" in labels, "add button present (plural label)"
    assert "← Back" in labels, "back button present"
    add_btn = _button_by_label(view, "+ Add keys")
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
    )
    text = _container_all_text(view)
    assert "## 🔑 Keys — " in text, "container header present"
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
    )
    text_displays = _walk_text_displays(view)
    chips_displays = [td for td in text_displays if "`A_KEY`" in td.content]
    assert len(chips_displays) == 1, "chips must be on exactly one line"
    assert "`B_KEY`" in chips_displays[0].content, "both keys on the same line"


def test_subview_d09_values_never_reach_tree(account_id: uuid.UUID) -> None:
    """Constructor takes names only; no value string can appear in the view."""
    entry = _entry("bot")
    view = CredentialsSubView(
        runtime=MagicMock(spec=DiscordRuntime),
        state=_state(entry, account_id),
        allowed_user_id=42,
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        secret_names=["XERO_API_KEY"],
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
    )
    select = _remove_select(view)
    option = select.options[0]
    assert option.value == "XERO_API_KEY", "option value is the key name"
    assert "XERO_API_KEY" in (option.label or ""), "option label shows the key name"
    # The secret VALUE sentinel must appear in NO option value or label.
    for o in select.options:
        assert _SECRET_VALUE not in o.value, "option value never carries a secret value"
        assert _SECRET_VALUE not in (o.label or ""), "option label never carries a secret value"
    # No per-key custom_id leak: the key name is never persisted in a custom_id.
    assert select.custom_id is not None
    assert "XERO_API_KEY" not in select.custom_id, "key name never persisted in a custom_id"


def test_subview_system_agent_enables_mutations(account_id: uuid.UUID) -> None:
    """Keys are per-agent daimon state, never part of the agent spec, so a
    system agent's remove select and add button are enabled exactly like a
    user agent's — provenance does not gate this sub-view's rendering. The
    enabled state is deliberate: the refusal happens at click time and carries
    the reason, which a greyed control cannot."""
    entry = _entry("sys", is_system=True)
    view = CredentialsSubView(
        runtime=MagicMock(spec=DiscordRuntime),
        state=_state(entry, account_id),
        allowed_user_id=42,
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        secret_names=["A", "B"],
    )
    assert _remove_select(view).disabled is False, (
        "a system agent's remove select must be enabled when it has variables"
    )
    assert _button_by_label(view, "+ Add keys").disabled is False, (
        "a system agent's add button must be enabled below the cap"
    )
    assert _button_by_label(view, "← Back").disabled is False, "back stays enabled"


def test_subview_empty_state_disables_select(account_id: uuid.UUID) -> None:
    entry = _entry("bot")
    view = CredentialsSubView(
        runtime=MagicMock(spec=DiscordRuntime),
        state=_state(entry, account_id),
        allowed_user_id=42,
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        secret_names=[],
    )
    select = _remove_select(view)
    assert select.disabled is True, "no-secrets select is disabled"
    assert "no keys" in (select.placeholder or "").lower(), "empty-state placeholder"


# --- PasteSecretModal: parse + validate + store (real DB) ------------------


@pytest.mark.asyncio
async def test_paste_modal_stores_each_pair_and_never_logs_value(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    account_id: uuid.UUID,
) -> None:
    tenant = await make_tenant(db_session, platform="discord", workspace_id="test-guild")
    _tid = tenant.id
    agent_id = uuid.uuid4()

    runtime = _runtime(db_session_factory)
    on_added = AsyncMock()
    modal = PasteSecretModal(
        runtime=runtime,
        tenant_id=tenant.id,
        agent_id=agent_id,
        entry=_entry("bot"),
        on_added=on_added,
    )
    modal.content_input._value = (  # pyright: ignore[reportPrivateUsage]
        f"# a comment\nXERO_API_KEY={_SECRET_VALUE}\n\nTOGGL_TOKEN=second-value\n"
    )

    interaction = _interaction()

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
    assert "Added 2 keys" in toast, "multi-key success copy"
    assert _SECRET_VALUE not in toast, "toast never echoes a value"
    # The re-render callback fires after a successful paste, carrying the COUNT
    # the collapsed render needs — never a key name and never a value.
    on_added.assert_awaited_once_with(interaction, key_count=2)

    for entry in cap.entries:
        assert _SECRET_VALUE not in repr(entry), "no log line may contain a secret value"


@pytest.mark.asyncio
async def test_paste_modal_rejects_invalid_key_and_writes_nothing(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session, platform="discord", workspace_id="test-guild")
    _tid = tenant.id
    agent_id = uuid.uuid4()

    runtime = _runtime(db_session_factory)
    modal = PasteSecretModal(
        runtime=runtime,
        tenant_id=tenant.id,
        agent_id=agent_id,
        entry=_entry("bot"),
        on_added=AsyncMock(),
    )
    modal.content_input._value = "123BAD=value\nGOOD_KEY=val"  # pyright: ignore[reportPrivateUsage]

    interaction = _interaction()

    await modal.on_submit(interaction)

    rows = await list_agent_files(db_session, tenant_id=tenant.id, agent_id=agent_id)
    assert rows == [], "fail-fast on an invalid key writes nothing"
    msg = interaction.followup.send.call_args.args[0]
    assert "Key name must match" in msg, "invalid-key toast shown"


# --- PasteSecretModal: click-time gate on a shared agent -------------------


@pytest.mark.asyncio
async def test_paste_modal_refuses_write_on_reachable_agent_for_non_admin(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """put_agent_file is an upsert and its files are mounted read-write on every
    session of the agent, so a non-admin pasting over an agent the workspace
    currently resolves to would silently replace secrets everyone depends on."""
    guild_id = 920401
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, platform="discord", workspace_id=str(guild_id))
    agent_id = uuid.uuid4()

    entry = _entry("bot")
    runtime = _runtime(db_session_factory, deployment_default=DeploymentDefault(agent_name="bot"))
    modal = PasteSecretModal(
        runtime=runtime,
        tenant_id=tenant.id,
        agent_id=agent_id,
        entry=entry,
        on_added=AsyncMock(),
    )
    modal.content_input._value = f"XERO_API_KEY={_SECRET_VALUE}"  # pyright: ignore[reportPrivateUsage]

    interaction = _interaction(is_admin=False, guild_id=guild_id)
    await modal.on_submit(interaction)

    async with db_session_factory() as session:
        rows = await list_agent_files(session, tenant_id=tenant.id, agent_id=agent_id)
    assert rows == [], "a non-admin must write no key onto a currently-reachable agent"
    interaction.response.defer.assert_not_called()
    interaction.response.send_message.assert_called_once()
    assert interaction.response.send_message.call_args.kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_paste_modal_refuses_write_on_system_agent_for_non_admin(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The deployment's built-in agent is shared by everyone in the install
    whether or not anything currently scopes to it, so its keys are closed
    to non-admins on provenance alone."""
    guild_id = 920402
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, platform="discord", workspace_id=str(guild_id))
    agent_id = uuid.uuid4()

    runtime = _runtime(db_session_factory)
    modal = PasteSecretModal(
        runtime=runtime,
        tenant_id=tenant.id,
        agent_id=agent_id,
        entry=_entry("daimon", is_system=True),
        on_added=AsyncMock(),
    )
    modal.content_input._value = f"XERO_API_KEY={_SECRET_VALUE}"  # pyright: ignore[reportPrivateUsage]

    interaction = _interaction(is_admin=False, guild_id=guild_id)
    await modal.on_submit(interaction)

    async with db_session_factory() as session:
        rows = await list_agent_files(session, tenant_id=tenant.id, agent_id=agent_id)
    assert rows == [], "a non-admin must write no key onto the built-in agent"
    interaction.response.defer.assert_not_called()
    interaction.response.send_message.assert_called_once()
    assert interaction.response.send_message.call_args.kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_paste_modal_still_writes_on_reachable_system_agent_for_admin(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Setting the built-in default agent's variables is how an admin finishes
    onboarding, and that agent is both a system agent and the workspace's
    default. The gate must not close that path."""
    guild_id = 920403
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, platform="discord", workspace_id=str(guild_id))
    agent_id = uuid.uuid4()

    runtime = _runtime(
        db_session_factory, deployment_default=DeploymentDefault(agent_name="daimon")
    )
    modal = PasteSecretModal(
        runtime=runtime,
        tenant_id=tenant.id,
        agent_id=agent_id,
        entry=_entry("daimon", is_system=True),
        on_added=AsyncMock(),
    )
    modal.content_input._value = f"XERO_API_KEY={_SECRET_VALUE}"  # pyright: ignore[reportPrivateUsage]

    interaction = _interaction(is_admin=True, guild_id=guild_id)
    await modal.on_submit(interaction)

    async with db_session_factory() as session:
        stored = await get_agent_file(
            session, tenant_id=tenant.id, agent_id=agent_id, key="XERO_API_KEY"
        )
    assert stored is not None and stored.content == _SECRET_VALUE, (
        "an admin must still set the built-in default agent's keys"
    )


@pytest.mark.asyncio
async def test_paste_modal_refusal_logs_no_key_names_or_values(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The module's hygiene invariant holds on the refusal path too: the gate
    runs before the pasted content is read, so neither the key names nor the
    values reach the log stream."""
    guild_id = 920404
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, platform="discord", workspace_id=str(guild_id))

    runtime = _runtime(db_session_factory)
    modal = PasteSecretModal(
        runtime=runtime,
        tenant_id=tenant.id,
        agent_id=uuid.uuid4(),
        entry=_entry("daimon", is_system=True),
        on_added=AsyncMock(),
    )
    modal.content_input._value = (  # pyright: ignore[reportPrivateUsage]
        f"XERO_API_KEY={_SECRET_VALUE}\nTOGGL_TOKEN=second-value"
    )

    interaction = _interaction(is_admin=False, guild_id=guild_id)
    cap = structlog.testing.LogCapture()
    structlog.configure(processors=[cap])
    try:
        await modal.on_submit(interaction)
    finally:
        structlog.reset_defaults()

    for captured in cap.entries:
        rendered = repr(captured)
        assert _SECRET_VALUE not in rendered, "a refused paste must not log a secret value"
        assert "XERO_API_KEY" not in rendered, "a refused paste must not log a key name either"
        assert "TOGGL_TOKEN" not in rendered, "a refused paste must not log a key name either"


# --- paste → panel collapse (real DB, full wiring) -------------------------


@pytest.mark.asyncio
async def test_paste_success_collapses_the_add_control_on_the_panel(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    account_id: uuid.UUID,
) -> None:
    """After a successful paste the panel stops asking for more input: the add
    control is gone, a result line names the count, and the freshly pasted keys
    are in the chips and the remove select. Remove and ← Back stay live."""
    tenant = await make_tenant(db_session, platform="discord", workspace_id="test-guild")
    agent_id = uuid.uuid4()

    view = CredentialsSubView(
        runtime=_runtime(db_session_factory),
        state=_state(_entry("bot"), account_id),
        allowed_user_id=42,
        tenant_id=tenant.id,
        agent_id=agent_id,
        secret_names=[],
    )

    # Two distinct interactions, as in production: the click that opens the
    # modal, then the modal submit.
    click_interaction = _interaction()
    await view._on_add(click_interaction)  # pyright: ignore[reportPrivateUsage]
    modal = click_interaction.response.send_modal.call_args.args[0]
    modal.content_input._value = (  # pyright: ignore[reportPrivateUsage]
        f"XERO_API_KEY={_SECRET_VALUE}\nTOGGL_TOKEN=second-value"
    )

    submit_interaction = _interaction()
    await modal.on_submit(submit_interaction)

    submit_interaction.edit_original_response.assert_awaited_once()
    panel = submit_interaction.edit_original_response.call_args.kwargs["view"]
    labels = [b.label for b in _walk_buttons(panel)]
    assert "+ Add keys" not in labels, "the add control is swapped out, not left live"
    assert "← Back" in labels, "← Back stays live on the collapsed panel"
    select = _remove_select(panel)
    assert select.disabled is False, "the remove select stays live on the collapsed panel"
    assert sorted(o.value for o in select.options) == ["TOGGL_TOKEN", "XERO_API_KEY"], (
        "the reload puts the freshly pasted keys into the remove select"
    )
    text = _container_all_text(panel)
    assert "Saved ✓ — 2 keys set" in text, "the result line names the count"
    assert "XERO_API_KEY" in text and "TOGGL_TOKEN" in text, "the chips list both pasted keys"


@pytest.mark.asyncio
async def test_paste_success_acks_as_a_message_update_so_the_panel_is_the_edit_target(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    account_id: uuid.UUID,
) -> None:
    """Regression guard on the ack TYPE, which is the whole mechanism.

    A `defer(ephemeral=True, thinking=True)` on a modal submit is a
    deferred_channel_message, which repoints this interaction's `@original` at
    a brand-new ephemeral message — so `edit_original_response` below would
    render a duplicate panel into the ex-spinner and the real panel would never
    change. A bare `defer()` is a deferred_message_update, whose `@original` is
    the message the modal was opened from: the panel."""
    tenant = await make_tenant(db_session, platform="discord", workspace_id="test-guild")

    view = CredentialsSubView(
        runtime=_runtime(db_session_factory),
        state=_state(_entry("bot"), account_id),
        allowed_user_id=42,
        tenant_id=tenant.id,
        agent_id=uuid.uuid4(),
        secret_names=[],
    )

    click_interaction = _interaction()
    await view._on_add(click_interaction)  # pyright: ignore[reportPrivateUsage]
    modal = click_interaction.response.send_modal.call_args.args[0]
    modal.content_input._value = f"XERO_API_KEY={_SECRET_VALUE}"  # pyright: ignore[reportPrivateUsage]

    submit_interaction = _interaction()
    await modal.on_submit(submit_interaction)

    assert submit_interaction.response.defer.call_args.args == (), (
        "the modal submit must ack with a bare defer() — no thinking, no ephemeral"
    )
    assert submit_interaction.response.defer.call_args.kwargs == {}, (
        "the modal submit must ack with a bare defer() — no thinking, no ephemeral"
    )
    submit_interaction.followup.send.assert_awaited()  # the toast still works after a type-6 ack


@pytest.mark.asyncio
async def test_paste_failure_leaves_the_panel_uncollapsed(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    account_id: uuid.UUID,
) -> None:
    """A rejected paste wrote nothing, so the panel must keep its add control."""
    tenant = await make_tenant(db_session, platform="discord", workspace_id="test-guild")

    view = CredentialsSubView(
        runtime=_runtime(db_session_factory),
        state=_state(_entry("bot"), account_id),
        allowed_user_id=42,
        tenant_id=tenant.id,
        agent_id=uuid.uuid4(),
        secret_names=[],
    )

    click_interaction = _interaction()
    await view._on_add(click_interaction)  # pyright: ignore[reportPrivateUsage]
    modal = click_interaction.response.send_modal.call_args.args[0]
    modal.content_input._value = "123BAD=value\nGOOD_KEY=val"  # pyright: ignore[reportPrivateUsage]

    submit_interaction = _interaction()
    await modal.on_submit(submit_interaction)

    submit_interaction.edit_original_response.assert_not_awaited()
    assert "Key name must match" in submit_interaction.followup.send.call_args.args[0], (
        "the invalid-key toast is the only feedback on a failed paste"
    )


@pytest.mark.asyncio
async def test_collapsed_panel_renders_key_names_never_values(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    account_id: uuid.UUID,
) -> None:
    """The collapse path is the newest place a value could leak, so the
    module's hygiene invariant is asserted directly on it: the key name shows,
    the value does not — not in the container, not in the edit call."""
    tenant = await make_tenant(db_session, platform="discord", workspace_id="test-guild")

    view = CredentialsSubView(
        runtime=_runtime(db_session_factory),
        state=_state(_entry("bot"), account_id),
        allowed_user_id=42,
        tenant_id=tenant.id,
        agent_id=uuid.uuid4(),
        secret_names=[],
    )

    click_interaction = _interaction()
    await view._on_add(click_interaction)  # pyright: ignore[reportPrivateUsage]
    modal = click_interaction.response.send_modal.call_args.args[0]
    modal.content_input._value = (  # pyright: ignore[reportPrivateUsage]
        f"XERO_API_KEY={_SECRET_VALUE}\nTOGGL_TOKEN=second-value"
    )

    submit_interaction = _interaction()
    await modal.on_submit(submit_interaction)

    all_text = _container_all_text(view)
    assert "XERO_API_KEY" in all_text, "the collapsed panel shows the key name"
    assert _SECRET_VALUE not in all_text, "no secret value may appear in the collapsed panel"
    assert _SECRET_VALUE not in str(submit_interaction.edit_original_response.call_args.kwargs), (
        "no secret value may ride along in the panel edit call"
    )


@pytest.mark.asyncio
async def test_remove_after_a_paste_restores_the_add_control_and_drops_the_result_line(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    account_id: uuid.UUID,
) -> None:
    """The result line is a per-render argument, never instance state: the next
    mutation on the same view instance must render the add control back and no
    `Saved ✓`. Stored on the view, this test fails with a stale result line."""
    tenant = await make_tenant(db_session, platform="discord", workspace_id="test-guild")

    view = CredentialsSubView(
        runtime=_runtime(db_session_factory),
        state=_state(_entry("bot"), account_id),
        allowed_user_id=42,
        tenant_id=tenant.id,
        agent_id=uuid.uuid4(),
        secret_names=[],
    )

    click_interaction = _interaction()
    await view._on_add(click_interaction)  # pyright: ignore[reportPrivateUsage]
    modal = click_interaction.response.send_modal.call_args.args[0]
    modal.content_input._value = (  # pyright: ignore[reportPrivateUsage]
        f"XERO_API_KEY={_SECRET_VALUE}\nTOGGL_TOKEN=second-value"
    )
    await modal.on_submit(_interaction())

    remove_interaction = _interaction()
    select = _remove_select(view)
    select._values = ["XERO_API_KEY"]  # pyright: ignore[reportPrivateUsage]  # simulate a user pick
    assert select.callback is not None
    await select.callback(remove_interaction)

    panel = remove_interaction.edit_original_response.call_args.kwargs["view"]
    labels = [b.label for b in _walk_buttons(panel)]
    assert "+ Add keys" in labels, "a removal renders the add control back"
    assert "Saved ✓" not in _container_all_text(panel), (
        "the paste result line must not stick around into the next render"
    )


# --- ✕ remove (real DB) -----------------------------------------------------


@pytest.mark.asyncio
async def test_remove_deletes_the_key_and_rerenders(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    account_id: uuid.UUID,
) -> None:
    tenant = await make_tenant(db_session, platform="discord", workspace_id="test-guild")
    _tid = tenant.id
    agent_id = uuid.uuid4()
    async with db_session_factory() as s, s.begin():
        await put_agent_file(
            s, tenant_id=tenant.id, agent_id=agent_id, key="XERO_API_KEY", content=_SECRET_VALUE
        )
        await put_agent_file(
            s, tenant_id=tenant.id, agent_id=agent_id, key="KEEP_ME", content="keep"
        )

    entry = _entry("bot")
    runtime = _runtime(db_session_factory)
    view = CredentialsSubView(
        runtime=runtime,
        state=_state(entry, account_id),
        allowed_user_id=42,
        tenant_id=tenant.id,
        agent_id=agent_id,
        secret_names=["XERO_API_KEY", "KEEP_ME"],
    )

    interaction = _interaction()

    # Drive the remove through the select callback (the new entry point).
    select = _remove_select(view)
    select._values = ["XERO_API_KEY"]  # pyright: ignore[reportPrivateUsage]  # simulate a user pick
    assert select.callback is not None
    await select.callback(interaction)

    rows = await list_agent_files(db_session, tenant_id=tenant.id, agent_id=agent_id)
    keys = [r.key for r in rows]
    assert keys == ["KEEP_ME"], "the targeted key is deleted; others remain"
    interaction.edit_original_response.assert_awaited()  # view re-rendered in place
    # The re-render view must not leak the surviving value.
    rerender_kwargs = interaction.edit_original_response.call_args.kwargs
    assert _SECRET_VALUE not in str(rerender_kwargs), "no value in re-render call kwargs"
    # Same ack mechanism as the paste path: a thinking defer would point
    # `@original` at a fresh ephemeral message and the panel would never change.
    assert interaction.response.defer.call_args.args == (), (
        "the remove must ack with a bare defer() — no thinking, no ephemeral"
    )
    assert interaction.response.defer.call_args.kwargs == {}, (
        "the remove must ack with a bare defer() — no thinking, no ephemeral"
    )


@pytest.mark.asyncio
async def test_remove_refuses_delete_on_reachable_agent_for_non_admin(
    db_session_factory: async_sessionmaker[AsyncSession],
    account_id: uuid.UUID,
) -> None:
    """One click on the remove select destroys a credential every session of a
    shared agent depends on, so a non-admin is refused on an agent the
    workspace currently resolves to."""
    guild_id = 920405
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, platform="discord", workspace_id=str(guild_id))
    agent_id = uuid.uuid4()
    async with db_session_factory() as s, s.begin():
        await put_agent_file(
            s, tenant_id=tenant.id, agent_id=agent_id, key="XERO_API_KEY", content=_SECRET_VALUE
        )
        await put_agent_file(
            s, tenant_id=tenant.id, agent_id=agent_id, key="KEEP_ME", content="keep"
        )

    entry = _entry("bot")
    runtime = _runtime(db_session_factory, deployment_default=DeploymentDefault(agent_name="bot"))
    view = CredentialsSubView(
        runtime=runtime,
        state=_state(entry, account_id),
        allowed_user_id=42,
        tenant_id=tenant.id,
        agent_id=agent_id,
        secret_names=["XERO_API_KEY", "KEEP_ME"],
    )

    interaction = _interaction(is_admin=False, guild_id=guild_id)
    select = _remove_select(view)
    select._values = ["XERO_API_KEY"]  # pyright: ignore[reportPrivateUsage]  # simulate a user pick
    assert select.callback is not None
    await select.callback(interaction)

    async with db_session_factory() as session:
        rows = await list_agent_files(session, tenant_id=tenant.id, agent_id=agent_id)
    assert sorted(r.key for r in rows) == ["KEEP_ME", "XERO_API_KEY"], (
        "a refused removal must delete nothing"
    )
    interaction.response.defer.assert_not_called()
    interaction.edit_original_response.assert_not_called()
    interaction.response.send_message.assert_called_once()
    assert interaction.response.send_message.call_args.kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_remove_still_deletes_on_reachable_system_agent_for_admin(
    db_session_factory: async_sessionmaker[AsyncSession],
    account_id: uuid.UUID,
) -> None:
    """The onboarding sibling of the paste-side positive: an admin must still
    be able to clear a stale variable off the built-in default agent."""
    guild_id = 920406
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, platform="discord", workspace_id=str(guild_id))
    agent_id = uuid.uuid4()
    async with db_session_factory() as s, s.begin():
        await put_agent_file(
            s, tenant_id=tenant.id, agent_id=agent_id, key="XERO_API_KEY", content=_SECRET_VALUE
        )
        await put_agent_file(
            s, tenant_id=tenant.id, agent_id=agent_id, key="KEEP_ME", content="keep"
        )

    entry = _entry("daimon", is_system=True)
    runtime = _runtime(
        db_session_factory, deployment_default=DeploymentDefault(agent_name="daimon")
    )
    view = CredentialsSubView(
        runtime=runtime,
        state=_state(entry, account_id),
        allowed_user_id=42,
        tenant_id=tenant.id,
        agent_id=agent_id,
        secret_names=["XERO_API_KEY", "KEEP_ME"],
    )

    interaction = _interaction(is_admin=True, guild_id=guild_id)
    select = _remove_select(view)
    select._values = ["XERO_API_KEY"]  # pyright: ignore[reportPrivateUsage]  # simulate a user pick
    assert select.callback is not None
    await select.callback(interaction)

    async with db_session_factory() as session:
        rows = await list_agent_files(session, tenant_id=tenant.id, agent_id=agent_id)
    assert [r.key for r in rows] == ["KEEP_ME"], (
        "an admin must still remove a variable from the built-in default agent"
    )
    interaction.edit_original_response.assert_awaited()  # sub-view re-rendered in place


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
        turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]  # never runs a turn
    )
    view = CredentialsSubView(
        runtime=runtime,
        state=_state(entry, account_id),
        allowed_user_id=42,
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        secret_names=["A"],
    )

    interaction = MagicMock()
    interaction.user.id = 42
    interaction.response.edit_message = AsyncMock()
    interaction.delete_original_response = AsyncMock()

    await view._on_back(interaction)  # pyright: ignore[reportPrivateUsage]

    interaction.response.edit_message.assert_awaited_once()  # back edits in place
    interaction.delete_original_response.assert_not_called()  # back must NOT delete
    sent_view = interaction.response.edit_message.call_args.kwargs["view"]
    assert isinstance(sent_view, EditView), "back returns to the unified EditView"


# --- EditView._on_env_vars opens the sub-view ------------------------------


@pytest.mark.asyncio
async def test_editview_env_vars_button_opens_subview(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    account_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await make_tenant(db_session, platform="discord", workspace_id="test-guild")
    _tid = tenant.id

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
        turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]  # never runs a turn
    )
    entry = _entry("bot")
    edit_view = EditView(_state(entry, account_id), runtime=runtime, allowed_user_id=42)

    interaction = MagicMock()
    interaction.user.id = 42
    interaction.response.edit_message = AsyncMock()
    interaction.guild_id = 123

    await edit_view._on_env_vars(interaction)  # pyright: ignore[reportPrivateUsage]

    interaction.response.edit_message.assert_awaited_once()  # keys opens the sub-view in place
    kwargs = interaction.response.edit_message.call_args.kwargs
    assert isinstance(kwargs["view"], CredentialsSubView), "view is the CredentialsSubView"
    # The sub-view's container must not contain the secret value
    all_text = _container_all_text(kwargs["view"])
    assert _SECRET_VALUE not in all_text, "the opened view lists the key masked, never its value"


@pytest.mark.asyncio
async def test_subview_opens_and_lists_key_names_for_a_non_admin_on_a_shared_agent(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    account_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the two writes are gated. A member opening the env-vars list on the
    shared built-in agent still sees the key names — that is what lets them ask
    an admin for the right variable, and tell whether a missing one is why their
    turn failed. The container carries names only, so nothing secret crosses."""
    tenant = await make_tenant(db_session, platform="discord", workspace_id="920407")

    ma_agent_id = "agent_017shared"
    agent_id = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=ma_agent_id)
    async with db_session_factory() as s, s.begin():
        await put_agent_file(
            s, tenant_id=tenant.id, agent_id=agent_id, key="XERO_API_KEY", content=_SECRET_VALUE
        )

    now = dt.datetime.now(dt.UTC)
    real_agent = BetaManagedAgentsAgent(
        id=ma_agent_id,
        type="agent",
        name="daimon",
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

    runtime = _runtime(
        db_session_factory, deployment_default=DeploymentDefault(agent_name="daimon")
    )
    entry = _entry("daimon", is_system=True)
    edit_view = EditView(_state(entry, account_id), runtime=runtime, allowed_user_id=42)

    interaction = _interaction(is_admin=False, guild_id=920407)

    await edit_view._on_env_vars(interaction)  # pyright: ignore[reportPrivateUsage]

    interaction.response.edit_message.assert_awaited_once()
    sub_view = interaction.response.edit_message.call_args.kwargs["view"]
    assert isinstance(sub_view, CredentialsSubView), "a non-admin still reaches the sub-view"
    all_text = _container_all_text(sub_view)
    assert "XERO_API_KEY" in all_text, "a member must see which variables the shared agent has"
    assert _SECRET_VALUE not in all_text, "no value may cross into the rendered container"
    interaction.response.send_message.assert_not_called()


def test_editview_env_vars_button_enabled_for_system_agent(account_id: uuid.UUID) -> None:
    """Keys are per-agent daimon state, never part of the agent spec, so
    the button is enabled for the seeded/system agent exactly like any other.
    The enabled state is deliberate — opening the list stays open to every
    member, and the two writes inside refuse at click time."""
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
        turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]  # never runs a turn
    )
    sys_entry = RosterEntry(
        name="sys",
        model="claude-sonnet-4-6",
        spec=AgentSpec(name="sys", model="claude-sonnet-4-6", system=None),
        is_system=True,
    )
    sys_view = EditView(_state(sys_entry, account_id), runtime=runtime, allowed_user_id=42)
    sys_buttons = {b.label: b for b in _walk_buttons(sys_view) if b.label is not None}
    assert sys_buttons["Keys"].disabled is False, (
        "the seeded/system agent's Keys button must be enabled — keys "
        "are not part of the agent spec"
    )

    user_entry = RosterEntry(
        name="bot",
        model="claude-sonnet-4-6",
        spec=AgentSpec(name="bot", model="claude-sonnet-4-6", system=None),
        is_system=False,
    )
    user_view = EditView(_state(user_entry, account_id), runtime=runtime, allowed_user_id=42)
    user_buttons = {b.label: b for b in _walk_buttons(user_view) if b.label is not None}
    assert user_buttons["Keys"].disabled is False, "user agents can open Keys"


# ---------------------------------------------------------------------------
# Shared on_timeout, including the reused-instance rebind
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_credentials_view_timeout_replaces_the_subview(account_id: uuid.UUID) -> None:
    entry = _entry("bot")
    view = CredentialsSubView(
        runtime=MagicMock(spec=DiscordRuntime),
        state=_state(entry, account_id),
        allowed_user_id=42,
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        secret_names=["A"],
    )
    interaction = MagicMock()
    interaction.edit_original_response = AsyncMock()
    view.bind_render_interaction(interaction, panel=_state(entry, account_id))

    await view.on_timeout()

    interaction.edit_original_response.assert_called_once()
    call_kwargs = interaction.edit_original_response.call_args.kwargs
    assert "content" not in call_kwargs, "the timeout edit must not override content"
    expired_view = call_kwargs["view"]
    walked = list(expired_view.walk_children())
    assert not any(isinstance(c, discord.ui.Button) for c in walked), (
        "the expired replacement must carry no interactive children"
    )
    assert not any(isinstance(c, discord.ui.Select) for c in walked), (
        "the expired replacement must carry no interactive children"
    )
    assert view.timeout == 300, "the shared on_timeout mixin leaves timeout values unchanged"


@pytest.mark.asyncio
async def test_credentials_rerender_rebinds_the_current_interaction(
    account_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression guard: `_reload_and_rerender` must rebind on every
    render, not just once at __init__ — this view is re-rendered as the SAME
    instance, so a stale-bound interaction would expire against the wrong
    (or a long-dead) message."""
    monkeypatch.setattr(credentials_mod, "list_agent_files", AsyncMock(return_value=[]))

    entry = _entry("bot")
    state = _state(entry, account_id)
    runtime = MagicMock()
    runtime.sessionmaker.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
    runtime.sessionmaker.return_value.__aexit__ = AsyncMock(return_value=False)

    view = CredentialsSubView(
        runtime=runtime,
        state=state,
        allowed_user_id=42,
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        secret_names=["A"],
    )

    first_interaction = MagicMock()
    first_interaction.edit_original_response = AsyncMock()
    view.bind_render_interaction(first_interaction, panel=state)

    second_interaction = MagicMock()
    second_interaction.edit_original_response = AsyncMock()
    await view._reload_and_rerender(second_interaction)  # pyright: ignore[reportPrivateUsage]

    await view.on_timeout()

    first_interaction.edit_original_response.assert_not_called()
    second_interaction.edit_original_response.assert_called()
    last_call_kwargs = second_interaction.edit_original_response.call_args.kwargs
    expired_view = last_call_kwargs["view"]
    walked = list(expired_view.walk_children())
    assert not any(isinstance(c, discord.ui.Button) for c in walked), (
        "the timeout edit must land through the SECOND interaction with the controls-less expired view"
    )
