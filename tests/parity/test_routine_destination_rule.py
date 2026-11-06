"""Rule contract: every routine destination check decides with the shared core rule.

A routine posts on its creator's behalf, so the chat tool that saves it, the
web app that saves it and the delivery that posts it must agree on where the
creator may post. The rule lives in core (`routine_delivery.discord_creator_may_post`,
`routine_delivery.slack_creator_may_post`, `routine_destinations`); each surface
supplies the platform facts it read. A surface with its own copy of the rule
drifts from the others without a failing test, so this walks each surface's
code and fails when it stops reaching the rule.
"""

from __future__ import annotations

import ast
import itertools
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.discord import routine_delivery as discord_delivery
from daimon.adapters.mcp.tools import routines as mcp_routines
from daimon.adapters.slack import routine_delivery as slack_delivery
from daimon.core.routine_delivery import discord_creator_may_post
from daimon.core.routine_destinations import slack_may_deliver
from fastmcp.exceptions import ToolError

_ROOT = Path(__file__).resolve().parents[2]
_ADAPTERS = _ROOT / "packages" / "adapters"

#: The core rule functions a destination check must reach.
_RULES = frozenset(
    {
        "discord_creator_may_post",
        "slack_creator_may_post",
        "slack_may_deliver",
        "discord_deliverable_channels",
    }
)
#: Calls that read the facts the rule decides on.
_FACT_READS = frozenset({"permissions_for", "conversations_info", "conversations_members"})

#: (module, function): each surface's destination check.
_SURFACES = (
    ("mcp/daimon/adapters/mcp/tools/routines.py", "_resolve_discord_destination"),
    ("mcp/daimon/adapters/mcp/tools/routines.py", "_resolve_slack_destination"),
    ("discord/daimon/adapters/discord/routine_delivery.py", "creator_may_post"),
    ("slack/daimon/adapters/slack/routine_delivery.py", "_destination_refusal"),
    ("web/daimon/adapters/web/channel_visibility.py", "_discord"),
    ("web/daimon/adapters/web/channel_visibility.py", "_slack"),
)


def _call_name(node: ast.Call) -> str | None:
    target = node.func
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    return None


def _call_graph(source: str) -> dict[str, frozenset[str]]:
    """Each module-level function's called names."""
    graph: dict[str, frozenset[str]] = {}
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            graph[node.name] = frozenset(
                name
                for call in ast.walk(node)
                if isinstance(call, ast.Call) and (name := _call_name(call)) is not None
            )
    return graph


def _reaches_rule(function: str, graph: dict[str, frozenset[str]]) -> bool:
    seen: set[str] = set()
    pending = [function]
    while pending:
        name = pending.pop()
        if name in seen or name not in graph:
            continue
        seen.add(name)
        if graph[name] & _RULES:
            return True
        pending.extend(graph[name])
    return False


def _called_from(roots: set[str], graph: dict[str, frozenset[str]]) -> set[str]:
    seen: set[str] = set()
    pending = [callee for root in roots for callee in graph[root]]
    while pending:
        name = pending.pop()
        if name in seen or name not in graph:
            continue
        seen.add(name)
        pending.extend(graph[name])
    return seen


@pytest.mark.parametrize(("module", "function"), _SURFACES)
def test_each_surface_decides_a_destination_with_the_core_rule(module: str, function: str) -> None:
    graph = _call_graph((_ADAPTERS / module).read_text(encoding="utf-8"))
    assert function in graph, f"{module} no longer defines {function}: update _SURFACES"
    assert _reaches_rule(function, graph), (
        f"{module}:{function} decides where a routine may post without the core rule "
        f"({', '.join(sorted(_RULES))}); supply the facts to it instead of a local copy"
    )


def test_every_routine_module_fact_read_reaches_the_rule() -> None:
    """A new check in a routine module that reads posting facts must use the rule too."""
    modules = sorted(_ADAPTERS.glob("*/daimon/adapters/*/**/routine*.py"))
    assert modules, "the walk found no routine module"
    offenders = []
    for path in modules:
        graph = _call_graph(path.read_text(encoding="utf-8"))
        deciders = {name for name in graph if _reaches_rule(name, graph)}
        # A helper that only reads facts for a decider is part of that decider.
        helpers = _called_from(deciders, graph)
        offenders.extend(
            f"{path.relative_to(_ADAPTERS)}:{name}"
            for name, calls in graph.items()
            if calls & _FACT_READS and name not in deciders | helpers
        )
    assert offenders == [], f"routine destination facts read without the core rule: {offenders}"


def test_the_contract_flags_a_local_copy_of_the_rule() -> None:
    source = (
        "async def _resolve_slack_destination(client, channel_id):\n"
        "    info = await client.conversations_info(channel=channel_id)\n"
        "    return bool(info['channel'].get('is_member'))\n"
        "def _decide(perms):\n"
        "    return discord_creator_may_post(administrator=perms.administrator)\n"
        "async def _resolve_discord_destination(channel, member):\n"
        "    return _decide(channel.permissions_for(member))\n"
    )
    graph = _call_graph(source)
    assert not _reaches_rule("_resolve_slack_destination", graph)
    assert _reaches_rule("_resolve_discord_destination", graph)


# ----- Behaviour: each surface's verdict equals the core rule's, for every fact -----

_SLACK_FACTS = (
    "bot_is_member",
    "is_archived",
    "is_im_or_mpim",
    "is_private",
    "is_guest",
    "caller_is_member",
)
_DISCORD_FACTS = (
    "administrator",
    "view_channel",
    "send_messages",
    "is_thread",
    "send_messages_in_threads",
    "is_private_thread",
    "manage_threads",
    "is_thread_member",
)


def _slack_client(facts: dict[str, bool]) -> MagicMock:
    client = MagicMock()
    client.conversations_info = AsyncMock(
        return_value={
            "channel": {
                "id": "C1",
                "is_member": facts["bot_is_member"],
                "is_archived": facts["is_archived"],
                "is_im": facts["is_im_or_mpim"],
                "is_private": facts["is_private"],
            }
        }
    )
    client.users_info = AsyncMock(
        return_value={"user": {"id": "U1", "is_restricted": facts["is_guest"]}}
    )
    client.conversations_members = AsyncMock(
        return_value={"members": ["U1"] if facts["caller_is_member"] else ["U2"]}
    )
    return client


@pytest.mark.parametrize("values", list(itertools.product((False, True), repeat=len(_SLACK_FACTS))))
async def test_slack_save_and_delivery_agree_with_the_rule(
    values: tuple[bool, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    facts = dict(zip(_SLACK_FACTS, values, strict=True))
    expected = slack_may_deliver(**facts)
    client = _slack_client(facts)

    async def web_client(*_args: Any, **_kwargs: Any) -> MagicMock:
        return client

    monkeypatch.setattr(mcp_routines, "slack_web_client", web_client)
    auth = cast(Any, SimpleNamespace(external_id="T1", platform_user_id="U1"))
    try:
        await mcp_routines._resolve_slack_destination(  # pyright: ignore[reportPrivateUsage]
            cast(Any, None), auth, kind="channel", destination_id="C1"
        )
        saved = True
    except ToolError:
        saved = False
    refusal = await slack_delivery._destination_refusal(  # pyright: ignore[reportPrivateUsage]
        client,
        cast(Any, SimpleNamespace(created_by_user_id="U1")),
        cast(Any, SimpleNamespace(channel_id="C1", thread_ts=None)),
    )
    assert (saved, refusal is None) == (expected, expected), (
        f"chat tool saves={saved}, delivery posts={refusal is None}, rule={expected}: {facts}"
    )


def _discord_place(facts: dict[str, bool]) -> tuple[MagicMock, MagicMock, MagicMock]:
    """(channel, permission source, member) carrying `facts`."""
    perms = SimpleNamespace(
        view_channel=facts["view_channel"],
        send_messages=facts["send_messages"],
        send_messages_in_threads=facts["send_messages_in_threads"],
        manage_threads=facts["manage_threads"],
    )
    member = MagicMock(spec=discord.Member)
    member.guild_permissions = SimpleNamespace(administrator=facts["administrator"])
    if not facts["is_thread"]:
        channel = MagicMock(spec=discord.TextChannel)
        channel.permissions_for = MagicMock(return_value=perms)
        source = channel
    else:
        source = MagicMock(spec=discord.TextChannel)
        source.permissions_for = MagicMock(return_value=perms)
        channel = MagicMock(spec=discord.Thread)
        channel.parent = source
        channel.type = (
            discord.ChannelType.private_thread
            if facts["is_private_thread"]
            else discord.ChannelType.public_thread
        )
        not_found = discord.NotFound(MagicMock(status=404, reason="Not Found"), "no member")
        channel.fetch_member = AsyncMock(
            return_value=member if facts["is_thread_member"] else None,
            side_effect=None if facts["is_thread_member"] else not_found,
        )
    channel.guild = MagicMock()
    channel.guild.get_member = MagicMock(return_value=member)
    return channel, source, member


@pytest.mark.parametrize(
    "values", list(itertools.product((False, True), repeat=len(_DISCORD_FACTS)))
)
async def test_discord_save_and_delivery_agree_with_the_rule(values: tuple[bool, ...]) -> None:
    facts = dict(zip(_DISCORD_FACTS, values, strict=True))
    if not facts["is_thread"] and (facts["is_private_thread"] or facts["is_thread_member"]):
        pytest.skip("thread facts do not apply to a channel")
    expected = discord_creator_may_post(**facts)
    channel, source, member = _discord_place(facts)
    saved = await mcp_routines._discord_caller_may_post(  # pyright: ignore[reportPrivateUsage]
        channel, member, "1"
    )
    posted = await discord_delivery.creator_may_post(channel, source, 1)
    assert (saved, posted) == (expected, expected), (
        f"chat tool saves={saved}, delivery posts={posted}, rule={expected}: {facts}"
    )
