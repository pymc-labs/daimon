"""daimon tenants ... sub-app."""

from __future__ import annotations

import json
import re
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Literal

import httpx
import typer
from daimon.adapters.cli.errors import run_cli
from daimon.adapters.cli.flags import JSON_OPTION, YES_OPTION
from daimon.adapters.cli.output import emit_rows, render_json
from daimon.adapters.cli.prompt import confirm_or_abort
from daimon.adapters.cli.runtime import CliRuntime, build_runtime
from daimon.core import channel_backend
from daimon.core.access_policy import (
    OPEN_ACCESS_POLICY,
    TenantAccessPolicy,
    is_discord_snowflake,
)
from daimon.core.config import load_settings
from daimon.core.errors import StoreError
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.native_admins import verify_backend_actor
from daimon.core.permissions import (
    ChannelReaders,
    ChannelRule,
    ChannelWriters,
    agent_permissions,
)
from daimon.core.stores import tenant_ledger, tenant_user_caps
from daimon.core.stores.access_policy import (
    AccessPolicyUnreadable,
    clear_access_policy,
    load_access_policy,
    lock_access_policy,
    lock_policy_writes_exclusive,
    policy_write_transaction,
    set_access_policy,
)
from daimon.core.stores.domain import Platform, TenantBackendPolicy
from daimon.core.stores.tenants import (
    delete_tenant,
    get_tenant,
    get_tenant_dependent_counts,
    list_tenants_by_platform,
    set_funding_mode,
    set_turn_cap,
)
from mux.contracts.ids import Provider
from pydantic import BaseModel, Field, TypeAdapter, ValidationError
from rich.console import Console
from rich.markup import escape
from rich.table import Table

tenants_app = typer.Typer(help="Tenants: list, credit, caps, funding and access policy, delete.")
access_policy_app = typer.Typer(
    help=("A tenant's access policy: who may invoke the agent, and its channel and agent rules.")
)
tenants_app.add_typer(access_policy_app, name="access-policy")


class TenantPolicySnapshot(TenantBackendPolicy):
    """Policy data for an explicit compare-and-set; never saved authorization."""

    platform: str
    external_id: str
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")


@tenants_app.command("backend-policy")
def tenants_backend_policy_command(
    operation: str,
    platform: str,
    external_id: str,
    actor_user: Annotated[
        str, typer.Option("--actor-user", help="Current platform admin user ID.")
    ],
    snapshot: Annotated[Path | None, typer.Option("--snapshot")] = None,
    output: Annotated[Path | None, typer.Option("--output")] = None,
    backends: Annotated[
        str | None,
        typer.Option(
            "--backends",
            help="Comma-separated providers, or 'default' to follow the keyed providers.",
        ),
    ] = None,
    expected_revision: Annotated[str | None, typer.Option("--expected-revision")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    yes: Annotated[bool, YES_OPTION] = False,
) -> None:
    """Snapshot, apply or revert a tenant backend allowlist with fresh admin verification."""
    settings, console = load_settings(), Console(highlight=False)

    async def run() -> None:
        async with build_runtime(settings) as rt:
            await tenants_backend_policy(
                rt=rt,
                console=console,
                operation=operation,
                platform=platform,
                external_id=external_id,
                actor_user=actor_user,
                snapshot=snapshot,
                output=output,
                backends=backends,
                expected_revision=expected_revision,
                dry_run=dry_run,
                yes=yes,
            )

    run_cli(run(), console=console)


async def tenants_backend_policy(
    *,
    rt: CliRuntime,
    console: Console,
    operation: str,
    platform: str,
    external_id: str,
    actor_user: str,
    snapshot: Path | None = None,
    output: Path | None = None,
    backends: str | None = None,
    expected_revision: str | None = None,
    dry_run: bool = False,
    yes: bool = False,
    http_client: httpx.AsyncClient | None = None,
) -> None:
    """Use the shared attestation and policy owners, including for preview and rollback."""
    if operation not in ("snapshot", "apply", "revert") or not actor_user.strip():
        raise typer.BadParameter("choose snapshot, apply or revert and a current admin user")
    if operation == "snapshot":
        if (
            output is None
            or snapshot is not None
            or backends is not None
            or expected_revision is not None
        ):
            raise typer.BadParameter("snapshot requires only --output and --actor-user")
    elif snapshot is None or output is not None:
        raise typer.BadParameter("apply/revert require --snapshot; --output is snapshot-only")
    elif (operation == "apply" and (backends is None or expected_revision is not None)) or (
        operation == "revert" and (backends is not None or not expected_revision)
    ):
        raise typer.BadParameter("apply requires --backends; revert requires --expected-revision")
    tenant_id = await _existing_tenant_id(rt, platform=platform, external_id=external_id)
    saved = None
    if snapshot is not None:
        try:
            with snapshot.open("rb") as file:
                content = file.read(65_537)
            if len(content) > 65_536:
                raise ValueError("snapshot too large")
            saved = TenantPolicySnapshot.model_validate_json(content)
        except (OSError, ValueError) as exc:
            raise typer.BadParameter("invalid policy snapshot") from exc
        if (saved.tenant_id, saved.platform, saved.external_id) != (
            tenant_id,
            platform,
            external_id,
        ):
            raise typer.BadParameter("policy snapshot belongs to another tenant or workspace")
    service = channel_backend.make_backend_service(rt.sessionmaker, rt.settings.turn)

    async def actor() -> channel_backend.BackendActor:
        return await verify_backend_actor(
            rt.sessionmaker,
            tenant_id=tenant_id,
            platform_user_id=actor_user,
            settings=rt.settings,
            http_client=http_client,
        )

    verified = await actor()
    before = await service.tenant_policy(tenant_id)
    if operation == "snapshot":
        captured = TenantPolicySnapshot(
            **before.model_dump(), platform=platform, external_id=external_id
        )
        if not dry_run:
            assert output is not None
            try:
                with output.open("x", encoding="utf-8") as file:
                    file.write(captured.model_dump_json() + "\n")
            except OSError as exc:
                raise StoreError("could not create policy snapshot; no policy was changed") from exc
        console.print(captured.model_dump_json(), markup=False)
        return
    assert saved is not None
    revision = saved.revision if operation == "apply" else expected_revision
    assert revision is not None
    try:
        # `--backends default` and a snapshot taken while defaulted both go back to
        # following the deployment's keyed providers.
        if operation == "apply":
            proposed: tuple[Provider, ...] | None = (
                None
                if (backends or "").strip() == "default"
                else TypeAdapter(tuple[Provider, ...]).validate_python(
                    tuple((backends or "").split(","))
                )
            )
        else:
            proposed = saved.enabled_backends if saved.explicit else None
        if not dry_run:
            confirm_or_abort(
                console, f"{operation} backend policy for {platform}:{external_id}?", yes=yes
            )
            verified = await actor()
        result = await service.set_tenant_allowlist(
            tenant_id, proposed, revision, verified, dry_run=dry_run
        )
    except ValidationError as exc:
        raise typer.BadParameter("invalid backend allowlist") from exc
    readback = None
    try:
        readback = await service.tenant_policy(tenant_id)
    finally:
        console.print_json(
            data={
                "operation": operation,
                "dry_run": dry_run,
                "expected_revision": revision,
                "before": before.model_dump(mode="json"),
                "result": result.model_dump(mode="json"),
                "readback": readback.model_dump(mode="json") if readback is not None else None,
            }
        )
    if (not dry_run and readback != result) or (dry_run and readback != before):
        raise StoreError("policy changed during independent readback; CAS result is in the receipt")


_VALID_PLATFORMS = ("discord", "cli", "slack", "teams")
_ENTRA_OBJECT_ID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
# A Teams channel thread is its channel id plus ";messageid=<root post>".
_TEAMS_CHANNEL_ID = r"19:[^\s;]+@thread\.[a-z0-9]+(?:;messageid=[0-9]+)?"
# What a pin or isolation names: a whole team channel, never a thread or group chat.
_TEAMS_WHOLE_CHANNEL_ID = r"19:[^\s;]+@thread\.(?:tacv2|skype)"


def _validate_platform(value: str) -> Platform:
    if value in _VALID_PLATFORMS:
        return value  # type: ignore[return-value]
    raise typer.BadParameter(
        f"unsupported platform {value!r}; valid: {', '.join(_VALID_PLATFORMS)}"
    )


@tenants_app.command("list")
def tenants_list_command(
    platform: str | None = typer.Option(default=None, help="Filter by platform."),
    as_json: Annotated[bool, JSON_OPTION] = False,
) -> None:
    settings = load_settings()
    console = Console(highlight=False)

    async def _with_defaults() -> None:
        async with build_runtime(settings) as rt:
            await tenants_list(rt=rt, console=console, platform=platform, as_json=as_json)

    run_cli(_with_defaults(), console=console)


async def tenants_list(
    *,
    rt: CliRuntime,
    console: Console,
    platform: str | None,
    as_json: bool,
) -> None:
    validated_platform: Platform | None = None
    if platform is not None:
        validated_platform = _validate_platform(platform)
    rows = await list_tenants_by_platform(rt.sessionmaker, platform=validated_platform)
    # Sort by (platform, external_id) for stable operator-readable output.
    rows = sorted(rows, key=lambda r: (r.platform, r.external_id))
    emit_rows(
        console,
        rows,
        columns=(
            "platform",
            "external_id",
            "funding_mode",
            "provision_status",
            "registered_at",
            "archived_at",
        ),
        as_json=as_json,
    )


@tenants_app.command("delete")
def tenants_delete_command(
    platform: str,
    external_id: str,
    cascade: Annotated[
        bool, typer.Option("--cascade", help="Delete even when dependents exist.")
    ] = False,
    yes: Annotated[bool, YES_OPTION] = False,
) -> None:
    settings = load_settings()
    console = Console(highlight=False)

    async def _with_defaults() -> None:
        async with build_runtime(settings) as rt:
            await tenants_delete(
                rt=rt,
                console=console,
                platform=platform,
                external_id=external_id,
                cascade=cascade,
                yes=yes,
            )

    run_cli(_with_defaults(), console=console)


async def tenants_delete(
    *,
    rt: CliRuntime,
    console: Console,
    platform: str,
    external_id: str,
    cascade: bool,
    yes: bool,
) -> None:
    validated_platform = _validate_platform(platform)
    tenant_id = derive_tenant_uuid(platform=validated_platform, workspace_id=external_id)

    async with rt.sessionmaker() as session, session.begin():
        counts = await get_tenant_dependent_counts(session, tenant_id=tenant_id)

    if counts.total > 0 and not cascade:
        raise StoreError(
            f"tenant has dependents (use --cascade to force): "
            f"routines={counts.routines}, "
            f"usage_events={counts.usage_events}, "
            f"payment_events={counts.payment_events}, "
            f"tenant_ledger={counts.tenant_ledger}, "
            f"tenant_user_caps={counts.tenant_user_caps}, "
            f"agent_files={counts.agent_files}, "
            f"agent_github_grants={counts.agent_github_grants}, "
            f"tenant_config={counts.tenant_config}, "
            f"channel_config={counts.channel_config}"
        )

    if counts.total > 0:
        console.print(
            f"[yellow]blast radius for {platform}:{external_id}:[/yellow]\n"
            f"  routines={counts.routines}, "
            f"usage_events={counts.usage_events}, "
            f"payment_events={counts.payment_events}, "
            f"tenant_ledger={counts.tenant_ledger}, "
            f"tenant_user_caps={counts.tenant_user_caps}, "
            f"agent_files={counts.agent_files}, "
            f"agent_github_grants={counts.agent_github_grants}, "
            f"tenant_config={counts.tenant_config}, "
            f"channel_config={counts.channel_config}"
        )

    confirm_or_abort(console, f"delete tenant {platform}:{external_id}?", yes=yes)

    async with rt.sessionmaker() as session, session.begin():
        await delete_tenant(session, tenant_id=tenant_id)

    console.print(f"[green]✓ deleted tenant {platform}:{external_id}[/green]")


@tenants_app.command("funding-mode")
def tenants_funding_mode_command(platform: str, external_id: str, mode: str) -> None:
    """Set a tenant to prepaid or operator_funded; retain usage and caps."""
    settings = load_settings()
    console = Console(highlight=False)

    async def _with_runtime() -> None:
        async with build_runtime(settings) as rt:
            await tenants_funding_mode(
                rt=rt, console=console, platform=platform, external_id=external_id, mode=mode
            )

    run_cli(_with_runtime(), console=console)


async def tenants_funding_mode(
    *, rt: CliRuntime, console: Console, platform: str, external_id: str, mode: str
) -> None:
    validated_platform = _validate_platform(platform)
    if mode not in ("prepaid", "operator_funded"):
        raise typer.BadParameter("mode must be prepaid or operator_funded")
    tenant_id = derive_tenant_uuid(platform=validated_platform, workspace_id=external_id)
    async with rt.sessionmaker() as session, session.begin():
        row = await set_funding_mode(session, tenant_id=tenant_id, funding_mode=mode)
    console.print(f"{platform}:{external_id} funding mode: {row.funding_mode}")


@tenants_app.command("turn-cap")
def tenants_turn_cap_command(platform: str, workspace_id: str, value: str) -> None:
    """Set a tenant's concurrent-turn cap, or restore the deployment default."""
    settings = load_settings()
    console = Console(highlight=False)

    async def _with_runtime() -> None:
        async with build_runtime(settings) as rt:
            await tenants_turn_cap(
                rt=rt, console=console, platform=platform, workspace_id=workspace_id, value=value
            )

    run_cli(_with_runtime(), console=console)


async def tenants_turn_cap(
    *, rt: CliRuntime, console: Console, platform: str, workspace_id: str, value: str
) -> None:
    if value == "default":
        cap = None
    else:
        try:
            cap = int(value)
        except ValueError as exc:
            raise typer.BadParameter("turn cap must be a positive integer or default") from exc
        if cap < 1 or str(cap) != value:
            raise typer.BadParameter("turn cap must be a positive integer or default")
    tenant_id = derive_tenant_uuid(platform=_validate_platform(platform), workspace_id=workspace_id)
    async with rt.sessionmaker() as session, session.begin():
        row = await set_turn_cap(session, tenant_id=tenant_id, cap=cap)
    console.print(f"{platform}:{workspace_id} turn cap: {row.turn_cap or 'default'}")


async def _existing_tenant_id(rt: CliRuntime, *, platform: str, external_id: str) -> uuid.UUID:
    tenant_id = derive_tenant_uuid(platform=_validate_platform(platform), workspace_id=external_id)
    async with rt.sessionmaker() as session:
        if await get_tenant(session, tenant_id) is None:
            raise StoreError(f"no tenant {platform}:{external_id}")
    return tenant_id


def _usd(value: str, *, positive: bool) -> Decimal:
    try:
        amount = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise typer.BadParameter("usd must be a dollar amount") from exc
    if not amount.is_finite() or amount < 0 or (positive and amount == 0):
        raise typer.BadParameter(
            "usd must be a positive dollar amount" if positive else "usd must be nonnegative"
        )
    exponent = amount.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -2:
        raise typer.BadParameter("usd must have at most two decimal places")
    return amount


@tenants_app.command("credit")
def tenants_credit_command(
    platform: str,
    workspace_id: str,
    usd: str,
    note: Annotated[str, typer.Option("--note", help="Operator note for this credit.")],
    request_id: Annotated[
        str | None, typer.Option("--id", help="Reuse this id to safely retry the credit.")
    ] = None,
) -> None:
    settings = load_settings()
    console = Console(highlight=False)

    async def _with_runtime() -> None:
        async with build_runtime(settings) as rt:
            await tenants_credit(
                rt=rt,
                console=console,
                platform=platform,
                workspace_id=workspace_id,
                usd=usd,
                note=note,
                request_id=request_id,
            )

    run_cli(_with_runtime(), console=console)


async def tenants_credit(
    *,
    rt: CliRuntime,
    console: Console,
    platform: str,
    workspace_id: str,
    usd: str,
    note: str,
    request_id: str | None,
) -> None:
    amount = _usd(usd, positive=True)
    if not note.strip():
        raise typer.BadParameter("note must not be empty")
    note_slug = re.sub(r"[_\W]+", "-", note.strip().lower()).strip("-")[:64].rstrip("-")
    if not note_slug:
        raise typer.BadParameter("note must contain letters or numbers")
    tenant_id = await _existing_tenant_id(rt, platform=platform, external_id=workspace_id)
    request_id = request_id or str(uuid.uuid4())
    key = f"manual:credit:{tenant_id}:{note_slug}:usd{amount:.2f}:{request_id}"
    async with rt.sessionmaker() as session, session.begin():
        inserted = await tenant_ledger.insert_entry(
            session,
            tenant_id=tenant_id,
            delta_usd=amount,
            reason="manual_credit",
            idempotency_key=key,
        )
        balance = await tenant_ledger.get_balance(session, tenant_id=tenant_id)
    status = "credited" if inserted else "already credited"
    console.print(f"{platform}:{workspace_id} balance: ${balance:.2f} ({status})")
    console.print(f"credit id: {request_id}")
    console.print(f"idempotency key: {key}")


@tenants_app.command("cap")
def tenants_cap_command(
    platform: str,
    workspace_id: str,
    usd: str,
    user: Annotated[str | None, typer.Option("--user", help="Platform user id override.")] = None,
) -> None:
    settings = load_settings()
    console = Console(highlight=False)

    async def _with_runtime() -> None:
        async with build_runtime(settings) as rt:
            await tenants_cap(
                rt=rt,
                console=console,
                platform=platform,
                workspace_id=workspace_id,
                usd=usd,
                user=user,
            )

    run_cli(_with_runtime(), console=console)


async def tenants_cap(
    *,
    rt: CliRuntime,
    console: Console,
    platform: str,
    workspace_id: str,
    usd: str,
    user: str | None,
) -> None:
    amount = _usd(usd, positive=False)
    tenant_id = await _existing_tenant_id(rt, platform=platform, external_id=workspace_id)
    if user == "":
        raise typer.BadParameter("user must not be empty")
    async with rt.sessionmaker() as session, session.begin():
        if user is None:
            await tenant_user_caps.set_default(session, tenant_id=tenant_id, amount=amount)
        else:
            await tenant_user_caps.set_override(
                session, tenant_id=tenant_id, user_id=user, amount=amount
            )
    console.print(f"{platform}:{workspace_id} monthly cap for {user or 'default'}: ${amount:.2f}")


def _print_policy(
    console: Console, *, label: str, policy: TenantAccessPolicy, as_json: bool
) -> None:
    if as_json:
        exclude = (
            {"enabled_backends"}
            if policy.enabled_backends == OPEN_ACCESS_POLICY.enabled_backends
            else None
        )
        console.print_json(policy.model_dump_json(exclude=exclude))
        return
    if policy == OPEN_ACCESS_POLICY:
        console.print(f"{label} access policy: open (anyone may invoke; no rules)")
        console.print("  invoker_admin_bypass: true")
        return
    console.print(f"{label} access policy:")
    console.print(f"  internal_role_ids: {', '.join(policy.internal_role_ids) or '-'}")
    console.print(f"  invoker_role_ids: {', '.join(policy.invoker_role_ids) or '-'}")
    console.print(f"  invoker_user_ids: {', '.join(policy.invoker_user_ids) or '-'}")
    console.print(f"  invoker_admin_bypass: {str(policy.invoker_admin_bypass).lower()}")
    console.print(f"  dm_memory_read_only: {str(policy.dm_memory_read_only).lower()}")
    rules = len(policy.channel_rules) + len(policy.category_rules) + len(policy.agent_rules)
    console.print(f"  rules: {rules or '-'} (see access-policy rules)")


@access_policy_app.command("get")
def tenants_access_policy_get_command(
    platform: str, external_id: str, as_json: Annotated[bool, JSON_OPTION] = False
) -> None:
    """Show a tenant's access policy. A tenant with none is open."""
    settings = load_settings()
    console = Console(highlight=False)

    async def _with_runtime() -> None:
        async with build_runtime(settings) as rt:
            await tenants_access_policy_get(
                rt=rt, console=console, platform=platform, external_id=external_id, as_json=as_json
            )

    run_cli(_with_runtime(), console=console)


async def tenants_access_policy_get(
    *, rt: CliRuntime, console: Console, platform: str, external_id: str, as_json: bool
) -> None:
    tenant_id = await _existing_tenant_id(rt, platform=platform, external_id=external_id)
    async with rt.sessionmaker() as session:
        policy = await load_access_policy(session, tenant_id=tenant_id)
    _print_policy(console, label=f"{platform}:{external_id}", policy=policy, as_json=as_json)


class PermissionRuleRow(BaseModel):
    """One channel, category or agent rule of a tenant's access policy."""

    kind: Literal["channel", "category", "agent"]
    id: str
    readers: ChannelReaders | None
    writers: ChannelWriters | None
    runs_in: list[str] | None
    home: str | None
    """For an agent: the channel kept to its own agents it is one of."""


def permission_rule_rows(policy: TenantAccessPolicy) -> list[PermissionRuleRow]:
    """The policy's rules, channels first (`daimon.core.permissions`)."""
    places: list[tuple[Literal["channel", "category"], dict[str, ChannelRule]]] = [
        ("channel", policy.channel_rules),
        ("category", policy.category_rules),
    ]
    rows = [
        PermissionRuleRow(
            kind=kind,
            id=place_id,
            readers=rule.readers,
            writers=rule.writers,
            runs_in=None,
            home=None,
        )
        for kind, rules in places
        for place_id, rule in sorted(rules.items())
    ]
    rows += [
        PermissionRuleRow(
            kind="agent",
            id=name,
            readers=None,
            writers=None,
            runs_in=None if rule.runs_in is None else list(rule.runs_in),
            home=agent_permissions(policy, (name,)).home,
        )
        for name, rule in sorted(policy.agent_rules.items())
    ]
    return rows


def _print_rule_rows(console: Console, rows: list[PermissionRuleRow]) -> None:
    table = Table(show_header=True, header_style="bold")
    for column in PermissionRuleRow.model_fields:
        table.add_column(column)
    for row in rows:
        runs_in = "-" if row.runs_in is None else ", ".join(row.runs_in) or "nowhere"
        cells = (row.kind, row.id, row.readers, row.writers, runs_in, row.home)
        table.add_row(*(escape(cell or "-") for cell in cells))
    console.print(table)


@access_policy_app.command("rules")
def tenants_access_policy_rules_command(
    platform: str, external_id: str, as_json: Annotated[bool, JSON_OPTION] = False
) -> None:
    """Show a tenant's channel, category and agent rules.

    A channel rule says who reads it (any, inside, own) and who posts there
    (any, own, none); an agent rule says where it runs. An agent whose rule
    names a channel kept to its own agents alone is one of them: that is its
    home. Change them with `daimon channels rule set` and `daimon agents rule set`.
    """
    settings = load_settings()
    console = Console(highlight=False)

    async def _with_runtime() -> None:
        async with build_runtime(settings) as rt:
            await tenants_access_policy_rules(
                rt=rt, console=console, platform=platform, external_id=external_id, as_json=as_json
            )

    run_cli(_with_runtime(), console=console)


async def tenants_access_policy_rules(
    *, rt: CliRuntime, console: Console, platform: str, external_id: str, as_json: bool
) -> None:
    tenant_id = await _existing_tenant_id(rt, platform=platform, external_id=external_id)
    async with rt.sessionmaker() as session:
        policy = await load_access_policy(session, tenant_id=tenant_id)
    rows = permission_rule_rows(policy)
    if not rows and not as_json:
        console.print(f"{platform}:{external_id}: no rules; every channel and agent open")
        return
    if as_json:
        render_json(console, rows)
    else:
        _print_rule_rows(console, rows)


@access_policy_app.command("set")
def tenants_access_policy_set_command(
    platform: str,
    external_id: str,
    invoker: Annotated[
        list[str] | None,
        typer.Option(
            help=(
                "Platform user id allowed to start turns (repeatable). "
                "Admins bypass the lists only while invoker_admin_bypass is on."
            )
        ),
    ] = None,
    invoker_role: Annotated[
        list[str] | None,
        typer.Option(
            help="Discord role id allowed to start turns (repeatable). Replaces the role list."
        ),
    ] = None,
    internal_role: Annotated[
        list[str] | None,
        typer.Option(
            help=(
                "Operator-only: internal Discord role id (repeatable). "
                "Replaces internal roles for outsider visibility checks."
            )
        ),
    ] = None,
    clear_internal_roles: Annotated[
        bool,
        typer.Option(
            help="Operator-only: clear internal roles; outsider checks then ask you to name them."
        ),
    ] = False,
    clear_invoker_roles: Annotated[
        bool, typer.Option("--clear-invoker-roles", help="Clear the Discord invoker role list.")
    ] = False,
    invoker_admin_bypass: Annotated[
        bool | None,
        typer.Option(
            "--invoker-admin-bypass/--no-invoker-admin-bypass",
            help="Allow server admins to bypass the invoker user/role lists.",
        ),
    ] = None,
    dm_memory_read_only: Annotated[
        bool | None,
        typer.Option(
            "--dm-memory-read-only/--no-dm-memory-read-only",
            help="Give turns started from a DM read-only memory.",
        ),
    ] = None,
    drop_agent_rules: Annotated[
        bool,
        typer.Option(
            "--drop-agent-rules",
            help="Let --clear drop agent rules too, leaving those agents running anywhere.",
        ),
    ] = False,
    clear: Annotated[
        bool,
        typer.Option(
            "--clear",
            help="Operator-only: remove the whole policy, including internal roles and every rule.",
        ),
    ] = False,
    as_json: Annotated[bool, JSON_OPTION] = False,
) -> None:
    """Deployment-operator command: set access policy using deployment DB/config access.

    All flags, including --internal-role, --clear-internal-roles and --clear,
    use the operator trust boundary; no Discord identity or platform-admin
    attestation is checked. Server admins use the in-product Permissions panel.

    --invoker replaces the stored list.
    Rules are set with `daimon channels rule set` and `daimon agents rule set`.
    --clear drops the whole policy, rules included; with agent rules stored it
    needs --drop-agent-rules, since those agents would then run anywhere.
    """
    settings = load_settings()
    console = Console(highlight=False)

    async def _with_runtime() -> None:
        async with build_runtime(settings) as rt:
            await tenants_access_policy_set(
                rt=rt,
                console=console,
                platform=platform,
                external_id=external_id,
                invoker=invoker,
                invoker_role=invoker_role,
                internal_role=internal_role,
                clear_internal_roles=clear_internal_roles,
                clear_invoker_roles=clear_invoker_roles,
                invoker_admin_bypass=invoker_admin_bypass,
                dm_memory_read_only=dm_memory_read_only,
                drop_agent_rules=drop_agent_rules,
                clear=clear,
                as_json=as_json,
            )

    run_cli(_with_runtime(), console=console)


def _parse_invokers(values: list[str], *, platform: str) -> tuple[str, ...]:
    if not values:
        raise typer.BadParameter("invoker_user_ids: pass at least one non-empty id")
    pattern = (
        r"[0-9]{15,21}"
        if platform == "discord"
        else _ENTRA_OBJECT_ID
        if platform == "teams"
        else r"[UW][A-Z0-9]+"
    )
    for value in values:
        cleaned = value.strip()
        if not cleaned or (platform != "cli" and re.fullmatch(pattern, cleaned) is None):
            raise typer.BadParameter(f"invoker_user_ids: invalid {platform} id {value!r}")
    return tuple(dict.fromkeys(value.strip() for value in values))


async def tenants_access_policy_set(
    *,
    rt: CliRuntime,
    console: Console,
    platform: str,
    external_id: str,
    invoker: list[str] | None = None,
    invoker_role: list[str] | None = None,
    clear_invoker_roles: bool = False,
    internal_role: list[str] | None = None,
    clear_internal_roles: bool = False,
    invoker_admin_bypass: bool | None = None,
    dm_memory_read_only: bool | None = None,
    drop_agent_rules: bool = False,
    clear: bool = False,
    as_json: bool = False,
) -> None:
    validated_platform = _validate_platform(platform)
    changes: dict[str, object] = {}
    if internal_role is not None and clear_internal_roles:
        raise typer.BadParameter("--internal-role cannot be combined with --clear-internal-roles")
    if internal_role is not None or clear_internal_roles:
        if validated_platform != "discord":
            raise typer.BadParameter("internal_role_ids are supported only for Discord")
        roles = tuple(value.strip() for value in (internal_role or []))
        if any(not is_discord_snowflake(value) or value == external_id for value in roles):
            raise typer.BadParameter(
                "internal_role_ids: expected Discord snowflakes excluding @everyone (guild id)"
            )
        changes["internal_role_ids"] = tuple(dict.fromkeys(roles))
    if invoker_role is not None and clear_invoker_roles:
        raise typer.BadParameter("--invoker-role cannot be combined with --clear-invoker-roles")
    if invoker_role is not None:
        if validated_platform != "discord":
            raise typer.BadParameter("invoker_role_ids are supported only for Discord")
        roles = tuple(value.strip() for value in invoker_role)
        if any(not is_discord_snowflake(value) or value == external_id for value in roles):
            raise typer.BadParameter(
                "invoker_role_ids: expected Discord snowflakes excluding @everyone (guild id)"
            )
        changes["invoker_role_ids"] = tuple(dict.fromkeys(roles))
    if clear_invoker_roles:
        changes["invoker_role_ids"] = ()
    if invoker is not None:
        changes["invoker_user_ids"] = _parse_invokers(invoker, platform=validated_platform)
    if invoker_admin_bypass is not None:
        changes["invoker_admin_bypass"] = invoker_admin_bypass
    if dm_memory_read_only is not None:
        changes["dm_memory_read_only"] = dm_memory_read_only
    if drop_agent_rules and not clear:
        raise typer.BadParameter("--drop-agent-rules only applies to --clear")
    if clear and changes:
        raise typer.BadParameter("--clear can't be combined with other policy flags")
    if not clear and not changes:
        raise typer.BadParameter("nothing to set: pass a policy flag or --clear")

    label = f"{platform}:{external_id}"
    tenant_id = await _existing_tenant_id(rt, platform=platform, external_id=external_id)
    dropped: list[str] = []
    lifted = False
    async with policy_write_transaction(rt.sessionmaker, tenant_id=tenant_id) as session:
        await lock_policy_writes_exclusive(session, tenant_id=tenant_id)
        await lock_access_policy(session, tenant_id=tenant_id)
        if clear:
            try:
                current = await load_access_policy(session, tenant_id=tenant_id)
                if current.approvals != "inherit" or any(
                    rule.approvals != "inherit" for rule in current.channel_rules.values()
                ):
                    raise typer.BadParameter(
                        "clear approval overrides with `daimon channels approvals clear` "
                        "first; each change requires a native admin and an audit event. "
                        "Nothing was changed."
                    )
                dropped = sorted(current.agent_rules)
            except AccessPolicyUnreadable:
                # --clear is the way out of an unreadable row, but the row may
                # hold agent rules nobody can list: ask all the same.
                if not drop_agent_rules:
                    raise typer.BadParameter(
                        "the stored policy can't be read, so its agent rules can't be listed; "
                        "pass --drop-agent-rules with --clear to drop it anyway. Nothing was "
                        "changed."
                    ) from None
            if dropped and not drop_agent_rules:
                raise typer.BadParameter(
                    f"--clear would drop every agent rule and leave {', '.join(dropped)} "
                    "running anywhere. Pass --drop-agent-rules to clear them too. Nothing "
                    "was changed."
                )
            try:
                lifted = (await load_access_policy(session, tenant_id=tenant_id)).backends_explicit
            except AccessPolicyUnreadable:
                lifted = False  # --clear is the way out of an unreadable row
            await clear_access_policy(session, tenant_id=tenant_id)
            policy = OPEN_ACCESS_POLICY
        else:
            # An unreadable stored row raises here rather than being overwritten
            # blind; --clear is the way out of that state.
            current = await load_access_policy(session, tenant_id=tenant_id)
            try:
                policy = TenantAccessPolicy.model_validate(current.model_dump() | changes)
            except ValidationError as exc:
                message = "; ".join(str(error["msg"]) for error in exc.errors())
                raise typer.BadParameter(f"{message}. Nothing was changed.") from None
            await set_access_policy(session, tenant_id=tenant_id, policy=policy)
    _print_policy(console, label=label, policy=policy, as_json=as_json)
    if not as_json:
        if lifted:
            console.print(
                "  its saved provider list was cleared too: it now uses every provider "
                "this install has a key for"
            )
        for name in dropped:
            console.print(f"  {name} has no rule now (runs anywhere)")


@tenants_app.command("dm-policy")
def tenants_dm_policy_command(
    platform: str,
    external_id: str,
    agent: Annotated[
        str | None, typer.Option("--agent", help="Tenant-owned cold DM agent name.")
    ] = None,
    clear_agent: Annotated[
        bool, typer.Option("--clear-agent", help="Disable cold DM discovery.")
    ] = False,
    enabled: Annotated[bool | None, typer.Option("--enabled/--disabled")] = None,
    json_output: Annotated[bool, JSON_OPTION] = False,
) -> None:
    """Inspect or update tenant DMs; an unset agent leaves /dm-me moves available."""

    console = Console()

    async def run() -> None:
        async with build_runtime(load_settings()) as runtime:
            result = await tenants_dm_policy(
                rt=runtime,
                platform=platform,
                external_id=external_id,
                agent=agent,
                clear_agent=clear_agent,
                enabled=enabled,
            )
        console.print(json.dumps(result) if json_output else result, markup=False)

    run_cli(run(), console=console)


async def tenants_dm_policy(
    *,
    rt: CliRuntime,
    platform: str,
    external_id: str,
    agent: str | None = None,
    clear_agent: bool = False,
    enabled: bool | None = None,
) -> dict[str, str | bool | None]:
    from daimon.core.stores.direct_messages import (
        dm_enabled,
        get_dm_agent_name,
        set_dm_agent_name,
        set_dm_enabled,
    )

    if agent is not None and clear_agent:
        raise typer.BadParameter("--agent and --clear-agent are mutually exclusive")
    tenant_id = derive_tenant_uuid(platform=_validate_platform(platform), workspace_id=external_id)
    async with rt.sessionmaker.begin() as session:
        tenant = await get_tenant(session, tenant_id)
        if tenant is None:
            raise typer.BadParameter("tenant not found")
        if agent is not None and tenant.platform != "discord":
            raise typer.BadParameter("cold DMs currently require Discord")
        if agent is not None or clear_agent:
            await set_dm_agent_name(session, tenant_id=tenant_id, agent_name=agent)
        if enabled is not None:
            await set_dm_enabled(session, tenant_id=tenant_id, enabled=enabled)
        return {
            "enabled": await dm_enabled(session, tenant_id=tenant_id),
            "dm_agent_name": await get_dm_agent_name(session, tenant_id=tenant_id),
        }


@tenants_app.command("long-job-policy")
def long_job_policy_command(
    tenant: Annotated[uuid.UUID, typer.Argument(help="Tenant UUID.")],
    actor_user: Annotated[
        str, typer.Option("--actor-user", help="Current platform admin user ID.")
    ],
    agent: Annotated[
        str | None, typer.Option("--agent", help="MA agent id; omit for tenant default.")
    ] = None,
    enabled: Annotated[bool | None, typer.Option("--enabled/--disabled")] = None,
    default_deadline: Annotated[int, typer.Option("--default-deadline")] = 60,
    max_deadline: Annotated[int, typer.Option("--max-deadline")] = 70,
    max_usd: Annotated[str, typer.Option("--max-usd")] = "5",
) -> None:
    """Read effective policy or set a complete policy with --enabled/--disabled.

    Uses a fresh platform admin attestation, like backend-policy. Every change
    records the human account and before/after values. Omit --agent to set the
    tenant default; an agent row replaces that default. Operator ceilings apply.
    """
    from daimon.core.long_job_models import LongJobPolicy
    from daimon.core.stores import long_jobs

    settings = load_settings()
    console = Console()

    async def run() -> None:
        async with build_runtime(settings) as rt:
            actor = await verify_backend_actor(
                rt.sessionmaker, tenant_id=tenant, platform_user_id=actor_user, settings=settings
            )
            async with rt.sessionmaker.begin() as db:
                if enabled is not None:
                    await long_jobs.set_policy(
                        db,
                        tenant_id=tenant,
                        agent_id=agent,
                        policy=LongJobPolicy(
                            enabled=enabled,
                            default_deadline_minutes=default_deadline,
                            max_deadline_minutes=max_deadline,
                            max_usd=Decimal(max_usd),
                        ),
                        ceilings=settings.long_jobs,
                        actor=actor.subject,
                        set_by=str(actor.account_id),
                    )
                effective = await long_jobs.get_policy(
                    db, tenant_id=tenant, agent_id=agent, ceilings=settings.long_jobs
                )
            console.print(effective.model_dump_json(indent=2))

    run_cli(run(), console=console)
