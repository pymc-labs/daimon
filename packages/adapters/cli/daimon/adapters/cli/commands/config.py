from __future__ import annotations

import uuid
from typing import Annotated

import typer
from daimon.adapters.cli.errors import run_cli
from daimon.adapters.cli.flags import JSON_OPTION
from daimon.adapters.cli.output import emit_rows
from daimon.adapters.cli.runtime import build_runtime
from daimon.adapters.cli.tenant import discover_tenant
from daimon.core.config import Settings, load_settings
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.scope import (
    ChannelScopeRef,
    ConfigField,
    DeploymentDefault,
    ScopeContext,
    ScopeRef,
    TenantScopeRef,
    UserScopeRef,
)
from daimon.core.stores.identity import get_or_create_cli_principal
from daimon.core.stores.scoped_config_read import get_scope, resolve
from daimon.core.stores.scoped_config_write import (
    propagate,
    set_fields,
    unset_fields,
)
from pydantic import BaseModel
from rich.console import Console
from sqlalchemy.ext.asyncio import AsyncSession

config_app = typer.Typer(
    help="Config: get/set/unset/propagate across scopes.",
)

_VALID_KEYS: frozenset[str] = frozenset({"agent_name", "environment_name"})
_ALL_FIELDS: list[ConfigField] = ["agent_name", "environment_name"]


def _parse_kv(raw: str) -> tuple[str, str]:
    if "=" not in raw:
        raise typer.BadParameter("expected <key>=<value>")
    key, _, value = raw.partition("=")
    return key.strip(), value.strip()


def _validate_key(key: str) -> ConfigField:
    if key not in _VALID_KEYS:
        raise typer.BadParameter(f"unknown config key {key!r}; valid: {sorted(_VALID_KEYS)}")
    result: ConfigField = key  # type: ignore[assignment]
    return result


def _parse_scope(
    raw: str,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
) -> ScopeRef:
    # NOTE: "deployment" is handled before _parse_scope is called (D-06)
    if raw == "user":
        return UserScopeRef(account_id=account_id)
    if raw == "tenant":
        return TenantScopeRef(tenant_id=tenant_id)
    if raw.startswith("tenant:discord/"):
        guild_id = raw[len("tenant:discord/") :]
        return TenantScopeRef(
            tenant_id=derive_tenant_uuid(platform="discord", workspace_id=guild_id)
        )
    if raw.startswith("channel:"):
        rest = raw[len("channel:") :]
        if "/" not in rest:
            # bare channel:<channel_id> — local tenant
            return ChannelScopeRef(tenant_id=tenant_id, channel_id=rest)
        parts = rest.split("/")
        if len(parts) == 3 and parts[0] == "discord":
            # channel:discord/<guild_id>/<channel_id>
            return ChannelScopeRef(
                tenant_id=derive_tenant_uuid(platform="discord", workspace_id=parts[1]),
                channel_id=parts[2],
            )
        raise typer.BadParameter(
            f"invalid channel scope {raw!r}; expected channel:<channel_id> "
            "or channel:discord/<guild_id>/<channel_id>"
        )
    raise typer.BadParameter(
        "unknown scope; valid: user, tenant, tenant:discord/<guild_id>, "
        "channel:<channel_id>, channel:discord/<guild_id>/<channel_id>, deployment (read-only)"
    )


# -- get -------------------------------------------------------------------


class _EffectiveRow(BaseModel):
    field: str
    value: str | None
    tier: str | None


class _RawRow(BaseModel):
    field: str
    value: str | None


@config_app.command("get")
def config_get_command(
    as_json: Annotated[bool, JSON_OPTION] = False,
    scope: Annotated[
        str | None,
        typer.Option(
            "--scope",
            help="Raw scope: user, tenant, tenant:discord/<guild_id>, channel:<channel_id>, "
            "channel:discord/<guild_id>/<channel_id>, deployment (read-only)",
        ),
    ] = None,
    channel: Annotated[
        str | None,
        typer.Option(
            "--channel", help="Channel for four-tier debug resolution: platform/workspace/channel"
        ),
    ] = None,
    account: Annotated[
        str | None,
        typer.Option("--account", help="Resolve for a different account (UUID)"),
    ] = None,
) -> None:
    if scope is not None and (channel is not None or account is not None):
        raise typer.BadParameter("--scope is mutually exclusive with --channel and --account")
    settings = load_settings()
    console = Console(highlight=False)
    run_cli(
        _config_get_command_entry(
            settings, as_json=as_json, scope_str=scope, channel_str=channel, account_str=account
        ),
        console=console,
    )


async def _config_get_command_entry(
    settings: Settings,
    *,
    as_json: bool,
    scope_str: str | None,
    channel_str: str | None,
    account_str: str | None,
) -> None:
    console = Console(highlight=False)
    async with build_runtime(settings) as rt, rt.sessionmaker() as session, session.begin():
        tenant_id = await discover_tenant(session)
        principal = await get_or_create_cli_principal(
            session, tenant_id=tenant_id, os_user=rt.settings.cli.local_user
        )
        account_override: uuid.UUID | None = None
        if account_str is not None:
            try:
                account_override = uuid.UUID(account_str)
            except ValueError as err:
                raise typer.BadParameter(f"invalid UUID: {account_str!r}") from err
        await _config_get_entry(
            session,
            tenant_id=tenant_id,
            account_id=principal.account_id,
            console=console,
            as_json=as_json,
            scope_str=scope_str,
            channel_str=channel_str,
            account_override=account_override,
            deployment_default=rt.deployment_default,
        )


async def _config_get_entry(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    console: Console,
    as_json: bool,
    scope_str: str | None,
    channel_str: str | None,
    account_override: uuid.UUID | None,
    deployment_default: DeploymentDefault,
) -> None:
    if scope_str is not None:
        # D-06/D-07: deployment is read-only; handled before _parse_scope
        if scope_str == "deployment":
            rows = [
                _RawRow(field="agent_name", value=deployment_default.agent_name),
                _RawRow(field="environment_name", value=deployment_default.environment_name),
            ]
            emit_rows(console, rows, columns=("field", "value"), as_json=as_json)
            return
        scope = _parse_scope(scope_str, tenant_id=tenant_id, account_id=account_id)
        row = await get_scope(session, scope=scope)
        if row is None:
            console.print("[dim]no config at this scope[/dim]")
            return
        raw_rows = [_RawRow(field=f, value=getattr(row, f, None)) for f in _ALL_FIELDS]
        emit_rows(console, raw_rows, columns=("field", "value"), as_json=as_json)
        return

    resolve_account = account_override or account_id
    channel_id: str | None = None

    if channel_str is not None:
        parts = channel_str.split("/")
        if len(parts) != 3:
            raise typer.BadParameter(
                f"invalid channel {channel_str!r}; expected platform/workspace_id/channel_id"
            )
        channel_id = parts[2]

    context = ScopeContext(
        account_id=resolve_account,
        tenant_id=tenant_id,
        channel_id=channel_id,
    )
    resolved = await resolve(session, context=context, default=deployment_default)

    effective_rows = [
        _EffectiveRow(
            field="agent_name",
            value=resolved.agent_name,
            tier=resolved.agent_name_tier,
        ),
        _EffectiveRow(
            field="environment_name",
            value=resolved.environment_name,
            tier=resolved.environment_name_tier,
        ),
    ]
    emit_rows(console, effective_rows, columns=("field", "value", "tier"), as_json=as_json)


# -- set -------------------------------------------------------------------


@config_app.command("set")
def config_set_command(
    kv: str,
    scope: Annotated[str, typer.Option("--scope", help="Target scope (default: user)")] = "user",
) -> None:
    settings = load_settings()
    key_raw, value = _parse_kv(kv)
    key = _validate_key(key_raw)
    console = Console(highlight=False)
    run_cli(
        _config_set_command_entry(settings, key=key, value=value, scope_str=scope), console=console
    )


async def _config_set_command_entry(
    settings: Settings,
    *,
    key: ConfigField,
    value: str,
    scope_str: str,
) -> None:
    console = Console(highlight=False)
    async with build_runtime(settings) as rt, rt.sessionmaker() as session, session.begin():
        tenant_id = await discover_tenant(session)
        principal = await get_or_create_cli_principal(
            session, tenant_id=tenant_id, os_user=rt.settings.cli.local_user
        )
        await _config_set_entry(
            session,
            tenant_id=tenant_id,
            account_id=principal.account_id,
            console=console,
            key=key,
            value=value,
            scope_str=scope_str,
        )


async def _config_set_entry(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    console: Console,
    key: ConfigField,
    value: str,
    scope_str: str,
) -> None:
    # D-06/D-08: deployment is read-only; handled before _parse_scope
    if scope_str == "deployment":
        console.print(
            "[yellow]The deployment default is read-only. "
            "Edit defaults/config.yaml and redeploy to change it.[/yellow]"
        )
        raise typer.Exit(1)
    scope = _parse_scope(scope_str, tenant_id=tenant_id, account_id=account_id)
    if key == "agent_name":
        await set_fields(
            session,
            scope=scope,
            tenant_id=tenant_id,
            agent_name=value,
            actor_account_id=account_id,
        )
    else:
        await set_fields(
            session,
            scope=scope,
            tenant_id=tenant_id,
            environment_name=value,
            actor_account_id=account_id,
        )
    console.print(f"[green]✓ set {key}={value} at {scope_str}[/green]")


# -- unset -----------------------------------------------------------------


@config_app.command("unset")
def config_unset_command(
    key: str,
    scope: Annotated[str, typer.Option("--scope", help="Target scope (default: user)")] = "user",
) -> None:
    settings = load_settings()
    validated = _validate_key(key)
    console = Console(highlight=False)
    run_cli(_config_unset_command_entry(settings, key=validated, scope_str=scope), console=console)


async def _config_unset_command_entry(
    settings: Settings,
    *,
    key: ConfigField,
    scope_str: str,
) -> None:
    console = Console(highlight=False)
    async with build_runtime(settings) as rt, rt.sessionmaker() as session, session.begin():
        tenant_id = await discover_tenant(session)
        principal = await get_or_create_cli_principal(
            session, tenant_id=tenant_id, os_user=rt.settings.cli.local_user
        )
        await _config_unset_entry(
            session,
            tenant_id=tenant_id,
            account_id=principal.account_id,
            console=console,
            key=key,
            scope_str=scope_str,
        )


async def _config_unset_entry(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    console: Console,
    key: ConfigField,
    scope_str: str,
) -> None:
    scope = _parse_scope(scope_str, tenant_id=tenant_id, account_id=account_id)
    await unset_fields(session, scope=scope, fields=[key], actor_account_id=account_id)
    console.print(f"[green]✓ unset {key} at {scope_str}[/green]")


# -- propagate -------------------------------------------------------------


@config_app.command("propagate")
def config_propagate_command(
    to: Annotated[list[str], typer.Option("--to", help="Target scope(s)")],
    from_scope: Annotated[
        str, typer.Option("--from", help="Source scope (default: user)")
    ] = "user",
    fields_str: Annotated[
        str | None, typer.Option("--fields", help="Comma-separated field names")
    ] = None,
    reset: Annotated[bool, typer.Option("--reset", help="Clear fields at target")] = False,
    as_json: Annotated[bool, JSON_OPTION] = False,
) -> None:
    settings = load_settings()
    console = Console(highlight=False)
    run_cli(
        _config_propagate_command_entry(
            settings, to_strs=to, from_str=from_scope, fields_str=fields_str, reset=reset
        ),
        console=console,
    )


async def _config_propagate_command_entry(
    settings: Settings,
    *,
    to_strs: list[str],
    from_str: str,
    fields_str: str | None,
    reset: bool,
) -> None:
    console = Console(highlight=False)
    async with build_runtime(settings) as rt, rt.sessionmaker() as session, session.begin():
        tenant_id = await discover_tenant(session)
        principal = await get_or_create_cli_principal(
            session, tenant_id=tenant_id, os_user=rt.settings.cli.local_user
        )
        await _config_propagate_entry(
            session,
            tenant_id=tenant_id,
            account_id=principal.account_id,
            console=console,
            to_strs=to_strs,
            from_str=from_str,
            fields_str=fields_str,
            reset=reset,
        )


async def _config_propagate_entry(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    console: Console,
    to_strs: list[str],
    from_str: str,
    fields_str: str | None,
    reset: bool,
) -> None:
    source = _parse_scope(from_str, tenant_id=tenant_id, account_id=account_id)
    targets = [_parse_scope(t, tenant_id=tenant_id, account_id=account_id) for t in to_strs]

    fields: list[ConfigField] | None = None
    if fields_str is not None:
        raw_fields = [f.strip() for f in fields_str.split(",")]
        fields = [_validate_key(f) for f in raw_fields]

    result = await propagate(
        session,
        tenant_id=tenant_id,
        source=source,
        target=targets,
        fields=fields,
        reset=reset,
        actor_account_id=account_id,
    )
    # When all outcomes have no fields written, the source had nothing to copy.
    if all(not outcome.fields_written for outcome in result.outcomes):
        console.print("[dim]nothing to propagate: source has no configured fields.[/dim]")
        return
    for outcome in result.outcomes:
        scope_label = _scope_label(outcome.scope)
        if outcome.fields_written:
            written = ", ".join(outcome.fields_written)
            console.print(f"[green]✓ propagated to {scope_label}: {written}[/green]")
        else:
            console.print(f"[dim]no fields written to {scope_label}[/dim]")


def _scope_label(scope: ScopeRef) -> str:
    if isinstance(scope, UserScopeRef):
        return "user"
    if isinstance(scope, TenantScopeRef):
        return "tenant"
    # ChannelScopeRef is the only remaining variant.
    assert isinstance(scope, ChannelScopeRef)
    return f"channel:{scope.channel_id}"
