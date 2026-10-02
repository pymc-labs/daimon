"""daimon channels ... sub-app: per-channel budgets, admins, skills, isolation and protection."""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal, cast

import httpx
import typer
from cryptography.fernet import InvalidToken
from daimon.adapters.cli.errors import run_cli
from daimon.adapters.cli.flags import JSON_OPTION
from daimon.adapters.cli.output import emit_rows
from daimon.adapters.cli.runtime import CliRuntime, build_runtime
from daimon.core.authz import Action, Subject
from daimon.core.channel_admins import InvalidChannelAdminIds, normalize_channel_admin_ids
from daimon.core.channel_budget import (
    BUDGET_WINDOWS,
    ChannelBudgetError,
    describe_budget,
    load_budget_status,
    parse_budget_spec,
)
from daimon.core.channel_isolation_setup import set_channel_isolation
from daimon.core.channel_protection import ChannelProtectionRefused, set_channel_protection
from daimon.core.channel_skills import REFUSALS as CHANNEL_SKILL_REFUSALS
from daimon.core.channel_skills import add_skill_to_channel
from daimon.core.config import load_settings
from daimon.core.errors import DaimonError, StoreError
from daimon.core.github_credentials import build_multifernet, decrypt_token
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores import channel_budgets
from daimon.core.stores.channel_admins import (
    delete_channel_admins,
    get_channel_admins,
    list_channel_admins,
    set_channel_admins,
)
from daimon.core.stores.channel_skills import list_channel_skills, remove_channel_skill
from daimon.core.stores.domain import Platform
from daimon.core.stores.security_audit import append_event
from daimon.core.stores.slack_bot_tokens import get_slack_bot_token
from daimon.core.stores.tenants import get_tenant
from daimon.core.tenant_summary import ChannelSummary, load_tenant_summary
from pydantic import BaseModel
from rich.console import Console
from rich.markup import escape
from sqlalchemy.ext.asyncio import AsyncSession

channels_app = typer.Typer(
    help="Channels: a summary, spend budgets, channel admins, isolation and protection."
)
budget_app = typer.Typer(
    help="A channel's spend budget: new turns there stop once its spend reaches the limit."
)
channels_app.add_typer(budget_app, name="budget")
admins_app = typer.Typer(
    help="A channel's admins: groups and members who run it on top of the server admins."
)
channels_app.add_typer(admins_app, name="admins")
skills_app = typer.Typer(
    help="A channel's extra skills: added to whatever agent answers there, there only."
)
channels_app.add_typer(skills_app, name="skills")
isolation_app = typer.Typer(help="Isolate a channel with its own agent, or lift isolation.")
channels_app.add_typer(isolation_app, name="isolation")

_PLATFORMS = ("discord", "slack", "teams")
_CHANNEL_HELP = "Channel id; a thread budgets against its parent channel."
_TEAMS_THREAD = ";messageid="
_DISCORD_API = "https://discord.com/api/v10"
_DISCORD_THREAD_TYPES = frozenset({10, 11, 12})
_DISCORD_MISSING = frozenset({403, 404})  # the bot cannot see the channel, or it is gone
_SLACK_CONVERSATIONS_INFO = "https://slack.com/api/conversations.info"


@isolation_app.command("set")
def isolation_set_command(
    platform: str,
    workspace_id: str,
    channel_id: str,
    fork_from: Annotated[str | None, typer.Option("--fork-from")] = None,
) -> None:
    """Seal and isolate a channel, copying an agent when --fork-from is given."""
    console = Console(highlight=False)

    async def _run() -> None:
        async with build_runtime(load_settings()) as rt:
            tenant_id = await _existing_tenant_id(rt, platform=platform, workspace_id=workspace_id)
            change = await set_channel_isolation(
                rt.anthropic,
                rt.sessionmaker,
                tenant_id=tenant_id,
                platform=platform,
                channel_id=channel_id,
                isolated=True,
                default=rt.deployment_default,
                actor_account_id=None,
                fork=fork_from is not None,
                fork_from=fork_from,
                public_url=str(rt.settings.mcp.public_url) if rt.settings.mcp.public_url else None,
                subject=Subject(is_admin=True),
            )
            console.print(f"{channel_id}: isolated, agent {change.agent_name}")

    run_cli(_run(), console=console)


@isolation_app.command("lift")
def isolation_lift_command(platform: str, workspace_id: str, channel_id: str) -> None:
    """End isolation and remove its seal and exclusive agent pins."""
    console = Console(highlight=False)

    async def _run() -> None:
        async with build_runtime(load_settings()) as rt:
            tenant_id = await _existing_tenant_id(rt, platform=platform, workspace_id=workspace_id)
            await set_channel_isolation(
                rt.anthropic,
                rt.sessionmaker,
                tenant_id=tenant_id,
                platform=platform,
                channel_id=channel_id,
                isolated=False,
                default=rt.deployment_default,
                actor_account_id=None,
                drop_seal_and_pins=True,
                subject=Subject(is_admin=True),
            )
            console.print(f"{channel_id}: isolation, seal and pins removed")

    run_cli(_run(), console=console)


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
        await _audit_budget_change(
            session, command="set", tenant_id=tenant_id, platform=platform, channel_id=target
        )
        status = await load_budget_status(session, budget, now=datetime.now(UTC))
    console.print(f"{platform}:{workspace_id} channel {target}: {describe_budget(status)}")


async def _audit_budget_change(
    session: AsyncSession,
    *,
    command: Literal["set", "clear"],
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str,
) -> None:
    """Record an operator's budget change in the same transaction, as the MCP tools are."""
    await append_event(
        session,
        tenant_id=tenant_id,
        account_id=None,
        agent_id=None,
        platform=platform,
        platform_user_id=None,
        tool_name=f"cli/channels budget {command}",
        operation=Action.SET_CHANNEL_BUDGET,
        outcome="allowed",
        reason=f"channel:{channel_id}",
    )


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
        if cleared:
            await _audit_budget_change(
                session, command="clear", tenant_id=tenant_id, platform=platform, channel_id=target
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


class ChannelListing(BaseModel):
    channel_id: str
    agent: str | None
    environment: str | None
    isolated: bool
    admins: str
    budget: str


def _listing(channel: ChannelSummary) -> ChannelListing:
    admins = [*(f"group {r}" for r in channel.admins.role_ids), *channel.admins.user_ids]
    budget = channel.budget
    return ChannelListing(
        channel_id=channel.channel_id,
        agent=channel.agent_name,
        environment=channel.environment_name,
        isolated=channel.isolated,
        admins=", ".join(admins),
        budget=f"${budget.spent_usd} of ${budget.limit_usd} ({budget.window})" if budget else "",
    )


@channels_app.command("list")
def channels_list_command(
    platform: str,
    workspace_id: str,
    as_json: Annotated[bool, JSON_OPTION] = False,
) -> None:
    """List the balance and every configured channel: agent, environment, admins, budget."""
    console = Console(highlight=False)

    async def _run() -> None:
        async with build_runtime(load_settings()) as rt:
            await channels_list(
                rt=rt,
                console=console,
                platform=platform,
                workspace_id=workspace_id,
                as_json=as_json,
            )

    run_cli(_run(), console=console)


async def channels_list(
    *, rt: CliRuntime, console: Console, platform: str, workspace_id: str, as_json: bool
) -> None:
    """The CLI twin of the MCP `get_tenant_summary` tool: the same JSON plus each channel's
    `sealed` and `protected`."""
    tenant_id = await _existing_tenant_id(rt, platform=platform, workspace_id=workspace_id)
    async with rt.sessionmaker() as session:
        # An operator reads the whole access policy anyway (`tenants access-policy get`).
        summary = await load_tenant_summary(
            session,
            tenant_id=tenant_id,
            default=rt.deployment_default,
            now=datetime.now(UTC),
            with_access=True,
        )
    if as_json:
        console.print(json.dumps(asdict(summary)), soft_wrap=True, highlight=False, markup=False)
        return
    console.print(
        f"{platform}:{workspace_id}: balance ${summary.balance_usd} ({summary.funding_mode}), "
        f"default agent {summary.default_agent or 'none'}",
        markup=False,
    )
    emit_rows(
        console,
        [_listing(channel) for channel in summary.channels],
        columns=tuple(ChannelListing.model_fields),
        as_json=False,
    )


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
        typer.Option(
            help="Group id (repeatable): a Discord role, a Slack user group, or a Teams "
            "team's Entra group id, which admits its owners."
        ),
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


_SKILL_COLUMNS = ("channel_id", "name", "skill_id", "version", "owner_agent_name")


@skills_app.command("list")
def channels_skills_list_command(
    platform: str,
    workspace_id: str,
    channel_id: Annotated[str | None, typer.Argument(help="One channel; all when omitted.")] = None,
    as_json: Annotated[bool, JSON_OPTION] = False,
) -> None:
    """List the extra skills channels add to their agent."""
    console = Console(highlight=False)

    async def _run() -> None:
        async with build_runtime(load_settings()) as rt:
            await channels_skills_list(
                rt=rt,
                console=console,
                platform=platform,
                workspace_id=workspace_id,
                channel_id=channel_id,
                as_json=as_json,
            )

    run_cli(_run(), console=console)


async def channels_skills_list(
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
        rows = await list_channel_skills(
            session,
            tenant_id=tenant_id,
            platform=platform,
            channel_id=_channel(platform, channel_id) if channel_id else None,
        )
    emit_rows(console, rows, columns=_SKILL_COLUMNS, as_json=as_json)


@skills_app.command("add")
def channels_skills_add_command(
    platform: str,
    workspace_id: str,
    channel_id: str,
    skill: Annotated[
        str, typer.Argument(help="A skill id, a library skill's name, or agent/name.")
    ],
) -> None:
    """Add a skill, at its latest version, to whatever agent answers in one channel."""
    console = Console(highlight=False)

    async def _run() -> None:
        async with build_runtime(load_settings()) as rt:
            await channels_skills_add(
                rt=rt,
                console=console,
                platform=platform,
                workspace_id=workspace_id,
                channel_id=channel_id,
                skill=skill,
            )

    run_cli(_run(), console=console)


async def channels_skills_add(
    *,
    rt: CliRuntime,
    console: Console,
    platform: str,
    workspace_id: str,
    channel_id: str,
    skill: str,
) -> None:
    tenant_id = await _existing_tenant_id(rt, platform=platform, workspace_id=workspace_id)
    channel = _channel(platform, channel_id)
    async with rt.sessionmaker() as session, session.begin():
        added = await add_skill_to_channel(
            session,
            rt.anthropic,
            tenant_id=tenant_id,
            platform=platform,
            channel_id=channel,
            skill=skill,
            default=rt.deployment_default,
            actor_account_id=None,
        )
        if isinstance(added, str):
            raise DaimonError(CHANNEL_SKILL_REFUSALS[added])
        await _audit_skill_change(
            session, command="add", tenant_id=tenant_id, platform=platform, channel_id=channel
        )
    console.print(f"channel {channel} adds {added.name} ({added.skill_id} {added.version})")


@skills_app.command("remove")
def channels_skills_remove_command(
    platform: str,
    workspace_id: str,
    channel_id: str,
    skill: Annotated[str, typer.Argument(help="The skill's id or name.")],
) -> None:
    """Remove one extra skill from a channel."""
    console = Console(highlight=False)

    async def _run() -> None:
        async with build_runtime(load_settings()) as rt:
            await channels_skills_remove(
                rt=rt,
                console=console,
                platform=platform,
                workspace_id=workspace_id,
                channel_id=channel_id,
                skill=skill,
            )

    run_cli(_run(), console=console)


async def channels_skills_remove(
    *,
    rt: CliRuntime,
    console: Console,
    platform: str,
    workspace_id: str,
    channel_id: str,
    skill: str,
) -> None:
    tenant_id = await _existing_tenant_id(rt, platform=platform, workspace_id=workspace_id)
    channel = _channel(platform, channel_id)
    async with rt.sessionmaker() as session, session.begin():
        rows = await list_channel_skills(
            session, tenant_id=tenant_id, platform=platform, channel_id=channel
        )
        match = next((row for row in rows if skill.strip() in (row.skill_id, row.name)), None)
        if match is not None:
            await remove_channel_skill(
                session,
                tenant_id=tenant_id,
                platform=platform,
                channel_id=channel,
                skill_id=match.skill_id,
            )
            await _audit_skill_change(
                session,
                command="remove",
                tenant_id=tenant_id,
                platform=platform,
                channel_id=channel,
            )
    console.print(f"removed {match.name}" if match else f"channel {channel} has no such skill")


async def _audit_skill_change(
    session: AsyncSession,
    *,
    command: Literal["add", "remove"],
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str,
) -> None:
    """Record an operator's channel skill change in the same transaction."""
    await append_event(
        session,
        tenant_id=tenant_id,
        account_id=None,
        agent_id=None,
        platform=platform,
        platform_user_id=None,
        tool_name=f"cli/channels skills {command}",
        operation=Action.SET_CHANNEL_SKILLS,
        outcome="allowed",
        reason=f"channel:{channel_id}",
    )


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


async def _channel_label(
    rt: CliRuntime,
    *,
    platform: str,
    workspace_id: str,
    channel_id: str,
    discord_transport: httpx.AsyncBaseTransport | None,
    slack_transport: httpx.AsyncBaseTransport | None,
) -> str | None:
    """The channel's name, to name a copied agent after; None when it can't be
    read, as `set_channel_isolation` the tool does. The CLI holds no Teams Graph
    access, so a Teams copy is named from the channel id."""
    if platform == "teams":
        return None
    if platform == "slack":
        return await _fetch_slack_channel_name(
            rt, team_id=workspace_id, channel_id=channel_id, transport=slack_transport
        )
    if rt.settings.discord is None:
        return None
    try:
        found = await _fetch_discord_channel(
            rt.settings.discord.bot_token.get_secret_value(),
            channel_id=channel_id,
            transport=discord_transport,
        )
    except DaimonError:
        return None
    if found is None or str(found.get("guild_id")) != workspace_id:
        return None
    name = found.get("name")
    return name if isinstance(name, str) else None


async def _fetch_slack_channel_name(
    rt: CliRuntime, *, team_id: str, channel_id: str, transport: httpx.AsyncBaseTransport | None
) -> str | None:
    """The Slack channel's name, read with the workspace's bot token; None without one."""
    keys = tuple(key.get_secret_value() for key in rt.settings.crypto.keys)
    if not keys:
        return None
    async with rt.sessionmaker() as session:
        row = await get_slack_bot_token(session, team_id=team_id)
    if row is None:
        return None
    try:
        token = decrypt_token(build_multifernet(keys), row.encrypted_token)
        async with httpx.AsyncClient(timeout=10.0, transport=transport) as http:
            response = await http.get(
                _SLACK_CONVERSATIONS_INFO,
                params={"channel": channel_id},
                headers={"Authorization": f"Bearer {token}"},
            )
        body = cast("dict[str, Any]", response.json()) if response.is_success else {}
    except (InvalidToken, httpx.HTTPError, ValueError):
        return None
    channel = cast("dict[str, Any]", body.get("channel") or {}) if body.get("ok") else {}
    name = channel.get("name")
    return name if isinstance(name, str) else None


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
    slack_transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    _validate_platform(platform)
    if end and fork_from is not None:
        raise typer.BadParameter("--fork-from only applies when isolating")
    if lift_seal_and_pins and not end:
        raise typer.BadParameter("--lift-seal-and-pins only applies with --end")
    channel, _, _ = _ids(platform, channel_id, roles=[], users=[])
    tenant_id = await _existing_tenant_id(rt, platform=platform, workspace_id=workspace_id)
    label = (
        await _channel_label(
            rt,
            platform=platform,
            workspace_id=workspace_id,
            channel_id=channel,
            discord_transport=discord_transport,
            slack_transport=slack_transport,
        )
        if fork_from is not None
        else None
    )
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


@channels_app.command("protect")
def channels_protect_command(
    platform: str,
    workspace_id: str,
    channel_id: Annotated[
        str, typer.Argument(help="The channel's id; a thread names its channel.")
    ],
    protect: Annotated[
        bool | None,
        typer.Option("--protect/--unprotect", help="Stop, or allow again, every post there."),
    ] = None,
    seal: Annotated[
        bool | None,
        typer.Option(
            "--seal/--unseal", help="Make its content readable only from inside it, or lift that."
        ),
    ] = None,
) -> None:
    """Protect or seal one channel, or lift either; an isolated channel stays sealed."""
    console = Console(highlight=False)

    async def _run() -> None:
        async with build_runtime(load_settings()) as rt:
            await channels_protect(
                rt=rt,
                console=console,
                platform=platform,
                workspace_id=workspace_id,
                channel_id=channel_id,
                protect=protect,
                seal=seal,
            )

    run_cli(_run(), console=console)


async def channels_protect(
    *,
    rt: CliRuntime,
    console: Console,
    platform: str,
    workspace_id: str,
    channel_id: str,
    protect: bool | None,
    seal: bool | None,
) -> None:
    _validate_platform(platform)
    if protect is None and seal is None:
        raise typer.BadParameter("pass --protect/--unprotect, --seal/--unseal or both")
    channel, _, _ = _ids(platform, channel_id, roles=[], users=[])
    tenant_id = await _existing_tenant_id(rt, platform=platform, workspace_id=workspace_id)
    try:
        change = await set_channel_protection(
            rt.anthropic,
            rt.sessionmaker,
            tenant_id=tenant_id,
            channel_id=channel,
            protected=protect,
            sealed=seal,
            # The CLI is the deployment operator.
            subject=Subject(is_admin=True),
            default=rt.deployment_default,
        )
    except ChannelProtectionRefused as exc:
        console.print(f"[red]{escape(str(exc))} Nothing was changed.[/red]")
        raise typer.Exit(1) from exc
    state = ", ".join(
        [
            "protected" if change.protected else "not protected",
            "sealed" if change.sealed else "not sealed",
        ]
    )
    status = "now" if change.changed else "already"
    console.print(f"{platform}:{workspace_id} channel {channel}: {status} {state}.")
    if change.network_warning:
        console.print(f"[yellow]{escape(change.network_warning)}[/yellow]")
