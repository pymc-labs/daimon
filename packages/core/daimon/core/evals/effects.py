"""Effects: the canonical meaning of a call, so expectations hold whichever tool an agent picks.

An effect verb is `service.resource[.sub].verb`. `KNOWN_VERBS` is the starting vocabulary;
unknown verbs are allowed and flagged. `canonical_calls` turns a run item into the calls
matching reads, with the suite's tool aliases applied.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from functools import cache
from typing import Final

from daimon.core.evals.format import CallPattern, Effect, FormatModel, RunItem, ToolAlias
from pydantic import JsonValue

KNOWN_VERBS: Final[frozenset[str]] = frozenset(
    f"{service}.{verb}"
    for service, verbs in {
        "github": (
            "repo.read", "issue.list", "issue.read", "issue.create", "issue.update",
            "issue.comment.create", "pull.list", "pull.read", "pull.create",
            "pull.review.create", "pull.merge", "content.read", "content.write",
            "workflow.dispatch", "release.create",
        ),
        "git": ("repo.clone", "repo.fetch", "repo.push"),
        "google": (
            "drive.file.list", "drive.file.read", "drive.file.create", "drive.file.share",
            "docs.document.update", "sheets.values.read", "sheets.values.update",
            "slides.presentation.create", "slides.presentation.update", "calendar.event.list",
            "calendar.event.create", "gmail.message.list", "gmail.message.send",
        ),
        "web": ("page.fetch", "search.query"),
        "chat": ("message.send", "message.edit", "reaction.add", "file.upload", "card.post"),
    }.items()
    for verb in verbs
)  # fmt: skip
_DAIMON_VERB = re.compile(r"^daimon\.[a-z0-9_]+\.call$")


def is_known_verb(verb: str) -> bool:
    """Whether `verb` is in the registry; `daimon.<tool>.call` covers every Daimon tool."""
    return verb in KNOWN_VERBS or bool(_DAIMON_VERB.match(verb))


class CanonicalCall(FormatModel):
    """One call as matching sees it: the tool after aliasing and the effects it had."""

    seq: int
    tool: str | None = None
    server: str | None = None
    service: str | None = None
    surface: str | None = None
    args: JsonValue = None
    effects: tuple[Effect, ...] = ()


@cache
def _glob(pattern: str) -> re.Pattern[str]:
    parts = re.split(r"(\*\*|\*)", pattern)
    body = "".join(".*" if p == "**" else "[^./]*" if p == "*" else re.escape(p) for p in parts)
    return re.compile(f"^{body}$")


def _hits(pattern: str | None, value: str | None) -> bool:
    return pattern is None or (value is not None and bool(_glob(pattern).match(value)))


def matches(pattern: CallPattern, call: CanonicalCall) -> bool:
    """Whether `call` fits `pattern`: every field set on the pattern glob-matches."""
    effect_hit = pattern.effect is None or any(_hits(pattern.effect, e.verb) for e in call.effects)
    return (
        effect_hit
        and _hits(pattern.tool, call.tool)
        and _hits(pattern.server, call.server)
        and _hits(pattern.service, call.service)
        and _hits(pattern.surface, call.surface)
    )


def _pointer_parts(pointer: str) -> list[str]:
    if pointer in ("", "/"):
        return []
    return [p.replace("~1", "/").replace("~0", "~") for p in pointer.lstrip("/").split("/")]


def pointer_get(document: JsonValue, pointer: str) -> tuple[bool, JsonValue]:
    """RFC 6901 lookup: (found, value)."""
    node: JsonValue = document
    for part in _pointer_parts(pointer):
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return False, None
    return True, node


def _pointer_set(document: dict[str, JsonValue], pointer: str, value: JsonValue) -> None:
    *parents, last = _pointer_parts(pointer) or [""]
    node = document
    for part in parents:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[last] = value


def _map_args(args: JsonValue, args_map: dict[str, str] | None) -> JsonValue:
    if not args_map:
        return args
    mapped: dict[str, JsonValue] = {}
    for source, target in args_map.items():
        found, value = pointer_get(args, source)
        if found:
            _pointer_set(mapped, target, value)
    return mapped


def apply_aliases(call: CanonicalCall, aliases: Sequence[ToolAlias]) -> CanonicalCall:
    """The call under the first alias whose `from` matches it; unchanged when none does."""
    for alias in aliases:
        if not matches(alias.from_, call):
            continue
        args = _map_args(call.args, alias.args_map)
        update: dict[str, object] = {"args": args}
        if alias.to_tool is not None:
            update |= {"tool": alias.to_tool.tool, "server": alias.to_tool.server}
        if alias.to_effect is not None:
            mapped = args if isinstance(args, dict) else None
            update["effects"] = (*call.effects, Effect(verb=alias.to_effect, args=mapped))
        return call.model_copy(update=update)
    return call


def canonical_calls(item: RunItem, aliases: Iterable[ToolAlias] = ()) -> list[CanonicalCall]:
    """Every call an item made, with world-call effects linked and aliases applied.

    Tool calls come from the transcript; a tool result naming a `world_call` seq lends that
    call's service and effects to its tool call. World calls no tool call claimed (HTTP from a
    CLI, git) follow as calls of their own, in seq order.
    """
    if item.transcript is not None and item.transcript.events is None and item.transcript.blob:
        raise ValueError("load the transcript blob into `transcript.events` first")
    events = item.transcript.events or [] if item.transcript else []
    world = {w.seq: w for w in item.world_calls or []}
    results = {e.call_id: e.result for e in events if e.kind == "tool_result" and e.result}
    claimed: set[int] = set()
    calls: list[CanonicalCall] = []
    for event in events:
        if event.kind != "tool_call" or event.tool is None:
            continue
        result = results.get(event.call_id)
        linked = world.get(result.world_call) if result and result.world_call is not None else None
        if linked is not None:
            claimed.add(linked.seq)
        calls.append(
            CanonicalCall(
                seq=event.seq,
                tool=event.tool.name,
                server=event.tool.server,
                service=linked.service if linked else None,
                surface=event.tool.surface,
                args=event.tool.input,
                effects=tuple(linked.effects or ()) if linked else (),
            )
        )
    for seq, call in sorted(world.items()):
        if seq in claimed:
            continue
        args = call.request.args if call.request else None
        calls.append(
            CanonicalCall(
                seq=seq, tool=call.tool, server=call.server, service=call.service,
                surface=call.surface, args=args, effects=tuple(call.effects or ()),
            )
        )  # fmt: skip
    alias_list = list(aliases)
    return [apply_aliases(c, alias_list) for c in calls]
