"""Operator-only offline import and explicit binding confirmation."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
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
    cancel_missing_timers,
    import_held_timer,
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
    confirm_import,
    get_watch,
    import_callback_watch,
    import_watch,
    list_notice_receipts,
    list_tenant_watches,
    renew_callback_watch,
    rotate_callback_link,
    set_watch_limits,
    unpause_imported_watch,
    watch_ceilings,
)

watches_app = typer.Typer(
    help="List a tenant's watches and their owner-notice receipts, import paused watches, "
    "confirm reviewed bindings and set daily firing limits."
)
limits_app = typer.Typer(help="Daily firing limits for every watch, or one tenant's.")
watches_app.add_typer(limits_app, name="limits")


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
    export_file: Path,
    tenant: Annotated[
        uuid.UUID | None, typer.Option("--tenant", help="Tenant for callback watches and timers")
    ] = None,
    agent_id: Annotated[
        str | None, typer.Option("--agent-id", help="Agent id that callback watches report to")
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
        typer.Option("--full", help="Cancel held timers this file no longer lists"),
    ] = False,
) -> None:
    """Import a v1 JSON manifest, offline. Every watch starts paused; no key is copied.

    The file's server must be the tenant's. Callback watches keep their token hash and
    get a new link, written only to --urls-out as {source id: url}. Timers are held
    until `confirm-timers`. A rerun updates what nobody has confirmed yet; with --full
    it also cancels held timers missing from the file.
    """
    if export_file.stat().st_size > 1048576:
        raise typer.BadParameter("Watch export exceeds 1 MiB")
    manifest = WatchExport.model_validate_json(export_file.read_bytes())
    callbacks = [w for w in manifest.watches if isinstance(w, ImportedCallbackWatch)]
    others = [w for w in manifest.watches if not isinstance(w, ImportedCallbackWatch)]
    if (callbacks or manifest.timers or full) and tenant is None:
        raise typer.BadParameter("--tenant is required for callback watches and timers")
    if callbacks:
        if agent_id is None or agent_name is None:
            raise typer.BadParameter("--agent-id and --agent-name are required for callbacks")
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
                if row.external_id != manifest.guild_id:
                    raise typer.BadParameter(
                        f"The file is for server {manifest.guild_id}, not tenant {tenant_id}"
                    )
        if callbacks:
            assert tenant is not None and agent_id and agent_name
            agent = await find_agent_by_daimon_tag(
                runtime.anthropic,
                tenant_id=tenant,
                name=agent_name,
                records=runtime.sessionmaker,
            )
            if agent is None or agent.id != agent_id:
                raise typer.BadParameter(
                    "--agent-id and --agent-name must name one agent of the tenant"
                )

    async def run() -> None:
        base = load_watches_settings().callback_base_url
        urls: dict[str, str] = {}
        async with build_runtime(load_settings()) as runtime:
            await check_targets(runtime)
            now = datetime.now(UTC)
            # Whole-file transaction: no half-import or lost owner notice on crash.
            async with runtime.sessionmaker.begin() as db:
                rows: list[WatchRow] = []
                over_cap = 0
                for entry in others:
                    try:
                        rows.append(await import_watch(db, entry, now=now))
                    except OwnerCapExceeded:
                        over_cap += 1
                moved: list[CallbackImport] = []
                for entry in callbacks:
                    assert tenant is not None and agent_id and agent_name
                    moved.append(
                        await import_callback_watch(
                            db,
                            entry,
                            tenant_id=tenant,
                            agent_id=agent_id,
                            agent_name=agent_name,
                            now=now,
                        )
                    )
                held: list[TimerImport] = []
                for timer in manifest.timers:
                    assert tenant is not None
                    held.append(await import_held_timer(db, timer, tenant_id=tenant, now=now))
                cancelled = 0
                if full:
                    assert tenant is not None
                    cancelled = await cancel_missing_timers(
                        db,
                        tenant_id=tenant,
                        keep={t.source_id for t in manifest.timers},
                        now=now,
                    )
                for item in moved:
                    if item.token is not None and item.watch is not None:
                        urls[item.legacy_id] = (
                            f"{base}/callbacks/watches/{item.watch.id}/{item.token}"
                        )
                if urls and urls_out is not None:
                    # Before the commit: a lost file would lose links nobody can recover.
                    _write_urls(urls_out, urls)
            for row in rows:
                detail = row.source_state.get("mapping_error")
                suffix = f": {detail}" if detail else ""
                typer.echo(f"{row.id} paused {row.source_state.get('reason')}{suffix}")
            for item in moved:
                if item.watch is None:
                    typer.echo(f"callback {item.legacy_id} {item.outcome}")
                    continue
                note = " lifetime capped to 30 days" if item.capped else ""
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
            if manifest.timers or full:
                counts = {
                    k: sum(1 for t in held if t.outcome == k)
                    for k in ("held", "updated", "existing", "past", "unsupported")
                }
                counts["truncated"] = sum(1 for t in held if t.truncated)
                if full:
                    counts["cancelled"] = cancelled
                typer.echo("timers " + " ".join(f"{k}={n}" for k, n in counts.items()))

    asyncio.run(run())


@watches_app.command("confirm-timers")
def confirm_timers_command(
    tenant: Annotated[uuid.UUID, typer.Option("--tenant")],
    agent_id: Annotated[str, typer.Option("--agent-id")],
    agent_name: Annotated[str, typer.Option("--agent-name")],
) -> None:
    """Schedule every unpaused held timer for this agent. Due and over-cap ones are skipped."""

    async def run() -> None:
        async with build_runtime(load_settings()) as runtime:
            counts = await settle_held_timers(
                runtime.sessionmaker,
                tenant_id=tenant,
                agent_id=agent_id,
                agent_name=agent_name,
                now=datetime.now(UTC),
            )
        typer.echo(" ".join(f"{k}={n}" for k, n in sorted(counts.items())) or "no held timers")

    asyncio.run(run())


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
    """Give a callback watch and its links another 30 days from now."""

    async def run() -> None:
        async with build_runtime(load_settings()) as runtime:
            async with runtime.sessionmaker.begin() as db:
                try:
                    row = await renew_callback_watch(
                        db, watch_id, tenant_id=tenant, now=datetime.now(UTC)
                    )
                except ValueError as exc:
                    raise typer.BadParameter(str(exc)) from None
            typer.echo(f"{row.id} expires {row.expires_at.isoformat() if row.expires_at else ''}")

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


@watches_app.command("confirm")
def confirm_command(
    watch_id: uuid.UUID,
    tenant_id: uuid.UUID,
    owner_account_id: uuid.UUID,
    agent_id: str,
    parent_channel_id: str,
    thread_id: str,
) -> None:
    """Confirm a mapped import's exact binding. Page watches first need owner key rotation."""

    async def run() -> None:
        async with build_runtime(load_settings()) as runtime:
            async with runtime.sessionmaker.begin() as db:
                current = await get_watch(db, watch_id)
                if current is not None and current.source_state.get("tool") == "read_thread":
                    typer.echo(
                        "Warning: imported message reads require a thread ID. "
                        "Channel and DM IDs are unsupported; use read_channel for channel sources.",
                        err=True,
                    )
                row = await confirm_import(
                    db,
                    watch_id,
                    tenant_id=tenant_id,
                    owner_account_id=owner_account_id,
                    agent_id=agent_id,
                    parent_channel_id=parent_channel_id,
                    thread_id=thread_id,
                    now=datetime.now(UTC),
                )
            typer.echo(f"{row.id} {row.status}")

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
