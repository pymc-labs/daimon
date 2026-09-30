"""daimon tenants ... sub-app."""

from __future__ import annotations

import re
import uuid
from decimal import Decimal, InvalidOperation
from typing import Annotated

import typer
from daimon.adapters.cli.errors import run_cli
from daimon.adapters.cli.flags import JSON_OPTION, YES_OPTION
from daimon.adapters.cli.output import emit_rows
from daimon.adapters.cli.prompt import confirm_or_abort
from daimon.adapters.cli.runtime import CliRuntime, build_runtime
from daimon.core.access_policy import OPEN_ACCESS_POLICY, TenantAccessPolicy
from daimon.core.config import load_settings
from daimon.core.errors import StoreError
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores import tenant_ledger, tenant_user_caps
from daimon.core.stores.access_policy import (
    clear_access_policy,
    load_access_policy,
    lock_access_policy,
    set_access_policy,
)
from daimon.core.stores.domain import Platform
from daimon.core.stores.tenants import (
    delete_tenant,
    get_tenant,
    get_tenant_dependent_counts,
    list_tenants_by_platform,
    set_funding_mode,
    set_turn_cap,
)
from rich.console import Console

tenants_app = typer.Typer(help="Tenants: list, credit, caps, funding and access policy, delete.")
access_policy_app = typer.Typer(
    help="A tenant's access policy: who may invoke the agent, protected and sealed channels."
)
tenants_app.add_typer(access_policy_app, name="access-policy")


_VALID_PLATFORMS = ("discord", "cli", "slack")


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
            f"agent_repo_binding={counts.agent_repo_binding}, "
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
            f"agent_repo_binding={counts.agent_repo_binding}, "
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
        console.print_json(policy.model_dump_json())
        return
    if policy == OPEN_ACCESS_POLICY:
        console.print(
            f"{label} access policy: open (anyone may invoke; nothing protected or sealed)"
        )
        return
    console.print(f"{label} access policy:")
    for field, ids in (
        ("invoker_user_ids", policy.invoker_user_ids),
        ("protected_channel_ids", policy.protected_channel_ids),
        ("protected_category_ids", policy.protected_category_ids),
        ("sealed_channel_ids", policy.sealed_channel_ids),
    ):
        console.print(f"  {field}: {', '.join(ids) or '-'}")
    console.print(f"  dm_memory_read_only: {str(policy.dm_memory_read_only).lower()}")
    pins = policy.agent_channel_pins
    console.print(
        "  agent_channel_pins: "
        + ("; ".join(f"{name} -> {', '.join(ids)}" for name, ids in sorted(pins.items())) or "-")
    )


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


@access_policy_app.command("set")
def tenants_access_policy_set_command(
    platform: str,
    external_id: str,
    invoker: Annotated[
        list[str] | None,
        typer.Option(
            help="Platform user id allowed to start turns (repeatable). Admins always may."
        ),
    ] = None,
    protected_channel: Annotated[
        list[str] | None,
        typer.Option(help="Channel id the agent must never write into (repeatable)."),
    ] = None,
    protected_category: Annotated[
        list[str] | None,
        typer.Option(help="Discord category id whose channels are protected (repeatable)."),
    ] = None,
    sealed_channel: Annotated[
        list[str] | None,
        typer.Option(
            help=(
                "Channel id readable only from a turn inside it (repeatable). A single "
                "thread: its Discord id, or Slack channel_id:thread_ts."
            )
        ),
    ] = None,
    dm_memory_read_only: Annotated[
        bool | None,
        typer.Option(
            "--dm-memory-read-only/--no-dm-memory-read-only",
            help="Give turns started from a DM read-only memory.",
        ),
    ] = None,
    pin_agent: Annotated[
        list[str] | None,
        typer.Option(
            help=(
                "AGENT=CHANNEL_ID: the named agent runs only in these channels and the "
                "threads under them (repeatable; repeat an agent for more channels)."
            )
        ),
    ] = None,
    clear: Annotated[
        bool, typer.Option("--clear", help="Remove the policy: back to open.")
    ] = False,
    as_json: Annotated[bool, JSON_OPTION] = False,
) -> None:
    """Set a tenant's access policy.

    Each flag given replaces that whole field; fields not given keep their
    stored value. To empty one field, --clear and set the rest again.
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
                protected_channel=protected_channel,
                protected_category=protected_category,
                sealed_channel=sealed_channel,
                dm_memory_read_only=dm_memory_read_only,
                pin_agent=pin_agent,
                clear=clear,
                as_json=as_json,
            )

    run_cli(_with_runtime(), console=console)


def _parse_agent_pins(pins: list[str], *, platform: str) -> dict[str, tuple[str, ...]]:
    """Turn repeated AGENT=CHANNEL_ID flags into the policy's pin mapping."""
    if not pins:
        raise typer.BadParameter("agent_channel_pins: pass at least one AGENT=CHANNEL_ID")
    parsed: dict[str, list[str]] = {}
    pattern = r"[0-9]{15,21}" if platform == "discord" else r"[CGD][A-Z0-9]+"
    for value in pins:
        name, sep, channel = (part.strip() for part in value.partition("="))
        if not sep or not name or not channel:
            raise typer.BadParameter(
                f"agent_channel_pins: expected AGENT=CHANNEL_ID, got {value!r}"
            )
        if platform != "cli" and re.fullmatch(pattern, channel) is None:
            raise typer.BadParameter(f"agent_channel_pins: invalid {platform} id {channel!r}")
        parsed.setdefault(name, [])
        if channel not in parsed[name]:
            parsed[name].append(channel)
    return {name: tuple(ids) for name, ids in parsed.items()}


async def tenants_access_policy_set(
    *,
    rt: CliRuntime,
    console: Console,
    platform: str,
    external_id: str,
    invoker: list[str] | None = None,
    protected_channel: list[str] | None = None,
    protected_category: list[str] | None = None,
    sealed_channel: list[str] | None = None,
    dm_memory_read_only: bool | None = None,
    pin_agent: list[str] | None = None,
    clear: bool = False,
    as_json: bool = False,
) -> None:
    validated_platform = _validate_platform(platform)
    changes: dict[str, object] = {}
    for field, ids in (
        ("invoker_user_ids", invoker),
        ("protected_channel_ids", protected_channel),
        ("protected_category_ids", protected_category),
        ("sealed_channel_ids", sealed_channel),
    ):
        if ids is not None:
            if not ids:
                raise typer.BadParameter(f"{field}: pass at least one non-empty id")
            for value in ids:
                cleaned = value.strip()
                pattern = (
                    r"[0-9]{15,21}"
                    if validated_platform == "discord"
                    else r"[UW][A-Z0-9]+"
                    if field == "invoker_user_ids"
                    # A Slack thread is sealed on its own as channel_id:thread_ts.
                    else r"[CGD][A-Z0-9]+(?::[0-9]+\.[0-9]+)?"
                    if field == "sealed_channel_ids"
                    else r"[CGD][A-Z0-9]+"
                )
                if not cleaned or (
                    validated_platform != "cli" and re.fullmatch(pattern, cleaned) is None
                ):
                    raise typer.BadParameter(f"{field}: invalid {validated_platform} id {value!r}")
            changes[field] = tuple(dict.fromkeys(value.strip() for value in ids))
    if dm_memory_read_only is not None:
        changes["dm_memory_read_only"] = dm_memory_read_only
    if pin_agent is not None:
        changes["agent_channel_pins"] = _parse_agent_pins(pin_agent, platform=validated_platform)
    if clear and changes:
        raise typer.BadParameter("--clear can't be combined with other policy flags")
    if not clear and not changes:
        raise typer.BadParameter("nothing to set: pass a policy flag or --clear")

    label = f"{platform}:{external_id}"
    tenant_id = await _existing_tenant_id(rt, platform=platform, external_id=external_id)
    async with rt.sessionmaker() as session, session.begin():
        await lock_access_policy(session, tenant_id=tenant_id)
        if clear:
            await clear_access_policy(session, tenant_id=tenant_id)
            policy = OPEN_ACCESS_POLICY
        else:
            # An unreadable stored row raises here rather than being overwritten
            # blind; --clear is the way out of that state.
            current = await load_access_policy(session, tenant_id=tenant_id)
            policy = TenantAccessPolicy.model_validate(current.model_dump() | changes)
            await set_access_policy(session, tenant_id=tenant_id, policy=policy)
    _print_policy(console, label=label, policy=policy, as_json=as_json)
