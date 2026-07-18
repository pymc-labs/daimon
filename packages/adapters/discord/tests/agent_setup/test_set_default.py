"""Real-Postgres write-path tests for agent_setup/scope_default.py.

Re-homed from tests/propagate/test_write.py.
The behavioral coverage is preserved; per-user-tier tests are dropped
(The per-user tier is retired; the fold only ever writes
mode="agent", which is implicit and has no mode= kwarg in the new API).

These tests import from `daimon.adapters.discord.agent_setup.scope_default`,
which is created in Plan 02. They are RED until Plan 02 lands, GREEN after.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.discord.agent_setup.scope_default import (
    PropagateResult,
    do_propagate,
    do_unpropagate,
    list_guild_propagations,
)
from daimon.adapters.discord.agent_setup.set_default import (
    ScopeBlock,
    SetDefaultView,
    _build_scope_blocks,  # pyright: ignore[reportPrivateUsage]  # shell assembly under test
    build_set_default_container,
)
from daimon.adapters.discord.agent_setup.state import PanelState, RosterEntry
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core._models import Account, Tenant  # noqa: PLC0415  # ORM only for setup
from daimon.core.errors import StoreError
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.scope import (
    ChannelConfigRow,
    ChannelScopeRef,
    DeploymentDefault,
    TenantConfigRow,
    TenantScopeRef,
)
from daimon.core.specs import AgentSpec
from daimon.core.stores.scoped_config_read import get_scope
from daimon.core.stores.scoped_config_write import set_fields
from sqlalchemy.ext.asyncio import AsyncSession

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_tenant(session: AsyncSession) -> Tenant:
    import uuid as _uuid

    ws_id = str(_uuid.uuid4())
    _tid = derive_tenant_uuid(platform="discord", workspace_id=ws_id)
    t = Tenant(id=_tid, platform="discord", external_id=ws_id)
    session.add(t)
    await session.flush()
    return t


async def _make_account(session: AsyncSession, tenant: Tenant) -> Account:
    a = Account(tenant_id=tenant.id)
    session.add(a)
    await session.flush()
    return a


def _entry(name: str) -> RosterEntry:
    return RosterEntry(
        name=name,
        model="claude-sonnet-4-6",
        spec=AgentSpec(name=name, model="claude-sonnet-4-6", system=None),
    )


def _make_state(
    *,
    selected_name: str = "alice",
    channel_id: int = 1001,
    channel_name: str | None = None,
    guild_id: int = 2001,
    cascade_view: tuple[TenantConfigRow | None, list[ChannelConfigRow]] = (None, []),
    deployment_default: DeploymentDefault | None = None,
    is_admin: bool = True,
) -> PanelState:
    entry = _entry(selected_name)
    return PanelState.initial(
        roster=[entry],
        account_id=uuid.UUID(int=0xAA),
        platform_principal_id=uuid.uuid4(),
        is_admin=is_admin,
        guild_id=guild_id,
        channel_id=channel_id,
        channel_name=channel_name,
        cascade_view=cascade_view,
        deployment_default=deployment_default
        if deployment_default is not None
        else DeploymentDefault(),
    )


# ---------------------------------------------------------------------------
# Write-path behavioral coverage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_propagate_clean_scope_returns_no_prior_and_stamps_audit(
    db_session: AsyncSession,
) -> None:
    tenant = await _make_tenant(db_session)
    actor = await _make_account(db_session, tenant)
    scope = ChannelScopeRef(tenant_id=tenant.id, channel_id="c1")
    result = await do_propagate(
        db_session,
        scope=scope,
        tenant_id=tenant.id,
        agent_name="writer-v1",
        actor_account_id=actor.id,
    )
    assert result == PropagateResult(
        prior_agent_name=None,
        prior_actor_account_id=None,
    ), "clean propagate must return both prior values as None"
    row = await get_scope(db_session, scope=scope)
    assert row is not None, "a row must be created after do_propagate"
    assert row.agent_name == "writer-v1", "row should hold the propagated agent_name"
    assert getattr(row, "agent_name_set_by_account_id", None) == actor.id, (
        "audit column must record the actor account_id"
    )


@pytest.mark.asyncio
async def test_do_propagate_overwrite_returns_prior_values(
    db_session: AsyncSession,
) -> None:
    """Overwrite returns prior agent name for cascade naming."""
    tenant = await _make_tenant(db_session)
    actor_a = await _make_account(db_session, tenant)
    actor_b = await _make_account(db_session, tenant)
    scope = TenantScopeRef(tenant_id=tenant.id)
    await do_propagate(
        db_session,
        scope=scope,
        tenant_id=tenant.id,
        agent_name="writer-v1",
        actor_account_id=actor_a.id,
    )
    result = await do_propagate(
        db_session,
        scope=scope,
        tenant_id=tenant.id,
        agent_name="writer-v2",
        actor_account_id=actor_b.id,
    )
    assert result.prior_agent_name == "writer-v1", (
        "overwrite must capture prior agent_name for the 'replaced X → Y' line"
    )
    assert result.prior_actor_account_id == actor_a.id, (
        "overwrite must capture prior actor_account_id"
    )
    row = await get_scope(db_session, scope=scope)
    assert row is not None and row.agent_name == "writer-v2", (
        "row must reflect the new agent_name after overwrite"
    )
    assert getattr(row, "agent_name_set_by_account_id", None) == actor_b.id, (
        "audit column must be re-stamped to the new actor"
    )


@pytest.mark.asyncio
async def test_do_unpropagate_deletes_row_when_only_agent_name_was_set(
    db_session: AsyncSession,
) -> None:
    """Clearing agent_name auto-deletes fully-NULL row."""
    tenant = await _make_tenant(db_session)
    actor = await _make_account(db_session, tenant)
    scope = TenantScopeRef(tenant_id=tenant.id)
    await do_propagate(
        db_session,
        scope=scope,
        tenant_id=tenant.id,
        agent_name="writer-v1",
        actor_account_id=actor.id,
    )
    await do_unpropagate(db_session, scope=scope, actor_account_id=actor.id)
    row = await get_scope(db_session, scope=scope)
    assert row is None, "row must be deleted when both agent_name and environment_name end up NULL"


@pytest.mark.asyncio
async def test_do_unpropagate_preserves_row_when_environment_name_still_set(
    db_session: AsyncSession,
) -> None:
    """Clearing agent_name preserves the row when environment_name is set."""
    tenant = await _make_tenant(db_session)
    actor = await _make_account(db_session, tenant)
    scope = TenantScopeRef(tenant_id=tenant.id)
    # set both fields explicitly via set_fields, then unpropagate only agent_name
    await set_fields(
        db_session,
        scope=scope,
        tenant_id=tenant.id,
        agent_name="writer-v1",
        environment_name="prod",
        actor_account_id=actor.id,
    )
    await do_unpropagate(db_session, scope=scope, actor_account_id=actor.id)
    row = await get_scope(db_session, scope=scope)
    assert row is not None, "row must survive when environment_name is still set"
    assert row.agent_name is None, "agent_name must be cleared by do_unpropagate"
    assert row.environment_name == "prod", "environment_name must be untouched by do_unpropagate"


@pytest.mark.asyncio
async def test_list_guild_propagations_filters_by_tenant_id(
    db_session: AsyncSession,
) -> None:
    """list_guild_propagations must isolate by tenant_id."""
    tenant_a = await _make_tenant(db_session)
    tenant_b = await _make_tenant(db_session)
    actor_a = await _make_account(db_session, tenant_a)
    actor_b = await _make_account(db_session, tenant_b)
    # tenant_a: tenant-level + 2 channels
    await do_propagate(
        db_session,
        scope=TenantScopeRef(tenant_id=tenant_a.id),
        tenant_id=tenant_a.id,
        agent_name="tenant-bot",
        actor_account_id=actor_a.id,
    )
    await do_propagate(
        db_session,
        scope=ChannelScopeRef(tenant_id=tenant_a.id, channel_id="c1"),
        tenant_id=tenant_a.id,
        agent_name="c1-bot",
        actor_account_id=actor_a.id,
    )
    await do_propagate(
        db_session,
        scope=ChannelScopeRef(tenant_id=tenant_a.id, channel_id="c2"),
        tenant_id=tenant_a.id,
        agent_name="c2-bot",
        actor_account_id=actor_a.id,
    )
    # tenant_b — must NOT leak across tenants
    await do_propagate(
        db_session,
        scope=TenantScopeRef(tenant_id=tenant_b.id),
        tenant_id=tenant_b.id,
        agent_name="other-tenant",
        actor_account_id=actor_b.id,
    )
    tenant_row, ch_rows = await list_guild_propagations(db_session, tenant_id=tenant_a.id)
    assert tenant_row is not None and tenant_row.agent_name == "tenant-bot", (
        "tenant row must be returned for the target tenant"
    )
    channel_names = sorted(r.agent_name for r in ch_rows if r.agent_name is not None)
    assert channel_names == ["c1-bot", "c2-bot"], (
        "only channel rows for the target tenant must be returned"
    )


@pytest.mark.asyncio
async def test_do_propagate_requires_agent_name(db_session: AsyncSession) -> None:
    """do_propagate must raise StoreError on falsy agent_name (mode='agent' is implicit)."""
    tenant = await _make_tenant(db_session)
    actor = await _make_account(db_session, tenant)
    scope = ChannelScopeRef(tenant_id=tenant.id, channel_id="c1")
    with pytest.raises(StoreError, match="agent_name"):
        await do_propagate(
            db_session,
            scope=scope,
            tenant_id=tenant.id,
            agent_name=None,
            actor_account_id=actor.id,
        )


# ---------------------------------------------------------------------------
# CR-01: server-side admin re-check on the privileged set-default write path
# ---------------------------------------------------------------------------


def _set_default_runtime() -> DiscordRuntime:
    return MagicMock(spec=DiscordRuntime)


def _member(*, user_id: int, is_admin: bool) -> discord.Member:
    member = MagicMock(spec=discord.Member)
    member.id = user_id
    member.guild_permissions.administrator = is_admin
    member.guild_permissions.manage_guild = False
    return member


@pytest.mark.asyncio
async def test_set_default_interaction_check_rejects_invoker_who_lost_admin() -> None:
    """A matching invoker who is no longer a guild admin is rejected at the live boundary.

    The button is hidden for non-admins at panel-open, but is_admin is a stale
    snapshot; the write path must re-verify against the live interaction.
    """
    view = SetDefaultView(_make_state(), runtime=_set_default_runtime(), allowed_user_id=42)
    interaction = MagicMock()
    interaction.user = _member(user_id=42, is_admin=False)
    interaction.guild.owner_id = 1
    interaction.response.send_message = AsyncMock()

    ok = await view.interaction_check(interaction)

    assert ok is False, "a non-admin must be rejected even when the invoker id matches"
    interaction.response.send_message.assert_called_once()


@pytest.mark.asyncio
async def test_set_default_interaction_check_rejects_wrong_user() -> None:
    """A different user clicking the ephemeral chooser is rejected before the admin check."""
    view = SetDefaultView(_make_state(), runtime=_set_default_runtime(), allowed_user_id=42)
    interaction = MagicMock()
    interaction.user = _member(user_id=999, is_admin=True)
    interaction.guild.owner_id = 1
    interaction.response.send_message = AsyncMock()

    ok = await view.interaction_check(interaction)

    assert ok is False, "a non-invoker must be rejected by the view's interaction_check"
    interaction.response.send_message.assert_called_once()


@pytest.mark.asyncio
async def test_set_default_interaction_check_allows_admin_invoker() -> None:
    """The original admin invoker passes the gate."""
    view = SetDefaultView(_make_state(), runtime=_set_default_runtime(), allowed_user_id=42)
    interaction = MagicMock()
    interaction.user = _member(user_id=42, is_admin=True)
    interaction.guild.owner_id = 1
    interaction.response.send_message = AsyncMock()

    ok = await view.interaction_check(interaction)

    assert ok is True, "an admin invoker must pass the set-default gate"


# ---------------------------------------------------------------------------
# RED tests — TenantScopeRef/TenantConfigRow + deployment tier
#
# These tests import not-yet-existing symbols (TenantScopeRef, TenantConfigRow,
# DeploymentDefault) and are RED until Plans 03/04/07 land. That is expected.
# ---------------------------------------------------------------------------


# Alias to keep existing admin-gate test name referenced by VALIDATION.md (R7)
test_non_admin_rejected = test_set_default_interaction_check_rejects_invoker_who_lost_admin


@pytest.mark.asyncio
async def test_whole_server_writes_tenant_config(
    db_session: AsyncSession,
) -> None:
    """'Whole server' button write targets tenant_config via TenantScopeRef (R7).

    This test is RED until Plan 03 (TenantScopeRef) + Plan 07 (panel.py re-key) land.
    """
    from daimon.adapters.discord.agent_setup.scope_default import do_propagate  # noqa: PLC0415
    from daimon.core.scope import TenantScopeRef  # noqa: PLC0415
    from daimon.core.stores.scoped_config_read import get_scope  # noqa: PLC0415

    t = await _make_tenant(db_session)
    actor = await _make_account(db_session, t)

    scope = TenantScopeRef(tenant_id=t.id)
    await do_propagate(
        db_session,
        scope=scope,
        tenant_id=t.id,
        agent_name="whole-server-agent",
        actor_account_id=actor.id,
    )

    row = await get_scope(db_session, scope=scope)
    assert row is not None, (
        "'Whole server' propagate must write a row accessible via TenantScopeRef"
    )
    assert row.agent_name == "whole-server-agent", (
        "TenantScopeRef row must hold the agent_name written by do_propagate"
    )


# ---------------------------------------------------------------------------
# Pure builder tests (build_set_default_container)
# ---------------------------------------------------------------------------


def test_build_set_default_container_single_block_three_line_grammar() -> None:
    """A single ScopeBlock renders one TextDisplay with the three-line grammar."""
    blocks = [ScopeBlock(scope_label="#bot-spam", agent_name="alice", audit_line="system default")]
    container = build_set_default_container(blocks)

    text_displays = [
        item for item in container.children if isinstance(item, discord.ui.TextDisplay)
    ]
    # Skip the header TextDisplay; find the block TextDisplay(s)
    block_displays = [td for td in text_displays if "**" in (td.content or "")]
    assert len(block_displays) == 1, "one block should produce one block TextDisplay"
    content = block_displays[0].content or ""
    assert "**#bot-spam**" in content, "scope label must be bold"
    assert "⚙️ `alice`" in content, "agent name must be backtick-formatted"
    assert "-# system default" in content, "audit line must be dim (-# prefix)"


def test_build_set_default_container_two_blocks_three_lines_each() -> None:
    """Two ScopeBlocks each render their three-line grammar."""
    blocks = [
        ScopeBlock(scope_label="#bot-spam", agent_name="alice", audit_line="system default"),
        ScopeBlock(
            scope_label="whole server", agent_name="bob", audit_line="set by @user · 2026-01-01"
        ),
    ]
    container = build_set_default_container(blocks)

    block_displays = [
        item
        for item in container.children
        if isinstance(item, discord.ui.TextDisplay) and "**" in (item.content or "")
    ]
    assert len(block_displays) == 2, "two blocks must produce two block TextDisplays"
    for td in block_displays:
        content = td.content or ""
        lines = content.split("\n")
        assert len(lines) == 3, (
            f"each block TextDisplay must have exactly three lines; got: {lines}"
        )


def test_build_set_default_container_air_gap_between_blocks() -> None:
    """Air-gap separators (visible=False, spacing=large) appear between blocks, not hairlines."""
    blocks = [
        ScopeBlock(scope_label="#ch1", agent_name="alice", audit_line="system default"),
        ScopeBlock(scope_label="#ch2", agent_name="bob", audit_line="set"),
        ScopeBlock(scope_label="everywhere else", agent_name="sys", audit_line="system default"),
    ]
    container = build_set_default_container(blocks)

    separators = [item for item in container.children if isinstance(item, discord.ui.Separator)]
    # First separator is the hairline after the header. Separators after that are air_gaps.
    air_gaps = [s for s in separators if not s.visible]
    assert len(air_gaps) == 2, "two air_gaps expected between 3 blocks (one less than block count)"
    for sep in air_gaps:
        assert sep.spacing == discord.SeparatorSpacing.large, (
            "air_gap separators must use large spacing"
        )


def test_build_set_default_container_no_unset_or_ladder_language() -> None:
    """The builder never emits (unset), ladder, or tier language."""
    blocks = [
        ScopeBlock(scope_label="whole server", agent_name="alice", audit_line="set"),
    ]
    container = build_set_default_container(blocks)

    all_text = " ".join(
        item.content or ""
        for item in container.children
        if isinstance(item, discord.ui.TextDisplay)
    )
    assert "(unset)" not in all_text, "no (unset) strings must appear"
    assert "ladder" not in all_text.lower(), "no ladder language must appear"
    assert "tier" not in all_text.lower(), "no tier language must appear"


def test_build_set_default_container_empty_blocks_shows_only_header() -> None:
    """Zero blocks yields just the header + hairline, no block TextDisplays."""
    container = build_set_default_container([])

    block_displays = [
        item
        for item in container.children
        if isinstance(item, discord.ui.TextDisplay) and "**" in (item.content or "")
    ]
    assert len(block_displays) == 0, "no block TextDisplays when block list is empty"


# ---------------------------------------------------------------------------
# SetDefaultView construction tests
# ---------------------------------------------------------------------------


def _make_view(
    state: PanelState | None = None,
    *,
    channel_default_exists: bool = False,
    server_default_exists: bool = False,
) -> SetDefaultView:
    if state is None:
        tenant_row = (
            TenantConfigRow(tenant_id=uuid.UUID(int=0), agent_name="bob")
            if server_default_exists
            else None
        )
        ch_row_list: list[ChannelConfigRow] = (
            [ChannelConfigRow(tenant_id=uuid.UUID(int=0), agent_name="alice", channel_id="1001")]
            if channel_default_exists
            else []
        )
        state = _make_state(
            cascade_view=(tenant_row, ch_row_list),
            channel_id=1001,
        )
    return SetDefaultView(state, runtime=_set_default_runtime(), allowed_user_id=42)


def _walk_children(
    view: discord.ui.LayoutView,
) -> list[object]:
    """Walk all descendants of a LayoutView recursively (depth-first)."""
    items: list[object] = []

    def _recurse(node: object) -> None:
        items.append(node)
        children = getattr(node, "children", None)
        if children:
            for child in children:
                _recurse(child)

    for child in view.children:
        _recurse(child)
    return items


def test_set_default_view_both_defaults_gives_four_options() -> None:
    """When both channel and server defaults exist, the action select has 4 options."""
    view = _make_view(channel_default_exists=True, server_default_exists=True)
    all_items = _walk_children(view)
    selects = [item for item in all_items if isinstance(item, discord.ui.Select)]
    action_select = next(
        (
            s
            for s in selects
            if isinstance(s, discord.ui.Select) and not isinstance(s, discord.ui.ChannelSelect)
        ),
        None,
    )
    assert action_select is not None, "action select must be present"
    values = [opt.value for opt in action_select.options]
    assert values == ["set_channel", "set_server", "clear_channel", "clear_server"], (
        "both defaults → 4 options in order: set_channel, set_server, clear_channel, clear_server"
    )


def test_set_default_view_no_defaults_gives_two_options() -> None:
    """When neither default exists, the action select has only 2 set options."""
    view = _make_view(channel_default_exists=False, server_default_exists=False)
    all_items = _walk_children(view)
    selects = [item for item in all_items if isinstance(item, discord.ui.Select)]
    action_select = next(
        (
            s
            for s in selects
            if isinstance(s, discord.ui.Select) and not isinstance(s, discord.ui.ChannelSelect)
        ),
        None,
    )
    assert action_select is not None, "action select must be present"
    values = [opt.value for opt in action_select.options]
    assert values == ["set_channel", "set_server"], (
        "no defaults → only 2 options: set_channel, set_server"
    )


def test_set_default_view_has_channel_select_with_text_type() -> None:
    """ChannelSelect is present with channel_types=[text]."""
    view = _make_view()
    all_items = _walk_children(view)
    channel_selects = [item for item in all_items if isinstance(item, discord.ui.ChannelSelect)]
    assert len(channel_selects) == 1, "exactly one ChannelSelect must be present"
    cs = channel_selects[0]
    assert cs.channel_types == [discord.ChannelType.text], (
        "ChannelSelect must filter to text channels only"
    )


def test_set_default_view_channel_select_placeholder_contains_agent_name() -> None:
    """ChannelSelect placeholder interpolates the selected agent name."""
    state = _make_state(selected_name="my-bot")
    view = SetDefaultView(state, runtime=_set_default_runtime(), allowed_user_id=42)
    all_items = _walk_children(view)
    channel_selects = [item for item in all_items if isinstance(item, discord.ui.ChannelSelect)]
    assert len(channel_selects) == 1, "ChannelSelect must be present"
    placeholder = channel_selects[0].placeholder or ""
    assert placeholder.startswith("…or pick any channel for "), (
        f"ChannelSelect placeholder must start with '…or pick any channel for ', got: {placeholder!r}"
    )
    assert "my-bot" in placeholder, (
        "selected agent name must appear in the ChannelSelect placeholder"
    )


def test_set_default_view_action_select_placeholder_contains_agent_name() -> None:
    """Action select placeholder interpolates the selected agent name."""
    state = _make_state(selected_name="my-bot")
    view = SetDefaultView(state, runtime=_set_default_runtime(), allowed_user_id=42)
    all_items = _walk_children(view)
    selects = [item for item in all_items if isinstance(item, discord.ui.Select)]
    action_select = next(
        (
            s
            for s in selects
            if isinstance(s, discord.ui.Select) and not isinstance(s, discord.ui.ChannelSelect)
        ),
        None,
    )
    assert action_select is not None, "action select must be present"
    placeholder = action_select.placeholder or ""
    assert "my-bot" in placeholder, (
        "selected agent name must appear in the action select placeholder"
    )


def test_set_default_view_has_back_button() -> None:
    """← Back button is present in the view."""
    view = _make_view()
    all_items = _walk_children(view)
    buttons = [item for item in all_items if isinstance(item, discord.ui.Button)]
    back_buttons = [b for b in buttons if (b.label or "").startswith("←")]
    assert len(back_buttons) == 1, "exactly one ← Back button must be present"


# ---------------------------------------------------------------------------
# Deployment-default fallback in the scope blocks (R7)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scope_blocks_fall_back_to_deployment_default_when_no_rows() -> None:
    """With no channel/tenant rows, the blocks end with an 'everywhere else'
    entry carrying the injected DeploymentDefault agent_name."""
    state = _make_state(
        selected_name="daimon",
        cascade_view=(None, []),
        deployment_default=DeploymentDefault(agent_name="daimon", environment_name="default"),
    )
    interaction = MagicMock()
    interaction.guild = None

    blocks = await _build_scope_blocks(state, interaction)

    assert blocks, "deployment default must produce a fallback block"
    assert blocks[-1].scope_label == "everywhere else", (
        "the deployment-default block is always last and labeled 'everywhere else'"
    )
    assert blocks[-1].agent_name == "daimon", (
        "the fallback block must show the injected DeploymentDefault.agent_name"
    )
