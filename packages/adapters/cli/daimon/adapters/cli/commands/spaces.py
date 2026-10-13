"""daimon spaces ... sub-app: channels sharing one audience and one rule."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Annotated

import typer
from daimon.adapters.cli.errors import run_cli
from daimon.adapters.cli.runtime import CliRuntime, build_runtime
from daimon.core.access_policy import ChannelRule
from daimon.core.authz import Subject
from daimon.core.config import load_settings
from daimon.core.errors import StoreError
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.space_rules import (
    SpaceChange,
    add_space_channel,
    create_space,
    delete_space,
    remove_space_channel,
)
from daimon.core.stores.domain import Platform
from daimon.core.stores.spaces import (
    count_external_accounts_seen,
    get_space,
    list_space_channels,
    list_spaces_for_tenant,
)
from daimon.core.stores.tenants import get_tenant
from pydantic import ValidationError
from rich.console import Console
from rich.markup import escape

spaces_app = typer.Typer(
    help="Spaces: channels sharing one audience and one rule. An agent whose rule names "
    "only channels of an own Space has the Space as its home."
)

_PLATFORMS: tuple[Platform, ...] = ("discord", "slack", "teams")
# The CLI is the deployment operator.
_OPERATOR = Subject(is_admin=True)
_CONFIRM = typer.Option(
    "--confirm", help="The channels share one audience: everyone in one may read the others."
)
_LOOSEN = typer.Option(
    "--loosen-rules",
    help="Let a channel kept to its own agents, or taking no replies, take the looser Space rule.",
)
_KEEP = typer.Option(
    "--keep-agents",
    help="Let an agent of the Space naming one channel stay that channel's own agent, "
    "with what it read across the Space in its memory.",
)
_UNSEEN = (
    "Daimon can't see who reads a channel, only who it has answered there. A Space "
    "shares every channel's history and its agents' memory with all the others, so "
    "check they have one audience, then repeat with --confirm."
)


async def _tenant_id(rt: CliRuntime, *, platform: str, workspace_id: str) -> uuid.UUID:
    if platform not in _PLATFORMS:
        raise typer.BadParameter(f"unsupported platform {platform!r}; valid: discord, slack, teams")
    tenant_id = derive_tenant_uuid(platform=platform, workspace_id=workspace_id)
    async with rt.sessionmaker() as session:
        if await get_tenant(session, tenant_id) is None:
            raise StoreError(f"no tenant {platform}:{workspace_id}")
    return tenant_id


async def _members(rt: CliRuntime, *, tenant_id: uuid.UUID, name: str) -> list[str]:
    async with rt.sessionmaker() as session:
        space = await get_space(session, tenant_id=tenant_id, name=name)
        if space is None:
            raise StoreError(f"there is no Space named {name}")
        return [
            ref.channel_id
            for ref in await list_space_channels(session, tenant_id=tenant_id, space_id=space.id)
        ]


async def _require_confirmed(
    rt: CliRuntime,
    console: Console,
    *,
    tenant_id: uuid.UUID,
    channel_ids: Sequence[str],
    confirm: bool,
) -> None:
    """Show what is known of each channel's audience; refuse without --confirm."""
    if len(channel_ids) < 2:
        return
    async with rt.sessionmaker() as session:
        seen = await count_external_accounts_seen(
            session, tenant_id=tenant_id, channel_ids=channel_ids
        )
    for channel_id in channel_ids:
        console.print(f"  {channel_id}: {seen[channel_id]} external account(s) answered there")
    if len({count > 0 for count in seen.values()}) > 1:
        console.print("[yellow]Only some of these channels have had external accounts.[/yellow]")
    if not confirm:
        console.print(f"[red]{_UNSEEN} Nothing was changed.[/red]")
        raise typer.Exit(1)


def _rule(rule: ChannelRule) -> str:
    return f"readers {rule.readers}, writers {rule.writers}"


def _print_change(console: Console, change: SpaceChange, verb: str) -> None:
    space = change.space
    channels = ", ".join(change.channel_ids) or "no channels"
    after = ChannelRule.model_validate({"readers": space.readers, "writers": space.writers})
    console.print(f"Space {space.name} {verb}: {_rule(after)}; {channels}.")
    for channel_id, before in change.rules_before:
        console.print(escape(f"  {channel_id}: {_rule(before)} -> {_rule(after)}"))


@spaces_app.command("create")
def spaces_create_command(
    platform: str,
    workspace_id: str,
    name: str,
    channel: Annotated[list[str], typer.Option("--channel", help="A channel id; repeat.")],
    readers: Annotated[str, typer.Option(help="own: kept to its own agents; inside.")] = "own",
    writers: Annotated[
        str | None, typer.Option(help="own or none with readers own; any or none with inside.")
    ] = None,
    confirm: Annotated[bool, _CONFIRM] = False,
    loosen_rules: Annotated[bool, _LOOSEN] = False,
) -> None:
    """Create a Space of existing channels; each takes its rule."""
    console = Console(highlight=False)

    async def _run() -> None:
        async with build_runtime(load_settings()) as rt:
            await spaces_create(
                rt=rt,
                console=console,
                platform=platform,
                workspace_id=workspace_id,
                name=name,
                channel_ids=channel,
                readers=readers,
                writers=writers,
                confirm=confirm,
                loosen_rules=loosen_rules,
            )

    run_cli(_run(), console=console)


async def spaces_create(
    *,
    rt: CliRuntime,
    console: Console,
    platform: str,
    workspace_id: str,
    name: str,
    channel_ids: Sequence[str],
    readers: str = "own",
    writers: str | None = None,
    confirm: bool = False,
    loosen_rules: bool = False,
) -> None:
    tenant_id = await _tenant_id(rt, platform=platform, workspace_id=workspace_id)
    default_writers = "own" if readers == "own" else "any"
    try:
        rule = ChannelRule.model_validate(
            {"readers": readers, "writers": writers or default_writers}
        )
    except ValidationError as exc:
        raise typer.BadParameter(f"--readers {readers} with --writers {writers}") from exc
    channels = list(dict.fromkeys(c.strip() for c in channel_ids))
    await _require_confirmed(
        rt, console, tenant_id=tenant_id, channel_ids=channels, confirm=confirm
    )
    change = await create_space(
        rt.anthropic,
        rt.sessionmaker,
        tenant_id=tenant_id,
        platform=platform,
        name=name,
        rule=rule,
        channel_ids=channels,
        subject=_OPERATOR,
        default=rt.deployment_default,
        loosen_rules=loosen_rules,
    )
    _print_change(console, change, "created")
    if rule.readers == "own":
        console.print(
            "An agent becomes its own when its rule names only these channels: "
            "`daimon agents rule set ... --runs-in` each of them."
        )


@spaces_app.command("add")
def spaces_add_command(
    platform: str,
    workspace_id: str,
    name: str,
    channel_id: str,
    extend_agents: Annotated[
        bool,
        typer.Option("--extend-agents", help="Let the Space's own agents run in the channel too."),
    ] = False,
    confirm: Annotated[bool, _CONFIRM] = False,
    loosen_rules: Annotated[bool, _LOOSEN] = False,
) -> None:
    """Add a channel to a Space; it takes the Space's rule."""
    console = Console(highlight=False)

    async def _run() -> None:
        async with build_runtime(load_settings()) as rt:
            await spaces_add(
                rt=rt,
                console=console,
                platform=platform,
                workspace_id=workspace_id,
                name=name,
                channel_id=channel_id,
                extend_agents=extend_agents,
                confirm=confirm,
                loosen_rules=loosen_rules,
            )

    run_cli(_run(), console=console)


async def spaces_add(
    *,
    rt: CliRuntime,
    console: Console,
    platform: str,
    workspace_id: str,
    name: str,
    channel_id: str,
    extend_agents: bool = False,
    confirm: bool = False,
    loosen_rules: bool = False,
) -> None:
    tenant_id = await _tenant_id(rt, platform=platform, workspace_id=workspace_id)
    members = await _members(rt, tenant_id=tenant_id, name=name)
    await _require_confirmed(
        rt, console, tenant_id=tenant_id, channel_ids=[*members, channel_id], confirm=confirm
    )
    change = await add_space_channel(
        rt.anthropic,
        rt.sessionmaker,
        tenant_id=tenant_id,
        platform=platform,
        name=name,
        channel_id=channel_id,
        subject=_OPERATOR,
        default=rt.deployment_default,
        extend_agents=extend_agents,
        loosen_rules=loosen_rules,
    )
    _print_change(console, change, "now")
    if change.agents:
        console.print(f"{', '.join(change.agents)} now also run in {channel_id}.")


def _print_kept(console: Console, kept: Sequence[str]) -> None:
    if kept:
        console.print(
            f"[yellow]{', '.join(kept)} stay their channel's own agents and still remember "
            "what they read across the Space.[/yellow]"
        )


@spaces_app.command("remove")
def spaces_remove_command(
    platform: str,
    workspace_id: str,
    name: str,
    channel_id: str,
    keep_agents: Annotated[bool, _KEEP] = False,
) -> None:
    """Take a channel out of a Space; it keeps the Space's rule, on its own."""
    console = Console(highlight=False)

    async def _run() -> None:
        async with build_runtime(load_settings()) as rt:
            await spaces_remove(
                rt=rt,
                console=console,
                platform=platform,
                workspace_id=workspace_id,
                name=name,
                channel_id=channel_id,
                keep_agents=keep_agents,
            )

    run_cli(_run(), console=console)


async def spaces_remove(
    *,
    rt: CliRuntime,
    console: Console,
    platform: str,
    workspace_id: str,
    name: str,
    channel_id: str,
    keep_agents: bool = False,
) -> None:
    tenant_id = await _tenant_id(rt, platform=platform, workspace_id=workspace_id)
    change = await remove_space_channel(
        rt.sessionmaker,
        tenant_id=tenant_id,
        name=name,
        channel_id=channel_id,
        subject=_OPERATOR,
        keep_agents=keep_agents,
    )
    _print_change(console, change, "now")
    if change.agents:
        console.print(f"{', '.join(change.agents)} no longer run in {channel_id}.")
    _print_kept(console, change.kept)
    console.print(
        f"{channel_id} keeps the Space's rule. What its agents already remember stays in "
        "their memory."
    )


@spaces_app.command("delete")
def spaces_delete_command(
    platform: str, workspace_id: str, name: str, keep_agents: Annotated[bool, _KEEP] = False
) -> None:
    """Delete a Space; its channels keep its rule, each on its own."""
    console = Console(highlight=False)

    async def _run() -> None:
        async with build_runtime(load_settings()) as rt:
            await spaces_delete(
                rt=rt,
                console=console,
                platform=platform,
                workspace_id=workspace_id,
                name=name,
                keep_agents=keep_agents,
            )

    run_cli(_run(), console=console)


async def spaces_delete(
    *,
    rt: CliRuntime,
    console: Console,
    platform: str,
    workspace_id: str,
    name: str,
    keep_agents: bool = False,
) -> None:
    tenant_id = await _tenant_id(rt, platform=platform, workspace_id=workspace_id)
    change = await delete_space(
        rt.sessionmaker, tenant_id=tenant_id, name=name, subject=_OPERATOR, keep_agents=keep_agents
    )
    console.print(
        f"Space {name} deleted; {', '.join(change.channel_ids) or 'no channels'} keep its rule."
    )
    if change.agents:
        console.print(
            f"[yellow]{', '.join(change.agents)} name several of its channels, so they now run "
            "nowhere until their rule names one.[/yellow]"
        )
    _print_kept(console, change.kept)


@spaces_app.command("list")
def spaces_list_command(platform: str, workspace_id: str) -> None:
    """List a workspace's Spaces and their channels."""
    console = Console(highlight=False)

    async def _run() -> None:
        async with build_runtime(load_settings()) as rt:
            await spaces_list(rt=rt, console=console, platform=platform, workspace_id=workspace_id)

    run_cli(_run(), console=console)


async def spaces_list(
    *, rt: CliRuntime, console: Console, platform: str, workspace_id: str
) -> None:
    tenant_id = await _tenant_id(rt, platform=platform, workspace_id=workspace_id)
    async with rt.sessionmaker() as session:
        spaces = await list_spaces_for_tenant(session, tenant_id=tenant_id)
        for space in spaces:
            refs = await list_space_channels(session, tenant_id=tenant_id, space_id=space.id)
            channels = ", ".join(ref.channel_id for ref in refs) or "no channels"
            console.print(
                escape(
                    f"{space.name}: readers {space.readers}, writers {space.writers}; {channels}"
                )
            )
    if not spaces:
        console.print(f"{platform}:{workspace_id}: no Spaces")
