"""daimon channels ... sub-app: per-channel spend budgets, channel admins and isolation."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any, cast

import httpx
import typer
from daimon.adapters.cli.errors import run_cli
from daimon.adapters.cli.flags import JSON_OPTION
from daimon.adapters.cli.output import emit_rows
from daimon.adapters.cli.runtime import CliRuntime, build_runtime
from daimon.core.authz import Subject
from daimon.core.channel_admins import InvalidChannelAdminIds, normalize_channel_admin_ids
from daimon.core.channel_budget import (
    BUDGET_WINDOWS,
    ChannelBudgetError,
    describe_budget,
    load_budget_status,
    parse_budget_spec,
)
from daimon.core.channel_isolation_setup import set_channel_isolation
from daimon.core.config import load_settings
from daimon.core.errors import DaimonError, StoreError
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores import channel_budgets
from daimon.core.stores.channel_admins import (
    delete_channel_admins,
    get_channel_admins,
    list_channel_admins,
    set_channel_admins,
)
from daimon.core.stores.domain import Platform
from daimon.core.stores.tenants import get_tenant
from pydantic import BaseModel
from rich.console import Console
from rich.markup import escape

channels_app = typer.Typer(help="Channels: spend budgets, channel admins and isolation.")
budget_app = typer.Typer(
    help="A channel's spend budget: new turns there stop once its spend reaches the limit."
)
channels_app.add_typer(budget_app, name="budget")
admins_app = typer.Typer(
    help="A channel's admins: roles and members who run it on top of the server admins."
)
channels_app.add_typer(admins_app, name="admins")

_PLATFORMS = ("discord", "slack", "teams")
_CHANNEL_HELP = "Channel id; a thread budgets against its parent channel."
_TEAMS_THREAD = ";messageid="
_DISCORD_API = "https://discord.com/api/v10"
_DISCORD_THREAD_TYPES = frozenset({10, 11, 12})
_DISCORD_MISSING = frozenset({403, 404})  # the bot cannot see the channel, or it is gone


class BudgetListing(BaseModel):
    channel_id: str
    limit_usd: Decimal
    window: str
    starts_at: datetime | None
    ends_at: datetime | None
    spent_usd: Decimal
    active: bool
    summary: str


def _validate_platform(value: str) -> Platform:
    if value in _PLATFORMS:
        return value  # type: ignore[return-value]
    raise typer.BadParameter(f"unsupported platform {value!r}; valid: {', '.join(_PLATFORMS)}")


def _channel(platform: str, value: str) -> str:
    """A thread id budgets against its channel.

    Slack threads are `<channel>:<thread ts>`, Teams threads
    `<channel>;messageid=<root>`; a Teams channel id itself contains ":".
    """
    separator = _TEAMS_THREAD if platform == "teams" else ":"
    channel_id = value.strip().partition(separator)[0].strip()
    if not channel_id:
        raise typer.BadParameter("channel id must not be empty")
    return channel_id


async def _fetch_discord_channel(
    token: str, *, channel_id: str, transport: httpx.AsyncBaseTransport | None
) -> dict[str, Any] | None:
    """The channel as Discord returns it to the bot; None when the bot cannot see it."""
    try:
        async with httpx.AsyncClient(
            base_url=_DISCORD_API,
            headers={"Authorization": f"Bot {token}"},
            timeout=10.0,
            transport=transport,
        ) as http:
            response = await http.get(f"/channels/{channel_id}")
    except httpx.HTTPError as exc:
        raise DaimonError(f"could not reach Discord to look up channel {channel_id}") from exc
    if response.status_code in _DISCORD_MISSING:
        return None
    if not response.is_success:
        raise DaimonError(
            f"Discord returned HTTP {response.status_code} looking up channel {channel_id}"
        )
    return cast("dict[str, Any]", response.json())


async def _discord_budget_channel(
    rt: CliRuntime,
    *,
    guild_id: str,
    channel_id: str,
    transport: httpx.AsyncBaseTransport | None,
    missing_ok: bool = False,
) -> str:
    """The server channel a Discord id budgets against: a thread's parent.

    Spend is attributed to parent channels, so a budget saved on a thread id
    would never gate anything; the id is looked up with the bot token. With
    `missing_ok`, an id the bot cannot see (403 or 404), or any id when no bot
    token is set, is returned as given, so the budget of a deleted channel can
    still be cleared.
    """
    if not channel_id.isdigit():
        raise typer.BadParameter(f"{channel_id!r} is not a Discord channel id")
    if rt.settings.discord is None:
        if missing_ok:
            return channel_id
        raise DaimonError(
            "DAIMON_DISCORD__BOT_TOKEN is not set; it is needed to look the channel up"
        )
    channel = await _fetch_discord_channel(
        rt.settings.discord.bot_token.get_secret_value(), channel_id=channel_id, transport=transport
    )
    if channel is None:
        if missing_ok:
            return channel_id
        raise DaimonError(f"Discord channel {channel_id} is not visible to daimon")
    if str(channel.get("guild_id")) != guild_id:
        raise DaimonError(f"channel {channel_id} is not in server {guild_id}")
    if channel.get("type") in _DISCORD_THREAD_TYPES:
        return str(channel["parent_id"])
    return channel_id


async def _existing_tenant_id(rt: CliRuntime, *, platform: str, workspace_id: str) -> uuid.UUID:
    tenant_id = derive_tenant_uuid(platform=_validate_platform(platform), workspace_id=workspace_id)
    async with rt.sessionmaker() as session:
        if await get_tenant(session, tenant_id) is None:
            raise StoreError(f"no tenant {platform}:{workspace_id}")
    return tenant_id


@budget_app.command("set")
def budget_set_command(
    platform: str,
    workspace_id: str,
    channel_id: Annotated[str, typer.Argument(help=_CHANNEL_HELP)],
    usd: Annotated[str, typer.Argument(help="Limit in dollars; 0 stops the channel.")],
    window: Annotated[
        str, typer.Option("--window", help=f"One of {', '.join(BUDGET_WINDOWS)}.")
    ] = "monthly",
    starts_at: Annotated[
        str | None, typer.Option("--starts-at", help="ISO 8601, UTC without an offset.")
    ] = None,
    ends_at: Annotated[
        str | None, typer.Option("--ends-at", help="ISO 8601; fixed windows only.")
    ] = None,
) -> None:
    """Set or replace a channel's budget."""
    settings = load_settings()
    console = Console(highlight=False)

    async def _with_runtime() -> None:
        async with build_runtime(settings) as rt:
            await budget_set(
                rt=rt,
                console=console,
                platform=platform,
                workspace_id=workspace_id,
                channel_id=channel_id,
                usd=usd,
                window=window,
                starts_at=starts_at,
                ends_at=ends_at,
            )

    run_cli(_with_runtime(), console=console)


async def budget_set(
    *,
    rt: CliRuntime,
    console: Console,
    platform: str,
    workspace_id: str,
    channel_id: str,
    usd: str,
    window: str,
    starts_at: str | None,
    ends_at: str | None,
    discord_transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    try:
        spec = parse_budget_spec(limit_usd=usd, window=window, starts_at=starts_at, ends_at=ends_at)
    except ChannelBudgetError as exc:
        raise typer.BadParameter(str(exc)) from exc
    target = _channel(platform, channel_id)
    tenant_id = await _existing_tenant_id(rt, platform=platform, workspace_id=workspace_id)
    if platform == "discord":
        target = await _discord_budget_channel(
            rt, guild_id=workspace_id, channel_id=target, transport=discord_transport
        )
    async with rt.sessionmaker() as session, session.begin():
        budget = await channel_budgets.set_channel_budget(
            session,
            tenant_id=tenant_id,
            platform=platform,
            channel_id=target,
            limit_usd=spec.limit_usd,
            window=spec.window,
            starts_at=spec.starts_at,
            ends_at=spec.ends_at,
            set_by_account_id=None,
        )
        status = await load_budget_status(session, budget, now=datetime.now(UTC))
    console.print(f"{platform}:{workspace_id} channel {target}: {describe_budget(status)}")


@budget_app.command("clear")
def budget_clear_command(
    platform: str,
    workspace_id: str,
    channel_id: Annotated[str, typer.Argument(help=_CHANNEL_HELP)],
) -> None:
    """Remove a channel's budget."""
    settings = load_settings()
    console = Console(highlight=False)

    async def _with_runtime() -> None:
        async with build_runtime(settings) as rt:
            await budget_clear(
                rt=rt,
                console=console,
                platform=platform,
                workspace_id=workspace_id,
                channel_id=channel_id,
            )

    run_cli(_with_runtime(), console=console)


async def budget_clear(
    *,
    rt: CliRuntime,
    console: Console,
    platform: str,
    workspace_id: str,
    channel_id: str,
    discord_transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    target = _channel(platform, channel_id)
    tenant_id = await _existing_tenant_id(rt, platform=platform, workspace_id=workspace_id)
    if platform == "discord":
        target = await _discord_budget_channel(
            rt,
            guild_id=workspace_id,
            channel_id=target,
            transport=discord_transport,
            missing_ok=True,
        )
    async with rt.sessionmaker() as session, session.begin():
        cleared = await channel_budgets.delete_channel_budget(
            session, tenant_id=tenant_id, platform=platform, channel_id=target
        )
    status = "budget cleared" if cleared else "had no budget"
    console.print(f"{platform}:{workspace_id} channel {target}: {status}")


@budget_app.command("list")
def budget_list_command(
    platform: str,
    workspace_id: str,
    as_json: Annotated[bool, JSON_OPTION] = False,
) -> None:
    """List a tenant's channel budgets with their spend."""
    settings = load_settings()
    console = Console(highlight=False)

    async def _with_runtime() -> None:
        async with build_runtime(settings) as rt:
            await budget_list(
                rt=rt,
                console=console,
                platform=platform,
                workspace_id=workspace_id,
                as_json=as_json,
            )

    run_cli(_with_runtime(), console=console)


async def budget_list(
    *, rt: CliRuntime, console: Console, platform: str, workspace_id: str, as_json: bool
) -> None:
    tenant_id = await _existing_tenant_id(rt, platform=platform, workspace_id=workspace_id)
    now = datetime.now(UTC)
    async with rt.sessionmaker() as session:
        budgets = await channel_budgets.list_channel_budgets(session, tenant_id=tenant_id)
        statuses = [await load_budget_status(session, budget, now=now) for budget in budgets]
    rows = [
        BudgetListing(
            channel_id=s.budget.channel_id,
            limit_usd=s.budget.limit_usd,
            window=s.budget.window,
            starts_at=s.budget.starts_at,
            ends_at=s.budget.ends_at,
            spent_usd=s.spent_usd,
            active=s.is_active,
            summary=describe_budget(s),
        )
        for s in statuses
        if s.budget.platform == platform
    ]
    emit_rows(console, rows, columns=("channel_id", "summary", "active"), as_json=as_json)


_ADMIN_COLUMNS = ("channel_id", "role_ids", "user_ids", "updated_at")


def _ids(
    platform: str, channel_id: str, *, roles: list[str], users: list[str]
) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    try:
        return normalize_channel_admin_ids(
            platform, channel_id=_channel(platform, channel_id), role_ids=roles, user_ids=users
        )
    except InvalidChannelAdminIds as exc:
        raise typer.BadParameter(str(exc)) from exc


@admins_app.command("get")
def channels_admins_get_command(
    platform: str,
    workspace_id: str,
    channel_id: Annotated[str | None, typer.Argument(help="One channel; omit for all.")] = None,
    as_json: Annotated[bool, JSON_OPTION] = False,
) -> None:
    """Show channel admins. A channel with none is run by the server admins alone."""
    console = Console(highlight=False)

    async def _run() -> None:
        async with build_runtime(load_settings()) as rt:
            await channels_admins_get(
                rt=rt,
                console=console,
                platform=platform,
                workspace_id=workspace_id,
                channel_id=channel_id,
                as_json=as_json,
            )

    run_cli(_run(), console=console)


async def channels_admins_get(
    *,
    rt: CliRuntime,
    console: Console,
    platform: str,
    workspace_id: str,
    channel_id: str | None,
    as_json: bool,
) -> None:
    tenant_id = await _existing_tenant_id(rt, platform=platform, workspace_id=workspace_id)
    async with rt.sessionmaker() as session:
        if channel_id is None:
            rows = await list_channel_admins(session, tenant_id=tenant_id, platform=platform)
        else:
            channel, _, _ = _ids(platform, channel_id, roles=[], users=[])
            row = await get_channel_admins(
                session, tenant_id=tenant_id, platform=platform, channel_id=channel
            )
            rows = [row] if row is not None else []
    emit_rows(console, rows, columns=_ADMIN_COLUMNS, as_json=as_json)


@admins_app.command("set")
def channels_admins_set_command(
    platform: str,
    workspace_id: str,
    channel_id: str,
    role: Annotated[
        list[str] | None,
        typer.Option(help="Discord role id (repeatable). Slack and Teams have no roles."),
    ] = None,
    user: Annotated[
        list[str] | None,
        typer.Option(help="Platform user id; on Teams the Entra object id (repeatable)."),
    ] = None,
    as_json: Annotated[bool, JSON_OPTION] = False,
) -> None:
    """Replace one channel's admins with the given roles and users."""
    console = Console(highlight=False)

    async def _run() -> None:
        async with build_runtime(load_settings()) as rt:
            await channels_admins_set(
                rt=rt,
                console=console,
                platform=platform,
                workspace_id=workspace_id,
                channel_id=channel_id,
                roles=role or [],
                users=user or [],
                as_json=as_json,
            )

    run_cli(_run(), console=console)


async def channels_admins_set(
    *,
    rt: CliRuntime,
    console: Console,
    platform: str,
    workspace_id: str,
    channel_id: str,
    roles: list[str],
    users: list[str],
    as_json: bool,
) -> None:
    tenant_id = await _existing_tenant_id(rt, platform=platform, workspace_id=workspace_id)
    channel, role_ids, user_ids = _ids(platform, channel_id, roles=roles, users=users)
    if not role_ids and not user_ids:
        raise typer.BadParameter("pass --role or --user; use clear to remove a channel's admins")
    async with rt.sessionmaker() as session, session.begin():
        row = await set_channel_admins(
            session,
            tenant_id=tenant_id,
            platform=platform,
            channel_id=channel,
            role_ids=role_ids,
            user_ids=user_ids,
            actor_account_id=None,
        )
    emit_rows(console, [row], columns=_ADMIN_COLUMNS, as_json=as_json)


@admins_app.command("clear")
def channels_admins_clear_command(platform: str, workspace_id: str, channel_id: str) -> None:
    """Remove one channel's admins, leaving it to the server admins."""
    console = Console(highlight=False)

    async def _run() -> None:
        async with build_runtime(load_settings()) as rt:
            await channels_admins_clear(
                rt=rt,
                console=console,
                platform=platform,
                workspace_id=workspace_id,
                channel_id=channel_id,
            )

    run_cli(_run(), console=console)


async def channels_admins_clear(
    *, rt: CliRuntime, console: Console, platform: str, workspace_id: str, channel_id: str
) -> None:
    tenant_id = await _existing_tenant_id(rt, platform=platform, workspace_id=workspace_id)
    channel, _, _ = _ids(platform, channel_id, roles=[], users=[])
    async with rt.sessionmaker() as session, session.begin():
        removed = await delete_channel_admins(
            session, tenant_id=tenant_id, platform=platform, channel_id=channel
        )
    console.print("cleared" if removed else "no channel admins to clear")


_ISOLATION_PLATFORMS = ("discord", "slack")


@channels_app.command("isolate")
def channels_isolate_command(
    platform: str,
    workspace_id: str,
    channel_id: Annotated[str, typer.Argument(help="The channel's id, never a thread's.")],
    fork_from: Annotated[
        str | None,
        typer.Option(
            "--fork-from",
            help="Copy this agent, without credentials, as the channel's own when it has none.",
        ),
    ] = None,
    end: Annotated[
        bool, typer.Option("--end", help="End isolation; the seal and pins stay.")
    ] = False,
    lift_seal_and_pins: Annotated[
        bool, typer.Option("--lift-seal-and-pins", help="With --end, lift the seal and pins too.")
    ] = False,
) -> None:
    """Isolate a channel: seal it and pin its default agent to it alone, in one write."""
    console = Console(highlight=False)

    async def _run() -> None:
        async with build_runtime(load_settings()) as rt:
            await channels_isolate(
                rt=rt,
                console=console,
                platform=platform,
                workspace_id=workspace_id,
                channel_id=channel_id,
                fork_from=fork_from,
                end=end,
                lift_seal_and_pins=lift_seal_and_pins,
            )

    run_cli(_run(), console=console)


async def channels_isolate(
    *,
    rt: CliRuntime,
    console: Console,
    platform: str,
    workspace_id: str,
    channel_id: str,
    fork_from: str | None = None,
    end: bool = False,
    lift_seal_and_pins: bool = False,
    discord_transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    if platform not in _ISOLATION_PLATFORMS:
        raise typer.BadParameter("channel isolation exists only on Discord and Slack")
    if end and fork_from is not None:
        raise typer.BadParameter("--fork-from only applies when isolating")
    if lift_seal_and_pins and not end:
        raise typer.BadParameter("--lift-seal-and-pins only applies with --end")
    channel, _, _ = _ids(platform, channel_id, roles=[], users=[])
    tenant_id = await _existing_tenant_id(rt, platform=platform, workspace_id=workspace_id)
    label = None
    if fork_from is not None and platform == "discord" and rt.settings.discord is not None:
        found = await _fetch_discord_channel(
            rt.settings.discord.bot_token.get_secret_value(),
            channel_id=channel,
            transport=discord_transport,
        )
        if found is not None and str(found.get("guild_id")) == workspace_id:
            label = cast("str | None", found.get("name"))
    public_url = rt.settings.mcp.public_url if fork_from is not None else None
    change = await set_channel_isolation(
        rt.anthropic,
        rt.sessionmaker,
        tenant_id=tenant_id,
        platform=platform,
        channel_id=channel,
        isolated=not end,
        default=rt.deployment_default,
        actor_account_id=None,
        channel_label=label,
        fork=fork_from is not None,
        fork_from=fork_from,
        public_url=str(public_url) if public_url is not None else None,
        drop_seal_and_pins=lift_seal_and_pins,
        # The CLI is the deployment operator.
        subject=Subject(is_admin=True),
    )
    where = f"{platform}:{workspace_id} channel {channel}"
    if not change.isolated:
        status = "isolation ended" if change.changed else "was not isolated"
        console.print(f"{where}: {status}. {change.end_warning}")
        return
    copied = f", copied from {change.forked_from}" if change.forked_from else ""
    status = "isolated" if change.changed else "already isolated"
    console.print(f"{where}: {status}; its own agent is {change.agent_name}{copied}.")
    for note in (change.dropped_skills_note, change.network_warning):
        if note:
            console.print(f"[yellow]{escape(note)}[/yellow]")
