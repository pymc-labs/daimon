"""Routines admin CLI commands.

Currently exposes the one-shot `backfill-agent-names` migration bridge for
Between migration 0011 (nullable `agent_name`) and the
forthcoming NOT NULL flip, this command walks every routine whose
`agent_name IS NULL`, retrieves the agent from MA, and writes the daimon-tag
into the row.

Owner-DM creation is available to operators; interactive edits use chat tools
and the adapters' `/routines` command. `sandbox-tools` is the operator's
switch for a routine's built-in sandbox tools, one routine or a confirmed list
of them (the cutover), with a dry run.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal, cast

import httpx
import structlog
import typer
from anthropic import APIStatusError
from daimon.adapters.cli.runtime import CliRuntime, build_runtime
from daimon.core.authz import Action, Place, Subject, Surface, authorize, build_agent_ref
from daimon.core.config import DirectMessagePolicy, load_settings
from daimon.core.cron import validated_next_slot
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME
from daimon.core.discord_membership import DiscordRestMembershipResolver, verify_discord_source
from daimon.core.errors import StoreError, UserFacingError
from daimon.core.membership import MembershipResolver
from daimon.core.membership_subject import resolve_subject
from daimon.core.mux_backend import resource_scope
from daimon.core.mux_compat import retrieve_agent
from daimon.core.routine_move import (
    RoutineFreshnessSettings,
    RoutineSeedEntry,
    routine_rollback_document,
    seed_scheduler_action,
)
from daimon.core.routine_sandbox import (
    SandboxPin,
    SandboxPinUnavailable,
    current_pin,
    pin_of,
)
from daimon.core.routines import routine_for_reader
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.direct_messages import dm_enabled
from daimon.core.stores.identity import get_or_create_cli_principal
from daimon.core.stores.routine_move import pause_for_handback, rollback_rows, seed_routines
from daimon.core.stores.routines import (
    DM_SANDBOX_REFUSAL,
    PIN_UNAVAILABLE_REFUSAL,
    create_routine,
    get_routine,
    link_source_routine,
    list_routines_missing_agent_name,
    set_sandbox_tools,
    update_routine,
    validate_dm_source,
)
from daimon.core.stores.tenants import get_tenant
from pydantic import JsonValue, ValidationError
from rich.console import Console
from sqlalchemy import text

_log = structlog.get_logger(__name__)

routines_app = typer.Typer(help="Routines admin tasks.")

_FALLBACK_NAME = "daimon"


@routines_app.command("backfill-agent-names")
def backfill_agent_names_command(
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """Populate ``routines.agent_name`` from MA agent metadata.

    Idempotent — skips rows where ``agent_name`` is already non-NULL.
    Falls back to ``"daimon"`` when MA returns 404 OR the agent is archived
    OR the agent's metadata lacks ``daimon_name`` (only one
    daimon-tagged agent per tenant today).

    Non-404 ``APIStatusError`` (5xx, network) propagates; the operator re-runs
    after MA recovers.
    """
    settings = load_settings()
    console = Console(highlight=False)

    async def _with_runtime() -> None:
        async with build_runtime(settings) as rt:
            await run_backfill_agent_names(rt=rt, console=console, dry_run=dry_run)

    asyncio.run(_with_runtime())


async def run_backfill_agent_names(
    *,
    rt: CliRuntime,
    console: Console,
    dry_run: bool,
) -> None:
    """Behavior body, callable by tests with an injected CliRuntime.

    Mirrors the `defaults_apply(rt=...)` split in `commands/defaults.py` so
    tests don't need to round-trip through `asyncio.run` + `load_settings`.
    """
    async with rt.sessionmaker() as session:
        rows = await list_routines_missing_agent_name(session)

    from_metadata = 0
    fallback = 0

    for row in rows:
        row = routine_for_reader(row, user_id=None)
        resolved: str
        try:
            agent = await retrieve_agent(
                rt.anthropic, row.agent_id, scope=resource_scope(tenant_id=str(row.tenant_id))
            )
        except APIStatusError as err:
            if err.status_code == 404:
                resolved = _FALLBACK_NAME
                fallback += 1
            else:
                raise
        else:
            tag = agent.metadata.get(MA_METADATA_KEY_NAME)
            if agent.archived_at is not None or tag is None:
                resolved = _FALLBACK_NAME
                fallback += 1
            else:
                resolved = tag
                from_metadata += 1

        if dry_run:
            _log.info(
                "backfill.dry_run",
                routine_id=str(row.id),
                agent_id=row.agent_id,
                resolved_agent_name=resolved,
            )
            continue

        # Per-row tx — canonical async sessionmaker pattern.
        async with rt.sessionmaker() as s, s.begin():
            await update_routine(s, row.id, tenant_id=row.tenant_id, agent_name=resolved)

    console.print(
        f"Backfilled {len(rows)} routine(s) "
        f"({from_metadata} from metadata, {fallback} fallback to {_FALLBACK_NAME!r})."
        + (" [dry-run, no writes]" if dry_run else "")
    )


@routines_app.command("create")
def create_command(
    tenant: Annotated[uuid.UUID, typer.Option("--tenant")],
    owner: Annotated[str, typer.Option("--owner")],
    agent: Annotated[str, typer.Option("--agent")],
    schedule: Annotated[str, typer.Option("--schedule")],
    timezone: Annotated[str, typer.Option("--timezone")],
    trigger: Annotated[str, typer.Option("--trigger")],
    destination: Annotated[Literal["dm"], typer.Option("--destination")],
    source_channel: Annotated[str | None, typer.Option("--source-channel")] = None,
    paused: Annotated[
        bool, typer.Option("--paused", help="Create paused, with no next fire time scheduled.")
    ] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """Create a Discord owner-DM routine; --owner is the sole recipient."""
    del destination  # Typer validates the only supported operator destination.
    settings = load_settings()

    if settings.discord is None:
        raise UserFacingError("Discord bot credentials are required to verify the owner.")
    bot_token = settings.discord.bot_token.get_secret_value()

    async def run() -> None:
        async with build_runtime(settings) as rt:
            await run_create_dm_routine(
                rt=rt,
                console=Console(highlight=False),
                tenant_id=tenant,
                owner=owner,
                agent_name=agent,
                schedule=schedule,
                timezone=timezone,
                trigger=trigger,
                source_channel=source_channel,
                paused=paused,
                dry_run=dry_run,
                resolver=DiscordRestMembershipResolver(bot_token),
            )

    asyncio.run(run())


async def run_create_dm_routine(
    *,
    rt: CliRuntime,
    console: Console,
    tenant_id: uuid.UUID,
    owner: str,
    agent_name: str,
    schedule: str,
    timezone: str,
    trigger: str,
    source_channel: str | None,
    dry_run: bool,
    resolver: MembershipResolver,
    source_transport: httpx.AsyncBaseTransport | None = None,
    paused: bool = False,
) -> None:
    """Verify the selected owner and source before writing, including in dry-run."""
    next_fire = validated_next_slot(schedule, timezone, datetime.now(UTC))
    if not owner.isdecimal():
        raise UserFacingError("The owner must be a Discord user ID.")
    if not trigger.strip():
        raise UserFacingError("A trigger message is required.")
    async with rt.sessionmaker() as session:
        tenant = await get_tenant(session, tenant_id)
        if tenant is None or tenant.archived_at is not None or tenant.platform != "discord":
            raise UserFacingError("Select an active Discord tenant.")
        policy = await load_access_policy(session, tenant_id=tenant_id)
        if not await dm_enabled(
            session, tenant_id=tenant_id
        ) or not rt.settings.direct_message_policies.get(tenant_id, DirectMessagePolicy()).allows(
            owner
        ):
            raise UserFacingError("Routine DMs are disabled or the owner is not allowed.")
        source_thread_id = None
        if source_channel is not None:
            if rt.settings.discord is None:
                raise UserFacingError("Discord bot credentials are required to verify the source.")
            try:
                source_channel, source_thread_id = await verify_discord_source(
                    rt.settings.discord.bot_token.get_secret_value(),
                    guild_id=tenant.external_id,
                    channel_id=source_channel,
                    transport=source_transport,
                )
                await validate_dm_source(session, tenant_id, source_channel, source_thread_id)
            except StoreError as exc:
                raise UserFacingError(str(exc)) from exc
    subject = await resolve_subject(
        rt.sessionmaker, resolver, tenant_id=tenant_id, user_id=owner, require_fresh=True
    )
    if subject.verified_membership is not True or not authorize(
        policy, subject=subject, action=Action.ACT_FOR_CREATOR, surface=Surface.ROUTINE
    ):
        raise UserFacingError("The owner is not a verified, allowed tenant member.")
    agent = await find_agent_by_daimon_tag(rt.anthropic, tenant_id=tenant_id, name=agent_name)
    if agent is None or not authorize(
        policy,
        subject=subject,
        action=Action.SAVE_ROUTINE,
        surface=Surface.ROUTINE,
        agent=build_agent_ref(agent.name, agent.metadata, agent_name),
        place=Place(),
    ):
        raise UserFacingError("The agent is unavailable for private routines.")
    console.print(
        f"Owner / recipient: {owner}; budget source: {source_channel or 'tenant/account'}"
    )
    if dry_run:
        console.print(
            "Dry run: no routine created. "
            + ("Would be created paused." if paused else "Would be created enabled.")
        )
        return
    async with rt.sessionmaker.begin() as session:
        row = await create_routine(
            session,
            tenant_id=tenant_id,
            created_by_user_id=owner,
            agent_id=agent.id,
            agent_name=agent_name,
            cron_expr=schedule,
            timezone_=timezone,
            trigger_message=trigger,
            destination_kind="dm",
            channel_id=source_channel,
            source_thread_id=source_thread_id,
            source_channel_id=source_channel,
            source_provenance_known=True,
            enabled=not paused,
            next_fire_at=None if paused else next_fire,
        )
    console.print(f"Created routine {row.id}.")


@routines_app.command("sandbox-tools")
def sandbox_tools_command(
    routine_ids: Annotated[list[uuid.UUID], typer.Argument(help="Routine ids to change.")],
    tenant: Annotated[uuid.UUID, typer.Option("--tenant")],
    off: Annotated[
        bool, typer.Option("--off", help="Turn sandbox tools off instead of on.")
    ] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """Turn built-in sandbox tools on (or --off) for each listed routine's own runs.

    Operator only. Every id must name a routine in --tenant, or nothing is
    changed. A routine already set that way is left alone, so re-running is
    safe. Each change records the CLI operator and the time.
    """
    settings = load_settings()

    async def run() -> None:
        async with build_runtime(settings) as rt:
            await run_sandbox_tools(
                rt=rt,
                console=Console(highlight=False),
                tenant_id=tenant,
                routine_ids=routine_ids,
                enabled=not off,
                dry_run=dry_run,
            )

    asyncio.run(run())


async def run_sandbox_tools(
    *,
    rt: CliRuntime,
    console: Console,
    tenant_id: uuid.UUID,
    routine_ids: list[uuid.UUID],
    enabled: bool,
    dry_run: bool,
) -> None:
    """Check every id first, then change each routine not already set, in one transaction."""
    ids = list(dict.fromkeys(routine_ids))
    state = "on" if enabled else "off"
    async with rt.sessionmaker.begin() as session:
        tenant = await get_tenant(session, tenant_id)
        if tenant is None or tenant.archived_at is not None:
            raise UserFacingError("Select an active tenant.")
        rows = {rid: await get_routine(session, rid, tenant_id=tenant_id) for rid in ids}
        missing = [str(rid) for rid, row in rows.items() if row is None]
        if missing:
            raise UserFacingError(
                f"No routine in this tenant with id {', '.join(missing)}. Nothing was changed."
            )
        dms = [
            str(rid)
            for rid, row in rows.items()
            if row is not None and row.destination_kind == "dm"
        ]
        if enabled and dms:
            raise UserFacingError(f"{', '.join(dms)}: {DM_SANDBOX_REFUSAL}")
        # An opt-in covers the agent version and environment each routine would
        # run with now; one that can't be read refuses the whole list.
        pins: dict[uuid.UUID, SandboxPin] = {}
        if enabled:
            unreadable: list[str] = []
            for rid, row in rows.items():
                assert row is not None
                try:
                    pins[rid] = await current_pin(
                        rt.anthropic, rt.sessionmaker, row, default=rt.deployment_default
                    )
                except SandboxPinUnavailable:
                    unreadable.append(str(rid))
            if unreadable:
                raise UserFacingError(f"{', '.join(unreadable)}: {PIN_UNAVAILABLE_REFUSAL}")
        pending = [
            rid
            for rid, row in rows.items()
            if row is not None
            and (row.sandbox_tools_enabled != enabled or (enabled and pin_of(row) != pins[rid]))
        ]
        for rid in ids:
            row = rows[rid]
            label = (
                "already " + state
                if rid not in pending
                else "re-approve current agent and environment"
                if enabled and row is not None and row.sandbox_tools_enabled
                else "turn " + state
            )
            pinned = pins.get(rid)
            if pinned is not None:
                label += (
                    f" (agent {pinned.agent_id} version {pinned.agent_version}, environment "
                    f"{pinned.environment_id} as of {pinned.environment_updated_at.isoformat()})"
                )
            console.print(f"{rid}: {label}")
        if dry_run:
            console.print(f"Dry run: {len(pending)} routine(s) would change; nothing written.")
            return
        principal = await get_or_create_cli_principal(
            session, tenant_id=tenant_id, os_user=rt.settings.cli.local_user
        )
        for rid in pending:
            await set_sandbox_tools(
                session,
                rid,
                tenant_id=tenant_id,
                enabled=enabled,
                set_by=str(principal.id),
                # Whoever holds the database credentials is the operator.
                actor=Subject(is_admin=True),
                via="cli",
                pin=pins.get(rid),
            )
    console.print(
        f"Sandbox tools {state} for {len(pending)} routine(s); {len(ids) - len(pending)} unchanged."
    )


def read_seed_entries(path: Path) -> list[RoutineSeedEntry]:
    """Refuse every malformed entry before opening a database transaction."""
    try:
        payload = cast(JsonValue, json.loads(path.read_text()))
    except (OSError, ValueError) as exc:
        raise UserFacingError(f"Cannot read seed JSON: {exc}") from exc
    if not isinstance(payload, list) or not payload:
        raise UserFacingError("Seed JSON must be a nonempty array of routine entries.")
    entries: list[RoutineSeedEntry] = []
    errors: list[str] = []
    for index, item in enumerate(payload):
        label = (
            item.get("source_routine_id", f"entry {index + 1}")
            if isinstance(item, dict)
            else f"entry {index + 1}"
        )
        try:
            entries.append(RoutineSeedEntry.model_validate(item))
        except ValidationError as exc:
            errors.append(
                f"{label}: "
                + "; ".join(
                    f"{'.'.join(map(str, error['loc']))}: {error['msg']}" for error in exc.errors()
                )
            )
    if errors:
        raise UserFacingError("\n".join(errors) + "\nNothing was changed.")
    return entries


@routines_app.command("link-source")
def link_source_command(
    tenant: Annotated[uuid.UUID, typer.Option("--tenant")],
    routine: Annotated[uuid.UUID, typer.Option("--routine", help="Daimon routine UUID.")],
    source_id: Annotated[
        uuid.UUID, typer.Option("--source-id", help="Routine UUID in the source system.")
    ],
    apply: Annotated[bool, typer.Option("--apply", help="Write the validated link.")] = False,
) -> None:
    """Operator only: link a paused, never-fired routine to its source system.

    Use before seed-first-run.
    """
    settings = load_settings()

    async def run() -> None:
        async with build_runtime(settings) as rt:
            await run_link_source(
                rt=rt,
                console=Console(highlight=False),
                tenant_id=tenant,
                routine_id=routine,
                source_id=source_id,
                apply=apply,
            )

    try:
        asyncio.run(run())
    except UserFacingError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None


async def run_link_source(
    *,
    rt: CliRuntime,
    console: Console,
    tenant_id: uuid.UUID,
    routine_id: uuid.UUID,
    source_id: uuid.UUID,
    apply: bool,
) -> None:
    try:
        async with rt.sessionmaker.begin() as session:
            await link_source_routine(
                session,
                tenant_id=tenant_id,
                routine_id=routine_id,
                source_routine_id=source_id,
                apply=apply,
            )
    except StoreError as exc:
        raise UserFacingError(str(exc)) from exc
    console.print(f"{routine_id}: source_routine_id={source_id} tenant={tenant_id}", markup=False)
    console.print("Applied atomically." if apply else "Dry run: nothing written.")


@routines_app.command("seed-first-run")
def seed_first_run_command(
    file: Annotated[
        Path, typer.Option("--file", help="JSON array of source system routine entries.")
    ],
    tenant: Annotated[uuid.UUID, typer.Option("--tenant")],
    apply: Annotated[
        bool, typer.Option("--apply", help="Apply the entire validated batch.")
    ] = False,
) -> None:
    """Operator only: preview (default) or atomically seed paused, never-fired routines.

    Database credentials confer operator authority, as for sandbox-tools.
    Reads DAIMON_SCHEDULER__MAX_AGE_S for the scheduler preview.
    """
    entries = read_seed_entries(file)
    settings = load_settings()
    max_age = timedelta(seconds=RoutineFreshnessSettings().max_age_s)
    console = Console(highlight=False)

    async def run() -> None:
        async with build_runtime(settings) as rt:
            await run_seed_first_run(
                rt=rt,
                console=console,
                tenant_id=tenant,
                entries=entries,
                apply=apply,
                max_age=max_age,
            )

    asyncio.run(run())


async def run_seed_first_run(
    *,
    rt: CliRuntime,
    console: Console,
    tenant_id: uuid.UUID,
    entries: list[RoutineSeedEntry],
    apply: bool,
    max_age: timedelta,
    now: datetime | None = None,
) -> None:
    now = now or datetime.now(UTC)
    try:
        async with rt.sessionmaker.begin() as session:
            planned = await seed_routines(
                session, entries, tenant_id=tenant_id, now=now, apply=apply
            )
    except StoreError as exc:
        raise UserFacingError(str(exc)) from exc
    seeds = {entry.source_routine_id: entry.last_completed_slot for entry in entries}
    for row, slot in planned:
        assert row.source_routine_id is not None
        action = seed_scheduler_action(row, slot, now=now, max_age=max_age)
        console.print(
            f"{row.source_routine_id}: T={seeds[row.source_routine_id].isoformat()} "
            f"cron={row.cron_expr} "
            f"tz={row.timezone} first_slot={slot.isoformat()} policy={row.catch_up_policy} "
            f"freshness_seconds={max_age.total_seconds():g}: {action}",
            markup=False,
        )
    console.print("Applied atomically." if apply else "Dry run: nothing written.")


@routines_app.command("export-rollback")
def export_rollback_command(
    tenant: Annotated[uuid.UUID, typer.Option("--tenant")],
    source_ids: Annotated[
        list[uuid.UUID] | None, typer.Option("--id", help="Source routine id; may be repeated.")
    ] = None,
    ids_file: Annotated[
        Path | None, typer.Option("--ids-file", help="JSON array of source routine ids.")
    ] = None,
    output: Annotated[Path | None, typer.Option("--output")] = None,
) -> None:
    """Operator only: export a read-only rollback snapshot for one tenant.

    With no filters, includes every routine in the team. Pause routines and
    drain executions and deliveries before relying on final=true.
    """
    if ids_file is not None:
        if source_ids:
            raise UserFacingError("Use either --id or --ids-file.")
        source_ids = read_source_ids(ids_file)
    settings = load_settings()

    async def run() -> str:
        async with build_runtime(settings) as rt:
            try:
                async with rt.sessionmaker.begin() as session:
                    await session.execute(text("SET TRANSACTION READ ONLY"))
                    rows = await rollback_rows(
                        session, tenant_id=tenant, source_routine_ids=source_ids
                    )
            except StoreError as exc:
                raise UserFacingError(str(exc)) from exc
        return json.dumps(routine_rollback_document(rows, exported_at=datetime.now(UTC)), indent=2)

    rendered = asyncio.run(run()) + "\n"
    if output is not None:
        output.write_text(rendered)
    else:
        typer.echo(rendered, nl=False)


def read_source_ids(path: Path) -> list[uuid.UUID]:
    try:
        payload = cast(JsonValue, json.loads(path.read_text()))
        if (
            not isinstance(payload, list)
            or not payload
            or not all(isinstance(item, str) for item in payload)
        ):
            raise ValueError("expected a nonempty JSON array of routine ids")
        ids = [uuid.UUID(item) for item in payload if isinstance(item, str)]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate routine id")
        return ids
    except (OSError, ValueError) as exc:
        raise UserFacingError(f"Cannot read routine ids: {exc}") from exc


@routines_app.command("pause-for-handback")
def pause_for_handback_command(
    tenant: Annotated[uuid.UUID, typer.Option("--tenant")],
    ids_file: Annotated[Path, typer.Option("--ids-file", help="JSON array of source routine ids.")],
    apply: Annotated[
        bool, typer.Option("--apply", help="Pause and capture the whole selection.")
    ] = False,
) -> None:
    """Operator only: capture the next run and pause routines for return to the source system."""
    ids = read_source_ids(ids_file)
    settings = load_settings()

    async def run() -> None:
        async with build_runtime(settings) as rt:
            await run_pause_for_handback(
                rt=rt,
                console=Console(highlight=False),
                tenant_id=tenant,
                source_ids=ids,
                apply=apply,
            )

    asyncio.run(run())


async def run_pause_for_handback(
    *,
    rt: CliRuntime,
    console: Console,
    tenant_id: uuid.UUID,
    source_ids: list[uuid.UUID],
    apply: bool,
    now: datetime | None = None,
) -> None:
    try:
        async with rt.sessionmaker.begin() as session:
            rows = await pause_for_handback(
                session,
                tenant_id=tenant_id,
                source_routine_ids=source_ids,
                now=now or datetime.now(UTC),
                apply=apply,
            )
    except StoreError as exc:
        raise UserFacingError(str(exc)) from exc
    for row in rows:
        recorded = row.handback_paused_at is not None
        enabled = row.handback_enabled if recorded else row.enabled
        next_run = row.handback_next_fire_at if recorded else row.next_fire_at
        console.print(
            f"{row.source_routine_id}: enabled={str(enabled).lower()} "
            f"expected_next_run={next_run.isoformat() if next_run else 'null'} "
            f"({'already captured; preserved' if recorded else 'pause and capture'})",
            markup=False,
        )
    console.print("Applied atomically." if apply else "Dry run: nothing written.")
