"""Operator-only offline import of watches and reminders, and making them live."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from daimon.adapters.cli.flags import JSON_OPTION
from daimon.adapters.cli.runtime import CliRuntime, build_runtime
from daimon.core.config import load_settings, load_watches_settings
from daimon.core.continuity.watch_imports import ImportedCallbackWatch, WatchExport
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag
from daimon.core.stores.held_timers import (
    TimerImport,
    agent_runs_scope_timers,
    cancel_missing_timers,
    import_held_timer,
    list_unrunnable_timers,
    settle_held_timers,
    unpause_timers,
)
from daimon.core.stores.platform_names import get_user_names
from daimon.core.stores.tenants import get_tenant
from daimon.core.stores.watches import (
    CallbackImport,
    NoticeReceipt,
    OwnerCapExceeded,
    WatchActivity,
    WatchRow,
    callback_lifetime,
    confirm_import,
    import_callback_watch,
    import_watch,
    list_notice_receipts,
    list_tenant_watches,
    list_unconfirmed_imports,
    renew_callback_watch,
    rotate_callback_link,
    set_watch_limits,
    unpause_imported_watch,
    watch_ceilings,
)

watches_app = typer.Typer(
    help="List a tenant's watches and their owner-notice receipts, import paused watches "
    "and reminders, make them live and set daily firing limits."
)
limits_app = typer.Typer(help="Daily firing limits for every watch, or one tenant's.")
watches_app.add_typer(limits_app, name="limits")


async def check_agent(
    runtime: CliRuntime, *, tenant_id: uuid.UUID, agent_id: str, agent_name: str
) -> None:
    """The tenant must be a live Discord one and the agent one of its unarchived agents."""
    async with runtime.sessionmaker() as db:
        tenant = await get_tenant(db, tenant_id)
    if tenant is None:
        raise typer.BadParameter(f"No such tenant {tenant_id}")
    if tenant.platform != "discord" or tenant.archived_at is not None:
        raise typer.BadParameter(f"Tenant {tenant_id} is archived or not a Discord server")
    agent = await find_agent_by_daimon_tag(
        runtime.anthropic, tenant_id=tenant_id, name=agent_name, records=runtime.sessionmaker
    )
    if agent is None or agent.id != agent_id:
        raise typer.BadParameter("--agent-id and --agent-name must name one agent of the tenant")


def _listed(item: WatchActivity, names: dict[tuple[str, str], str | None]) -> dict[str, object]:
    """The operator view of one watch. Never its instructions, keys, baseline or evidence."""
    row = item.watch
    reason, tool = row.source_state.get("reason"), row.source_state.get("tool")
    return {
        "watch_id": str(row.id),
        "owner_id": row.owner_external_id,
        "owner": names.get((row.platform, row.owner_external_id)),
        "status": row.status,
        "reason": str(reason) if reason else None,
        "source": row.source,
        "source_tool": str(tool) if tool else None,
        "destination": f"{row.platform} thread",
        "parent_channel_id": row.parent_channel_id,
        "thread_id": row.thread_id,
        "agent_id": row.agent_id,
        "expires_at": row.expires_at.isoformat() if row.expires_at else "permanent",
        "fires_today": item.fires_today,
        "last_fired_at": item.last_fired_at.isoformat() if item.last_fired_at else None,
        "errors": row.consecutive_errors,
        "retry_after": row.backoff_until.isoformat() if row.backoff_until else None,
    }


@watches_app.command("list")
def list_command(
    tenant: Annotated[uuid.UUID, typer.Option("--tenant")],
    as_json: Annotated[bool, JSON_OPTION] = False,
) -> None:
    """List every watch in --tenant, read-only: owner, status, source, expiry and firings."""

    async def run() -> list[dict[str, object]]:
        async with build_runtime(load_settings()) as runtime, runtime.sessionmaker() as db:
            if await get_tenant(db, tenant) is None:
                raise typer.BadParameter("No such tenant", param_hint="--tenant")
            listed = await list_tenant_watches(db, tenant_id=tenant, now=datetime.now(UTC))
            names: dict[tuple[str, str], str | None] = {}
            for platform in {item.watch.platform for item in listed}:
                found = await get_user_names(
                    db,
                    tenant_id=tenant,
                    platform=platform,
                    user_ids={
                        item.watch.owner_external_id
                        for item in listed
                        if item.watch.platform == platform
                    },
                )
                names.update({(platform, user): name.label for user, name in found.items()})
        return [_listed(item, names) for item in listed]

    rows = asyncio.run(run())
    if as_json:
        typer.echo(json.dumps(rows, indent=2))
        return
    if not rows:
        typer.echo("No watches.")
        return
    for row in rows:
        owner = f"{row['owner']} ({row['owner_id']})" if row["owner"] else row["owner_id"]
        status = f"{row['status']}: {row['reason']}" if row["reason"] else row["status"]
        source = f"{row['source']}/{row['source_tool']}" if row["source_tool"] else row["source"]
        last = row["last_fired_at"] or "never"
        typer.echo(
            f"{row['watch_id']}  owner={owner}  status={status}  source={source}  "
            f"to={row['destination']} {row['thread_id']}  expires={row['expires_at']}  "
            f"fires_today={row['fires_today']}  last_fired={last}  errors={row['errors']}"
        )


def _receipt(row: NoticeReceipt) -> dict[str, object]:
    """One notice attempt: ids, times and outcome. Receipts never hold the notice text."""
    return {
        "kind": row.kind,
        "generation": row.generation,
        "attempt": row.attempt,
        "outcome": row.outcome,
        "attempted_at": row.attempted_at.isoformat(),
        "settled_at": row.settled_at.isoformat() if row.settled_at else None,
        "owner_account_id": str(row.owner_account_id),
        "platform": row.platform,
        "channel_id": row.channel_id,
        "message_id": row.message_id,
    }


def _messages_sent(rows: list[NoticeReceipt]) -> dict[str, int]:
    """Distinct messages sent per notice kind; a sent receipt without a message id counts once."""
    seen: dict[str, set[str]] = {}
    for index, row in enumerate(rows):
        if row.outcome == "sent":
            seen.setdefault(row.kind, set()).add(row.message_id or f"unknown-{index}")
    return {kind: len(ids) for kind, ids in sorted(seen.items())}


@watches_app.command("notices")
def notices_command(
    watch: Annotated[uuid.UUID, typer.Option("--watch")],
    as_json: Annotated[bool, JSON_OPTION] = False,
) -> None:
    """Show --watch's owner-notice receipts, read-only, kept 30 days and after deletion.

    `sent` counts distinct messages per notice kind: an expiry notice delivered
    exactly once shows `"expired": 1`.
    """

    async def run() -> list[NoticeReceipt]:
        async with build_runtime(load_settings()) as runtime, runtime.sessionmaker() as db:
            return await list_notice_receipts(db, watch_id=watch)

    rows = asyncio.run(run())
    sent = _messages_sent(rows)
    if as_json:
        report = {"watch_id": str(watch), "sent": sent, "receipts": [_receipt(r) for r in rows]}
        typer.echo(json.dumps(report, indent=2))
        return
    if not rows:
        typer.echo("No notice receipts.")
        return
    for row in map(_receipt, rows):
        typer.echo(
            f"{row['attempted_at']}  {row['kind']}  generation={row['generation']}  "
            f"attempt={row['attempt']}  outcome={row['outcome']}  "
            f"message={row['message_id'] or 'none'}"
        )
    typer.echo("sent: " + (", ".join(f"{kind}={n}" for kind, n in sent.items()) or "none"))


def _write_urls(path: Path, urls: dict[str, str]) -> None:
    """Merge into any earlier map and write it owner-only. The links are secrets."""
    merged: dict[str, str] = {}
    if path.exists():
        merged = json.loads(path.read_text(encoding="utf-8"))
    merged.update(urls)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(merged, indent=2))
    path.chmod(0o600)


@watches_app.command("import")
def import_command(
    export_files: Annotated[list[Path], typer.Argument(metavar="EXPORT_FILE...")],
    tenant: Annotated[
        uuid.UUID | None, typer.Option("--tenant", help="Tenant for callback watches and timers")
    ] = None,
    agent_id: Annotated[
        str | None,
        typer.Option("--agent-id", help="Agent id that callback watches and timers are for"),
    ] = None,
    agent_name: Annotated[
        str | None, typer.Option("--agent-name", help="Name of that agent")
    ] = None,
    urls_out: Annotated[
        Path | None,
        typer.Option("--urls-out", help="Write new callback links here (JSON, mode 0600)"),
    ] = None,
    full: Annotated[
        bool,
        typer.Option("--full", help="Cancel held timers that no file of this run lists"),
    ] = False,
) -> None:
    """Import v1 JSON manifests of one export, offline. Every watch starts paused; no key
    is copied.

    Give every file of one export in the same run. Their server must be the tenant's.
    Each watch commits on its own, so ingress never waits long; a rerun finishes a
    run that stopped. Callback watches keep their token hash and get a new link, written
    only to --urls-out as {source id: url}. Timers are held for --agent-id until its
    `go-live`. A rerun updates what nobody has confirmed yet; with --full it also
    cancels that agent's held timers that no file lists, and refuses a run whose files
    list no timers.
    """
    manifests: list[WatchExport] = []
    for export_file in export_files:
        if export_file.stat().st_size > 1048576:
            raise typer.BadParameter(f"Watch export {export_file.name} exceeds 1 MiB")
        manifests.append(WatchExport.model_validate_json(export_file.read_bytes()))
    guilds = {manifest.guild_id for manifest in manifests}
    if len(guilds) > 1:
        raise typer.BadParameter("The files are for different servers")
    guild_id = guilds.pop()
    entries = [entry for manifest in manifests for entry in manifest.watches]
    timers = [timer for manifest in manifests for timer in manifest.timers]
    callbacks = [w for w in entries if isinstance(w, ImportedCallbackWatch)]
    others = [w for w in entries if not isinstance(w, ImportedCallbackWatch)]
    if full and not timers:
        # Later files of a split export list no timers: --full on them alone would
        # cancel every held reminder.
        raise typer.BadParameter(
            "No file lists timers; give every file of the export with --full",
            param_hint="--full",
        )
    if (callbacks or timers) and tenant is None:
        raise typer.BadParameter("--tenant is required for callback watches and timers")
    if (callbacks or timers) and (agent_id is None or agent_name is None):
        raise typer.BadParameter(
            "--agent-id and --agent-name are required for callbacks and timers"
        )
    if callbacks:
        if urls_out is None:
            raise typer.BadParameter("--urls-out is required for callback watches")
        if load_watches_settings().callback_base_url is None:
            raise typer.BadParameter("Set DAIMON_WATCHES__CALLBACK_BASE_URL first")

    async def check_targets(runtime: CliRuntime) -> None:
        """The file, tenant and agent must agree before anything is written."""
        tenants: set[uuid.UUID] = {w.os_binding.tenant_id for w in others}
        if tenant is not None:
            tenants.add(tenant)
        async with runtime.sessionmaker() as db:
            for tenant_id in tenants:
                row = await get_tenant(db, tenant_id)
                if row is None:
                    raise typer.BadParameter(f"No such tenant {tenant_id}")
                if row.external_id != guild_id:
                    raise typer.BadParameter(
                        f"The file is for server {guild_id}, not tenant {tenant_id}"
                    )
        if callbacks or timers:
            assert tenant is not None and agent_id and agent_name
            await check_agent(runtime, tenant_id=tenant, agent_id=agent_id, agent_name=agent_name)

    async def run() -> None:
        base = load_watches_settings().callback_base_url
        async with build_runtime(load_settings()) as runtime:
            await check_targets(runtime)
            now = datetime.now(UTC)
            rows: list[WatchRow] = []
            over_cap = 0
            for entry in others:
                try:
                    # The global watch lock lasts one commit, so ingress waits for one watch.
                    async with runtime.sessionmaker.begin() as db:
                        rows.append(await import_watch(db, entry, now=now))
                except OwnerCapExceeded:
                    over_cap += 1
            moved: list[CallbackImport] = []
            cap = None
            if callbacks and tenant is not None:
                async with runtime.sessionmaker() as db:
                    cap = await callback_lifetime(db, runtime.settings, tenant)
            for entry in callbacks:
                assert tenant is not None and agent_id and agent_name
                async with runtime.sessionmaker.begin() as db:
                    item = await import_callback_watch(
                        db,
                        entry,
                        tenant_id=tenant,
                        agent_id=agent_id,
                        agent_name=agent_name,
                        now=now,
                        lifetime_cap=cap,
                    )
                    if item.token is not None and item.watch is not None and urls_out is not None:
                        link = f"{base}/callbacks/watches/{item.watch.id}/{item.token}"
                        # Before the commit: a lost file would lose a link nobody can recover.
                        _write_urls(urls_out, {item.legacy_id: link})
                moved.append(item)
            held: list[TimerImport] = []
            cancelled = 0
            if timers:
                assert tenant is not None and agent_id
                async with runtime.sessionmaker.begin() as db:
                    for timer in timers:
                        held.append(
                            await import_held_timer(
                                db, timer, tenant_id=tenant, agent_id=agent_id, now=now
                            )
                        )
                    if full:
                        cancelled = await cancel_missing_timers(
                            db,
                            tenant_id=tenant,
                            agent_id=agent_id,
                            keep={t.source_id for t in timers},
                            now=now,
                        )
            for row in rows:
                detail = row.source_state.get("mapping_error")
                suffix = f": {detail}" if detail else ""
                typer.echo(f"{row.id} paused {row.source_state.get('reason')}{suffix}")
            for item in moved:
                if item.watch is None:
                    typer.echo(f"callback {item.legacy_id} {item.outcome}")
                    continue
                note = f" lifetime capped to {cap.days} days" if item.capped and cap else ""
                typer.echo(
                    f"{item.watch.id} paused {item.outcome} callback owner="
                    f"{item.watch.owner_account_id} channel={item.watch.parent_channel_id} "
                    f"thread={item.watch.thread_id}{note}"
                    f"{' link written' if item.token else ''}"
                )
            cut = sum(1 for row in rows if row.source_state.get("truncated")) + sum(
                1 for item in moved if item.truncated
            )
            capped = over_cap + sum(1 for item in moved if item.outcome == "owner_cap")
            if capped or cut:
                typer.echo(f"watches owner_cap={capped} truncated={cut}")
            if timers:
                counts = {
                    k: sum(1 for t in held if t.outcome == k)
                    for k in ("held", "updated", "existing", "past", "unsupported")
                }
                counts["truncated"] = sum(1 for t in held if t.truncated)
                if full:
                    counts["cancelled"] = cancelled
                typer.echo("timers " + " ".join(f"{k}={n}" for k, n in counts.items()))

    asyncio.run(run())


@watches_app.command("go-live")
def go_live_command(
    tenant: Annotated[uuid.UUID, typer.Option("--tenant")],
    agent_id: Annotated[str, typer.Option("--agent-id")],
    agent_name: Annotated[str, typer.Option("--agent-name")],
) -> None:
    """Make everything an import holds for this agent live. Safe to rerun.

    Each imported watch is confirmed on the owner, channel and thread the import
    printed, and resumes unless it was paused where it came from. Each unpaused held
    timer still more than a minute away is scheduled once; a due one is skipped. DM and
    channel timers of an agent not on Managed Agents never fire: they are listed by name,
    counted as not_scheduled and left held. A watch that cannot go live is listed with
    the reason and stays as it was.
    """

    async def run() -> tuple[Counter[str], Counter[str]]:
        watches: Counter[str] = Counter()
        async with build_runtime(load_settings()) as runtime:
            await check_agent(runtime, tenant_id=tenant, agent_id=agent_id, agent_name=agent_name)
            async with runtime.sessionmaker() as db:
                held = await list_unconfirmed_imports(db, tenant_id=tenant, agent_id=agent_id)
            for watch in held:
                if watch.source_state.get("tool") == "read_thread":
                    typer.echo(
                        f"Warning: {watch.id} reads messages, which needs a thread ID. "
                        "Channel and DM IDs are unsupported; use read_channel for channels.",
                        err=True,
                    )
                try:
                    async with runtime.sessionmaker.begin() as db:
                        row = await confirm_import(
                            db,
                            watch.id,
                            tenant_id=tenant,
                            owner_account_id=watch.owner_account_id,
                            agent_id=agent_id,
                            parent_channel_id=watch.parent_channel_id,
                            thread_id=watch.thread_id,
                            now=datetime.now(UTC),
                        )
                except ValueError as exc:
                    watches["refused"] += 1
                    typer.echo(f"{watch.id} not live: {exc}")
                    continue
                watches["live" if row.status == "active" else "kept_paused"] += 1
            async with runtime.sessionmaker() as db:
                scope_timers = await agent_runs_scope_timers(
                    db, tenant_id=tenant, agent_id=agent_id
                )
                unrunnable = (
                    []
                    if scope_timers
                    else await list_unrunnable_timers(db, tenant_id=tenant, agent_id=agent_id)
                )
            for source_id in unrunnable:
                typer.echo(
                    f"timer {source_id} not scheduled: DM and channel timers need an agent "
                    "on Managed Agents"
                )
            timers = await settle_held_timers(
                runtime.sessionmaker,
                tenant_id=tenant,
                agent_id=agent_id,
                agent_name=agent_name,
                now=datetime.now(UTC),
                scope_timers=scope_timers,
            )
        counts = Counter({str(k): n for k, n in timers.items()})
        if unrunnable:
            counts["not_scheduled"] = len(unrunnable)
        return watches, counts

    watches, timers = asyncio.run(run())
    typer.echo(
        "watches " + " ".join(f"{k}={watches[k]}" for k in ("live", "kept_paused", "refused"))
    )
    typer.echo("timers " + (" ".join(f"{k}={n}" for k, n in sorted(timers.items())) or "none"))


@watches_app.command("unpause")
def unpause_command(
    tenant: Annotated[uuid.UUID, typer.Option("--tenant")],
    watch_id: Annotated[
        uuid.UUID | None, typer.Option("--watch-id", help="A confirmed imported watch")
    ] = None,
    timer_id: Annotated[
        str | None, typer.Option("--timer-id", help="One held timer, by its source id")
    ] = None,
    all_timers: Annotated[
        bool, typer.Option("--all-timers", help="Every paused held timer of the tenant")
    ] = False,
) -> None:
    """Let a watch or held timers that were paused where they came from run here."""
    if sum(x is not None and x is not False for x in (watch_id, timer_id, all_timers)) != 1:
        raise typer.BadParameter("Give exactly one of --watch-id, --timer-id, --all-timers")

    async def run() -> None:
        async with build_runtime(load_settings()) as runtime:
            async with runtime.sessionmaker.begin() as db:
                if watch_id is not None:
                    try:
                        row = await unpause_imported_watch(
                            db, watch_id, tenant_id=tenant, now=datetime.now(UTC)
                        )
                    except ValueError as exc:
                        raise typer.BadParameter(str(exc)) from None
                    typer.echo(f"{row.id} {row.status}")
                    return
                changed = await unpause_timers(db, tenant_id=tenant, source_id=timer_id)
            typer.echo(f"timers unpaused={changed}")

    asyncio.run(run())


@watches_app.command("renew")
def renew_command(
    watch_id: uuid.UUID, tenant: Annotated[uuid.UUID, typer.Option("--tenant")]
) -> None:
    """Give a callback watch and its links a fresh lifetime from now (set by the
    callback_watch_lifetime feature; 30 days unless a server admin changed it)."""

    async def run() -> None:
        async with build_runtime(load_settings()) as runtime:
            async with runtime.sessionmaker.begin() as db:
                try:
                    row = await renew_callback_watch(
                        db,
                        watch_id,
                        tenant_id=tenant,
                        now=datetime.now(UTC),
                        lifetime=await callback_lifetime(db, runtime.settings, tenant),
                    )
                except ValueError as exc:
                    raise typer.BadParameter(str(exc)) from None
            ends = row.expires_at.isoformat() if row.expires_at else "never"
            typer.echo(f"{row.id} expires {ends}")

    asyncio.run(run())


@watches_app.command("rotate-link")
def rotate_link_command(
    watch_id: uuid.UUID,
    tenant: Annotated[uuid.UUID, typer.Option("--tenant")],
    urls_out: Annotated[
        Path | None,
        typer.Option("--urls-out", help="Write the new link here (JSON, mode 0600), not to screen"),
    ] = None,
) -> None:
    """Replace every link of a callback watch with a new one, shown once."""
    base = load_watches_settings().callback_base_url
    if base is None:
        raise typer.BadParameter("Set DAIMON_WATCHES__CALLBACK_BASE_URL first")

    async def run() -> None:
        async with build_runtime(load_settings()) as runtime:
            async with runtime.sessionmaker.begin() as db:
                try:
                    token = await rotate_callback_link(
                        db, watch_id, tenant_id=tenant, now=datetime.now(UTC)
                    )
                except ValueError as exc:
                    raise typer.BadParameter(str(exc)) from None
                url = f"{base}/callbacks/watches/{watch_id}/{token}"
                if urls_out is not None:
                    _write_urls(urls_out, {str(watch_id): url})
            if urls_out is None:
                typer.echo(url)
            else:
                typer.echo(f"{watch_id} link written")

    asyncio.run(run())


ScopeOption = Annotated[
    str, typer.Option("--scope", help="global, or tenant:TENANT_UUID to override it for one tenant")
]


@limits_app.command("set")
def limits_set_command(
    scope: ScopeOption,
    per_watch: Annotated[
        int | None,
        typer.Option("--per-watch", min=1, max=240, help="Most reports one watch makes a day"),
    ] = None,
    per_owner: Annotated[
        int | None,
        typer.Option(
            "--per-owner", min=1, max=1000, help="Most reports one owner's watches make a day"
        ),
    ] = None,
) -> None:
    """Set the daily limits for --scope; a limit left out returns to its default (24 and 100).

    Every firing reads them, so a lowered limit holds from the next firing. A watch
    can ask for more than 24 a day only up to this limit, and only with an admin.
    """

    async def run() -> None:
        async with build_runtime(load_settings()) as runtime, runtime.sessionmaker.begin() as db:
            await set_watch_limits(db, scope=scope, per_watch=per_watch, per_owner=per_owner)

    try:
        asyncio.run(run())
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--scope") from None
    typer.echo(f"{scope}: per watch {per_watch or 'default'}, per owner {per_owner or 'default'}")


@limits_app.command("show")
def limits_show_command(
    tenant: Annotated[uuid.UUID, typer.Option("--tenant")],
    as_json: Annotated[bool, JSON_OPTION] = False,
) -> None:
    """Show the daily limits in force for --tenant: its own, else the global ones, else defaults."""

    async def run() -> dict[str, int]:
        async with build_runtime(load_settings()) as runtime, runtime.sessionmaker() as db:
            ceilings = await watch_ceilings(db, tenant)
        return {"per_watch": ceilings.per_watch, "per_owner": ceilings.per_owner}

    limits = asyncio.run(run())
    if as_json:
        typer.echo(json.dumps(limits))
        return
    typer.echo(f"per watch {limits['per_watch']}, per owner {limits['per_owner']}")
