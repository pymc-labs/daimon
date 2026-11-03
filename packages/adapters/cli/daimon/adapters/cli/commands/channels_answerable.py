"""daimon channels answerable: the Discord channels where Daimon may answer at all.

Read-only. Daimon may answer in every server channel where the turn gate would
admit a turn: one not under a `writers: none` rule, its own or its category's.
Each channel and each bound thread is put through the same `authorize`
START_TURN check a turn passes, so a thread bound under a `writers: none`
channel answers no more than the channel does. Agent rules (`--runs-in`,
`--nowhere`) are not considered: they pick which agent answers, not whether
Daimon may.

`--expect` takes the list of channels another bot sharing the server does not
answer, the ones it has handed to Daimon. Daimon's answerable set must equal
it: a channel in one and not the other is a difference either way, and exits 1.

It fails closed: anything it cannot tell is reported as answerable or as an
error, never as silent. Channels come from Discord's channel list for the
server, which names every channel, those the bot cannot view included; viewing
permission is ignored, because it can be granted later without touching a
Daimon rule. A channel whose category is not in the list is counted as
answerable unless its own rule silences it, and reported as an error. A thread
binding whose parent channel is not in the list is reported as an error and
not counted.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any, cast

import httpx
import typer
from daimon.adapters.cli.errors import run_cli
from daimon.adapters.cli.flags import JSON_OPTION
from daimon.adapters.cli.output import emit_rows
from daimon.adapters.cli.runtime import CliRuntime, build_runtime
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import Action, authorize, build_subject, build_turn_place
from daimon.core.config import load_settings
from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.permissions import channel_rule
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.domain import ThreadAgentBindingRow
from daimon.core.stores.thread_agent_bindings import list_answering_bindings
from pydantic import BaseModel
from rich.console import Console

_DISCORD_API = "https://discord.com/api/v10"
_DISCORD_CATEGORY = 4


class ChannelAnswer(BaseModel):
    channel_id: str
    name: str
    category_id: str | None
    answers: bool
    why: str


class BindingAnswer(BaseModel):
    thread_id: str
    parent_channel_id: str
    agent: str
    kind: str
    parent_in_server: bool
    answers: bool


class Answerable(BaseModel):
    guild_id: str
    answerable: list[str]
    channels: list[ChannelAnswer]
    thread_bindings: list[BindingAnswer]
    errors: list[str]


class Comparison(BaseModel):
    match: bool
    missing_from_expect: list[str]
    """Daimon may answer here, but the file does not list it."""
    missing_from_daimon: list[str]
    """The file lists it, but Daimon may not answer here."""
    unlisted_thread_bindings: list[BindingAnswer]
    """Bound threads that answer under a parent channel the file does not list."""


def _sorted_ids(ids: set[str]) -> list[str]:
    return sorted(ids, key=lambda i: (len(i), i))


def _admits_turn(
    policy: TenantAccessPolicy,
    *,
    channel_id: str,
    thread_id: str | None = None,
    category_id: str | None,
) -> bool:
    """Whether the turn gate would let a turn start here. No user is named, so
    only the place's rules count, not the invoker allowlist."""
    return authorize(
        policy,
        subject=build_subject(is_admin=False, platform_user_id=None),
        action=Action.START_TURN,
        place=build_turn_place(channel_id=channel_id, thread_id=thread_id, category_id=category_id),
    ).allowed


def compute_answerable(
    policy: TenantAccessPolicy,
    *,
    guild_id: str,
    guild_channels: list[dict[str, Any]],
    bindings: list[ThreadAgentBindingRow],
) -> Answerable:
    """Where Daimon may answer in one server, from its access policy, Discord's
    channel list (threads excluded) and the thread bindings. Pure."""
    categories = {str(c["id"]) for c in guild_channels if c.get("type") == _DISCORD_CATEGORY}
    channels: list[ChannelAnswer] = []
    errors: list[str] = []
    for raw in guild_channels:
        if raw.get("type") == _DISCORD_CATEGORY:
            continue
        channel_id = str(raw["id"])
        parent = raw.get("parent_id")
        category_id = str(parent) if parent is not None else None
        unresolved = category_id is not None and category_id not in categories
        silenced_by_own = channel_rule(policy, channel_id).writers == "none"
        # The same gate a turn passes; an unresolved category is never taken as silencing.
        answers = _admits_turn(
            policy, channel_id=channel_id, category_id=None if unresolved else category_id
        )
        if silenced_by_own:
            why = "channel rule writers none"
        elif not answers:
            why = f"category {category_id} writers none"
        elif unresolved:
            why = f"category {category_id} unknown"
            errors.append(
                f"channel {channel_id}: category {category_id} is not in the server's "
                "channel list; counted as answerable"
            )
        else:
            why = "no writers none rule"
        channels.append(
            ChannelAnswer(
                channel_id=channel_id,
                name=str(raw.get("name") or ""),
                category_id=category_id,
                answers=answers,
                why=why,
            )
        )
    by_id = {c.channel_id: c for c in channels}
    answerable = {c.channel_id for c in channels if c.answers}
    thread_bindings: list[BindingAnswer] = []
    for b in bindings:
        parent = by_id.get(b.parent_channel_id)
        if parent is None:
            errors.append(
                f"thread {b.thread_id}: parent channel {b.parent_channel_id} is not in the "
                "server's channel list; not counted"
            )
        unresolved = parent is not None and parent.category_id not in (None, *categories)
        answers = parent is not None and _admits_turn(
            policy,
            channel_id=parent.channel_id,
            thread_id=b.thread_id,
            category_id=None if unresolved else parent.category_id,
        )
        thread_bindings.append(
            BindingAnswer(
                thread_id=b.thread_id,
                parent_channel_id=b.parent_channel_id,
                agent=b.responder_name,
                kind=b.kind,
                parent_in_server=parent is not None,
                answers=answers,
            )
        )
        if answers:
            answerable.add(b.parent_channel_id)
    return Answerable(
        guild_id=guild_id,
        answerable=_sorted_ids(answerable),
        channels=sorted(channels, key=lambda c: (len(c.channel_id), c.channel_id)),
        thread_bindings=thread_bindings,
        errors=errors,
    )


def compare(found: Answerable, expected: set[str]) -> Comparison:
    answerable = set(found.answerable)
    unlisted = [
        b for b in found.thread_bindings if b.answers and b.parent_channel_id not in expected
    ]
    missing_from_expect = _sorted_ids(answerable - expected)
    missing_from_daimon = _sorted_ids(expected - answerable)
    return Comparison(
        match=not (missing_from_expect or missing_from_daimon or unlisted),
        missing_from_expect=missing_from_expect,
        missing_from_daimon=missing_from_daimon,
        unlisted_thread_bindings=unlisted,
    )


def read_expect_file(path: Path) -> set[str]:
    """One Discord channel id per line; `#` starts a comment, blank lines are skipped."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise typer.BadParameter(f"cannot read {path}: {exc.strerror}") from exc
    ids: set[str] = set()
    for number, line in enumerate(text.splitlines(), start=1):
        value = line.partition("#")[0].strip()
        if not value:
            continue
        if not value.isdigit():
            raise typer.BadParameter(f"{path}:{number}: {value!r} is not a Discord channel id")
        ids.add(value)
    return ids


async def _fetch_guild_channels(
    token: str, *, guild_id: str, transport: httpx.AsyncBaseTransport | None
) -> list[dict[str, Any]]:
    """Every channel in the server, threads excluded, as Discord returns it to the bot."""
    try:
        async with httpx.AsyncClient(
            base_url=_DISCORD_API,
            headers={"Authorization": f"Bot {token}"},
            timeout=10.0,
            transport=transport,
        ) as http:
            response = await http.get(f"/guilds/{guild_id}/channels")
    except httpx.HTTPError as exc:
        raise DaimonError(f"could not reach Discord to list server {guild_id}'s channels") from exc
    if response.status_code in (403, 404):
        raise DaimonError(f"Discord server {guild_id} is not visible to daimon")
    if not response.is_success:
        raise DaimonError(
            f"Discord returned HTTP {response.status_code} listing server {guild_id}'s channels"
        )
    body = response.json()
    if not isinstance(body, list):
        raise DaimonError(f"Discord returned no channel list for server {guild_id}")
    return cast("list[dict[str, Any]]", body)


def channels_answerable_command(
    platform: str,
    guild_id: str,
    expect: Annotated[
        Path | None,
        typer.Option(
            "--expect",
            help="A file of the channel ids the other bot does not answer, one per line "
            "(# comments): Daimon's answerable set must equal it; exit 1 on any difference.",
            dir_okay=False,
        ),
    ] = None,
    as_json: Annotated[bool, JSON_OPTION] = False,
) -> None:
    """List the Discord channels where Daimon may answer: those not under a writers none
    rule, theirs or their category's. Read-only. Exits 1 on an error or, with --expect,
    on any difference."""
    console = Console(highlight=False)
    # Bad arguments fail before the runtime opens the database.
    guild_id, expected = _check_arguments(platform, guild_id, expect)

    async def _run() -> None:
        async with build_runtime(load_settings()) as rt:
            await channels_answerable(
                rt=rt,
                console=console,
                platform=platform,
                guild_id=guild_id,
                expected=expected,
                as_json=as_json,
            )

    run_cli(_run(), console=console)


def _check_arguments(
    platform: str, guild_id: str, expect: Path | None
) -> tuple[str, set[str] | None]:
    """The server id and the expected ids, or a usage error."""
    if platform != "discord":
        raise typer.BadParameter("only discord: the check reads a Discord server's channels")
    guild_id = guild_id.strip()
    if not guild_id.isdigit():
        raise typer.BadParameter(f"{guild_id!r} is not a Discord server id")
    return guild_id, read_expect_file(expect) if expect is not None else None


async def channels_answerable(
    *,
    rt: CliRuntime,
    console: Console,
    platform: str,
    guild_id: str,
    expected: set[str] | None = None,
    as_json: bool = False,
    discord_transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    guild_id, _ = _check_arguments(platform, guild_id, None)
    if rt.settings.discord is None:
        raise DaimonError("DAIMON_DISCORD__BOT_TOKEN is not set; it is needed to list channels")
    guild_channels = await _fetch_guild_channels(
        rt.settings.discord.bot_token.get_secret_value(),
        guild_id=guild_id,
        transport=discord_transport,
    )
    # No tenant row yet means no policy and no bindings: everything is open.
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=guild_id)
    async with rt.sessionmaker() as session:
        policy = await load_access_policy(session, tenant_id=tenant_id)
        bindings = await list_answering_bindings(session, tenant_id=tenant_id, platform="discord")
    found = compute_answerable(
        policy, guild_id=guild_id, guild_channels=guild_channels, bindings=bindings
    )
    comparison = compare(found, expected) if expected is not None else None
    if as_json:
        payload: dict[str, Any] = found.model_dump()
        if comparison is not None:
            payload["comparison"] = comparison.model_dump()
        console.print(json.dumps(payload), soft_wrap=True, highlight=False, markup=False)
    else:
        _print_text(console, found, comparison)
    if found.errors or (comparison is not None and not comparison.match):
        raise typer.Exit(1)


def _print_text(console: Console, found: Answerable, comparison: Comparison | None) -> None:
    console.print(
        f"discord:{found.guild_id}: Daimon may answer in {len(found.answerable)} channel(s)",
        markup=False,
    )
    emit_rows(
        console,
        [c for c in found.channels if c.answers],
        columns=("channel_id", "name", "category_id", "why"),
        as_json=False,
    )
    silent = [c for c in found.channels if not c.answers]
    if silent:
        console.print(f"Silent in {len(silent)} channel(s):", markup=False)
        emit_rows(console, silent, columns=("channel_id", "name", "why"), as_json=False)
    if found.thread_bindings:
        console.print(f"Thread bindings ({len(found.thread_bindings)}):", markup=False)
        emit_rows(
            console,
            found.thread_bindings,
            columns=tuple(BindingAnswer.model_fields),
            as_json=False,
        )
    for error in found.errors:
        console.print(f"✗ {error}", style="red", markup=False)
    if comparison is None:
        return
    if comparison.match:
        console.print("✓ matches the expected list", markup=False)
        return
    console.print("✗ differs from the expected list", style="red", markup=False)
    console.print(
        "Daimon may answer, not in the file: "
        + (", ".join(comparison.missing_from_expect) or "none"),
        markup=False,
    )
    console.print(
        "In the file, Daimon may not answer: "
        + (", ".join(comparison.missing_from_daimon) or "none"),
        markup=False,
    )
    for b in comparison.unlisted_thread_bindings:
        console.print(
            f"Thread {b.thread_id} ({b.agent}) answers under unlisted channel "
            f"{b.parent_channel_id}",
            markup=False,
        )
