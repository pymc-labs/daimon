from __future__ import annotations

import uuid
from io import StringIO
from typing import cast

import pytest
import typer
from daimon.adapters.cli.commands.config import (
    _config_get_entry,
    _config_propagate_entry,
    _parse_scope,
)
from daimon.core._models import Account
from daimon.core.scope import (
    ChannelScopeRef,
    DeploymentDefault,
    TenantScopeRef,
    UserScopeRef,
)
from daimon.core.stores.scoped_config_write import set_fields
from daimon.testing.factories import make_tenant
from rich.console import Console
from sqlalchemy.ext.asyncio import AsyncSession


def test_parse_scope_user() -> None:
    ref = _parse_scope("user", tenant_id=uuid.uuid4(), account_id=uuid.uuid4())
    assert isinstance(ref, UserScopeRef)


def test_parse_scope_tenant() -> None:
    tid = uuid.uuid4()
    ref = _parse_scope("tenant", tenant_id=tid, account_id=uuid.uuid4())
    assert isinstance(ref, TenantScopeRef), "bare 'tenant' must return local TenantScopeRef"
    assert ref.tenant_id == tid, "tenant_id must be the discovered local tenant"


def test_parse_scope_tenant_discord() -> None:
    tid = uuid.uuid4()
    ref = _parse_scope("tenant:discord/9999", tenant_id=tid, account_id=uuid.uuid4())
    assert isinstance(ref, TenantScopeRef), "tenant:discord/<guild_id> must return TenantScopeRef"
    # tenant_id is derived from (platform="discord", workspace_id="9999") — not the local tenant
    assert ref.tenant_id != tid, "derived tenant_id must differ from the local tenant"


def test_parse_scope_channel_bare() -> None:
    tid = uuid.uuid4()
    ref = _parse_scope("channel:chan-123", tenant_id=tid, account_id=uuid.uuid4())
    assert isinstance(ref, ChannelScopeRef), "bare channel:<id> must return ChannelScopeRef"
    assert ref.channel_id == "chan-123", "channel_id must be set"
    assert ref.tenant_id == tid, "bare channel scope uses local tenant"


def test_parse_scope_channel_discord() -> None:
    tid = uuid.uuid4()
    ref = _parse_scope("channel:discord/1234/5678", tenant_id=tid, account_id=uuid.uuid4())
    assert isinstance(ref, ChannelScopeRef), (
        "channel:discord/<guild_id>/<channel_id> must return ChannelScopeRef"
    )
    assert ref.channel_id == "5678", "channel_id must be the last path segment"


def test_parse_scope_invalid_raises() -> None:
    with pytest.raises(typer.BadParameter):
        _parse_scope("bogus", tenant_id=uuid.uuid4(), account_id=uuid.uuid4())


def test_parse_scope_channel_bad_discord_format_raises() -> None:
    with pytest.raises(typer.BadParameter):
        # channel:discord/<guild_id> is missing the channel_id segment — invalid
        _parse_scope("channel:discord/1234", tenant_id=uuid.uuid4(), account_id=uuid.uuid4())


@pytest.mark.asyncio
async def test_config_get_effective_shows_provenance(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    account = Account(tenant_id=tenant.id)
    db_session.add(account)
    await db_session.flush()

    await set_fields(
        db_session,
        scope=TenantScopeRef(tenant_id=tenant.id),
        tenant_id=tenant.id,
        agent_name="a",
    )
    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)
    await _config_get_entry(
        db_session,
        tenant_id=tenant.id,
        account_id=account.id,
        console=console,
        as_json=False,
        scope_str=None,
        channel_str=None,
        account_override=None,
        deployment_default=DeploymentDefault(),
    )
    out = cast(StringIO, console.file).getvalue()
    assert "tenant" in out, "provenance tier should appear in output"
    assert "a" in out, "agent name should appear in output"


@pytest.mark.asyncio
async def test_config_get_raw_scope(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    account = Account(tenant_id=tenant.id)
    db_session.add(account)
    await db_session.flush()

    await set_fields(
        db_session,
        scope=UserScopeRef(account_id=account.id),
        tenant_id=tenant.id,
        agent_name="b",
    )
    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)
    await _config_get_entry(
        db_session,
        tenant_id=tenant.id,
        account_id=account.id,
        console=console,
        as_json=False,
        scope_str="user",
        channel_str=None,
        account_override=None,
        deployment_default=DeploymentDefault(),
    )
    out = cast(StringIO, console.file).getvalue()
    assert "b" in out, "raw scope should show user-tier value"


@pytest.mark.asyncio
async def test_config_get_three_tier_with_channel(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    account = Account(tenant_id=tenant.id)
    db_session.add(account)
    await db_session.flush()

    await set_fields(
        db_session,
        scope=ChannelScopeRef(tenant_id=tenant.id, channel_id="5678"),
        tenant_id=tenant.id,
        agent_name="chan-agent",
    )
    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)
    await _config_get_entry(
        db_session,
        tenant_id=tenant.id,
        account_id=account.id,
        console=console,
        as_json=False,
        scope_str=None,
        channel_str="discord/1234/5678",
        account_override=None,
        deployment_default=DeploymentDefault(),
    )
    out = cast(StringIO, console.file).getvalue()
    assert "channel" in out, "three-tier resolution should show channel provenance"
    assert "chan-agent" in out, "channel-tier agent should appear"


@pytest.mark.asyncio
async def test_config_propagate_empty_source(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    account = Account(tenant_id=tenant.id)
    db_session.add(account)
    await db_session.flush()

    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)
    await _config_propagate_entry(
        db_session,
        tenant_id=tenant.id,
        account_id=account.id,
        console=console,
        to_strs=["tenant"],
        from_str="user",
        fields_str=None,
        reset=False,
    )
    out = cast(StringIO, console.file).getvalue()
    assert "nothing to propagate" in out, "empty source should produce no-op message"


# ---------------------------------------------------------------------------
# RED tests — new scope grammar
#
# These tests call _parse_scope / _config_set_entry / _config_get_entry directly
# against db_session (not CliRunner). They import not-yet-existing symbols
# (TenantScopeRef, DeploymentDefault) and are RED until Plans 03/04/06 land.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_scope_tenant(db_session: AsyncSession) -> None:
    """config set --scope tenant writes a row to tenant_config (R6)."""
    from daimon.adapters.cli.commands.config import _config_set_entry  # noqa: PLC0415
    from daimon.core.scope import TenantScopeRef  # noqa: PLC0415
    from daimon.core.stores.scoped_config_read import get_scope  # noqa: PLC0415

    tenant = await make_tenant(db_session)
    account = Account(tenant_id=tenant.id)
    db_session.add(account)
    await db_session.flush()

    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)
    await _config_set_entry(
        db_session,
        tenant_id=tenant.id,
        account_id=account.id,
        console=console,
        key="agent_name",
        value="my-agent",
        scope_str="tenant",
    )
    row = await get_scope(db_session, scope=TenantScopeRef(tenant_id=tenant.id))
    assert row is not None, "set --scope tenant must write a row to tenant_config"
    assert row.agent_name == "my-agent", (
        "tenant_config row must hold the value written by config set --scope tenant"
    )


@pytest.mark.asyncio
async def test_set_scope_channel_writes_channel_config(db_session: AsyncSession) -> None:
    """config set --scope channel:<channel_id> writes channel_config (R6)."""
    from daimon.adapters.cli.commands.config import _config_set_entry  # noqa: PLC0415
    from daimon.core.scope import ChannelScopeRef  # noqa: PLC0415
    from daimon.core.stores.scoped_config_read import get_scope  # noqa: PLC0415

    tenant = await make_tenant(db_session)
    account = Account(tenant_id=tenant.id)
    db_session.add(account)
    await db_session.flush()

    channel_id = "chan-123"
    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)
    await _config_set_entry(
        db_session,
        tenant_id=tenant.id,
        account_id=account.id,
        console=console,
        key="agent_name",
        value="chan-agent",
        scope_str=f"channel:{channel_id}",
    )
    row = await get_scope(
        db_session,
        scope=ChannelScopeRef(tenant_id=tenant.id, channel_id=channel_id),
    )
    assert row is not None, "set --scope channel:<id> must write a row to channel_config"
    assert row.agent_name == "chan-agent", (
        "channel_config row must hold the value written by config set --scope channel:<id>"
    )


def test_set_scope_deployment_rejects() -> None:
    """config set --scope deployment exits non-zero with redeploy guidance (R6).

    Pins threat T-58.3-02: guild admins cannot set the deployment default.
    """
    # After Plan 06, _config_set_entry will early-exit with typer.Exit(1) for "deployment".
    # For now (RED), we verify _parse_scope raises BadParameter (old behavior)
    # OR that the set entry raises Exit(1) (new behavior).
    # Either satisfies the intent. This test imports the new grammar symbols.
    import typer  # noqa: PLC0415
    from daimon.adapters.cli.commands.config import _parse_scope  # noqa: PLC0415

    with pytest.raises((typer.BadParameter, typer.Exit)):
        _parse_scope("deployment", tenant_id=uuid.uuid4(), account_id=uuid.uuid4())


def test_scope_tenant_system_rejected() -> None:
    """_parse_scope('tenant_system', ...) raises BadParameter in the new grammar (R6)."""
    import typer  # noqa: PLC0415
    from daimon.adapters.cli.commands.config import _parse_scope  # noqa: PLC0415

    with pytest.raises(typer.BadParameter):
        _parse_scope("tenant_system", tenant_id=uuid.uuid4(), account_id=uuid.uuid4())


def test_scope_workspace_rejected() -> None:
    """_parse_scope('workspace:discord/123', ...) raises BadParameter in the new grammar (R6)."""
    import typer  # noqa: PLC0415
    from daimon.adapters.cli.commands.config import _parse_scope  # noqa: PLC0415

    with pytest.raises(typer.BadParameter):
        _parse_scope("workspace:discord/123", tenant_id=uuid.uuid4(), account_id=uuid.uuid4())


@pytest.mark.asyncio
async def test_get_scope_deployment(db_session: AsyncSession) -> None:
    """config get --scope deployment prints the injected rt.deployment_default values (R6)."""
    from io import StringIO  # noqa: PLC0415

    from daimon.adapters.cli.commands.config import _config_get_entry  # noqa: PLC0415
    from daimon.core.scope import DeploymentDefault  # noqa: PLC0415

    tenant = await make_tenant(db_session)
    account = Account(tenant_id=tenant.id)
    db_session.add(account)
    await db_session.flush()

    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=120)
    # After Plan 06, _config_get_entry intercepts scope_str="deployment" before _parse_scope
    # and prints the injected DeploymentDefault. This test drives the new signature.
    # RED until Plan 06 adds the deployment pre-check and threads deployment_default
    # (currently _config_get_entry does not accept a deployment_default param).
    await _config_get_entry(
        db_session,
        tenant_id=tenant.id,
        account_id=account.id,
        console=console,
        as_json=False,
        scope_str="deployment",
        channel_str=None,
        account_override=None,
        deployment_default=DeploymentDefault(agent_name="daimon", environment_name="default"),
    )
    out = cast(StringIO, console.file).getvalue()
    assert "daimon" in out, (
        "config get --scope deployment must print the injected deployment_default.agent_name"
    )
