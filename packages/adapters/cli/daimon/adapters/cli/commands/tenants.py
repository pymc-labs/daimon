"""daimon tenants ... sub-app."""

from __future__ import annotations

import re
import uuid
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal

import typer
from daimon.adapters.cli.errors import run_cli
from daimon.adapters.cli.flags import JSON_OPTION, YES_OPTION
from daimon.adapters.cli.output import emit_rows, render_json
from daimon.adapters.cli.prompt import confirm_or_abort
from daimon.adapters.cli.runtime import CliRuntime, build_runtime
from daimon.core.access_policy import OPEN_ACCESS_POLICY, TenantAccessPolicy
from daimon.core.config import load_settings
from daimon.core.errors import StoreError
from daimon.core.ma_identity import derive_tenant_uuid
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
from daimon.core.stores.domain import Platform
from daimon.core.stores.tenants import (
    delete_tenant,
    get_tenant,
    get_tenant_dependent_counts,
    list_tenants_by_platform,
    set_funding_mode,
    set_turn_cap,
)
from pydantic import BaseModel, ValidationError
from rich.console import Console
from rich.markup import escape
from rich.table import Table

tenants_app = typer.Typer(help="Tenants: list, credit, caps, funding and access policy, delete.")
access_policy_app = typer.Typer(
    help=("A tenant's access policy: who may invoke the agent, and its channel and agent rules.")
)
tenants_app.add_typer(access_policy_app, name="access-policy")


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
        console.print(f"{label} access policy: open (anyone may invoke; no rules)")
        return
    console.print(f"{label} access policy:")
    console.print(f"  invoker_user_ids: {', '.join(policy.invoker_user_ids) or '-'}")
    console.print(f"  member_guest_ids: {', '.join(policy.member_guest_ids) or '-'}")
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
            help="Platform user id allowed to start turns (repeatable). Admins always may."
        ),
    ] = None,
    dm_memory_read_only: Annotated[
        bool | None,
        typer.Option(
            "--dm-memory-read-only/--no-dm-memory-read-only",
            help="Give turns started from a DM read-only memory.",
        ),
    ] = None,
    add_member_guest: Annotated[
        list[str] | None,
        typer.Option(
            help=(
                "Teams guest's Entra object id to treat as a member of the organisation "
                "(repeatable). Other guests are answered as from another organisation. "
                "Only used while DAIMON_TEAMS__RESTRICT_GUESTS is on."
            )
        ),
    ] = None,
    remove_member_guest: Annotated[
        list[str] | None,
        typer.Option(help="Teams guest's Entra object id to drop from the members (repeatable)."),
    ] = None,
    drop_agent_rules: Annotated[
        bool,
        typer.Option(
            "--drop-agent-rules",
            help="Let --clear drop agent rules too, leaving those agents running anywhere.",
        ),
    ] = False,
    clear: Annotated[
        bool, typer.Option("--clear", help="Remove the policy and every rule: back to open.")
    ] = False,
    as_json: Annotated[bool, JSON_OPTION] = False,
) -> None:
    """Set who may start turns, DM memory and Teams member guests.

    --invoker replaces the stored list; member guests are edited in place.
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
                dm_memory_read_only=dm_memory_read_only,
                add_member_guest=add_member_guest,
                remove_member_guest=remove_member_guest,
                drop_agent_rules=drop_agent_rules,
                clear=clear,
                as_json=as_json,
            )

    run_cli(_with_runtime(), console=console)


def _parse_member_guests(values: list[str] | None, *, platform: str) -> tuple[str, ...]:
    """Repeated guest flags as lower-case Entra object ids; Teams only."""
    if values is None:
        return ()
    if platform != "teams":
        raise typer.BadParameter("member_guest_ids: only Teams has guests")
    ids = tuple(dict.fromkeys(value.strip().lower() for value in values))
    for value in ids:
        if re.fullmatch(_ENTRA_OBJECT_ID, value) is None:
            raise typer.BadParameter(f"member_guest_ids: invalid Entra object id {value!r}")
    if not ids:
        raise typer.BadParameter("member_guest_ids: pass at least one id")
    return ids


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
    dm_memory_read_only: bool | None = None,
    add_member_guest: list[str] | None = None,
    remove_member_guest: list[str] | None = None,
    drop_agent_rules: bool = False,
    clear: bool = False,
    as_json: bool = False,
) -> None:
    validated_platform = _validate_platform(platform)
    guests_to_add = _parse_member_guests(add_member_guest, platform=validated_platform)
    guests_to_remove = _parse_member_guests(remove_member_guest, platform=validated_platform)
    if set(guests_to_add) & set(guests_to_remove):
        raise typer.BadParameter(
            "member_guest_ids: an id named in both --add-member-guest and --remove-member-guest"
        )
    changes: dict[str, object] = {}
    if invoker is not None:
        changes["invoker_user_ids"] = _parse_invokers(invoker, platform=validated_platform)
    if dm_memory_read_only is not None:
        changes["dm_memory_read_only"] = dm_memory_read_only
    edits_guests = bool(guests_to_add or guests_to_remove)
    if drop_agent_rules and not clear:
        raise typer.BadParameter("--drop-agent-rules only applies to --clear")
    if clear and (changes or edits_guests):
        raise typer.BadParameter("--clear can't be combined with other policy flags")
    if not clear and not changes and not edits_guests:
        raise typer.BadParameter("nothing to set: pass a policy flag or --clear")

    label = f"{platform}:{external_id}"
    tenant_id = await _existing_tenant_id(rt, platform=platform, external_id=external_id)
    dropped: list[str] = []
    async with policy_write_transaction(rt.sessionmaker, tenant_id=tenant_id) as session:
        await lock_policy_writes_exclusive(session, tenant_id=tenant_id)
        await lock_access_policy(session, tenant_id=tenant_id)
        if clear:
            try:
                dropped = sorted(
                    (await load_access_policy(session, tenant_id=tenant_id)).agent_rules
                )
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
            await clear_access_policy(session, tenant_id=tenant_id)
            policy = OPEN_ACCESS_POLICY
        else:
            # An unreadable stored row raises here rather than being overwritten
            # blind; --clear is the way out of that state.
            current = await load_access_policy(session, tenant_id=tenant_id)
            if edits_guests:
                missing = sorted(set(guests_to_remove) - set(current.member_guest_ids))
                if missing:
                    raise typer.BadParameter(
                        f"member_guest_ids: {', '.join(missing)} is not listed. Nothing was "
                        "changed."
                    )
                kept = (g for g in current.member_guest_ids if g not in guests_to_remove)
                changes["member_guest_ids"] = tuple(dict.fromkeys((*kept, *guests_to_add)))
            try:
                policy = TenantAccessPolicy.model_validate(current.model_dump() | changes)
            except ValidationError as exc:
                message = "; ".join(str(error["msg"]) for error in exc.errors())
                raise typer.BadParameter(f"{message}. Nothing was changed.") from None
            await set_access_policy(session, tenant_id=tenant_id, policy=policy)
    _print_policy(console, label=label, policy=policy, as_json=as_json)
    if not as_json:
        for name in dropped:
            console.print(f"  {name} has no rule now (runs anywhere)")
