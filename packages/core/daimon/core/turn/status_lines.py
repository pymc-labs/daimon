"""Words for the in-progress turn card, shared by the chat adapters.

Pure. Discord and Slack both draw a headline, a code block of tool lines and
the latest draft from the same `TurnState`; this module owns the words so the
two cards cannot drift, and each adapter adds only its own markup. Per T-13-01
a tool line names the tool and never shows its arguments.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from daimon.core.turn.state import ContentBlock, ToolUseBlock

MAX_TOOL_LINES = 6
DRAFT_MAX_CHARS = 300
_LABEL_MAX_CHARS = 48

_DONE_ICON = "✔️"
_FAILED_ICON = "🚫"
_WRITE_ICON = "🖋️"
_LIST_ICON = "📜"
_LOOKUP_ICON = "🔍"


@dataclass(frozen=True, slots=True)
class _Narration:
    icon: str
    running: str
    finished: str


# The sandbox toolset's built-in tools, by the lowercase name Managed Agents reports.
_BUILT_IN: Mapping[str, _Narration] = {
    "bash": _Narration(_WRITE_ICON, "Running a command", "Ran a command"),
    "read": _Narration(_LOOKUP_ICON, "Reading a file", "Read a file"),
    "write": _Narration(_WRITE_ICON, "Writing a file", "Wrote a file"),
    "edit": _Narration(_WRITE_ICON, "Editing a file", "Edited a file"),
    "glob": _Narration(_LIST_ICON, "Listing files", "Listed files"),
    "grep": _Narration(_LOOKUP_ICON, "Searching files", "Searched files"),
    "web_fetch": _Narration(_LOOKUP_ICON, "Fetching a web page", "Fetched a web page"),
    "web_search": _Narration(_LOOKUP_ICON, "Searching the web", "Searched the web"),
}
# Any other tool's icon, by the first of these verbs in its name; else it writes.
_VERB_ICONS: Mapping[str, str] = {
    "discover": _LIST_ICON,
    "list": _LIST_ICON,
    "search": _LOOKUP_ICON,
    "get": _LOOKUP_ICON,
    "query": _LOOKUP_ICON,
    "read": _LOOKUP_ICON,
    "fetch": _LOOKUP_ICON,
    "view": _LOOKUP_ICON,
}
_WORD_BREAK = re.compile(r"[\s_.-]+|(?<=[a-z0-9])(?=[A-Z])")


def format_duration(seconds: float) -> str:
    """``12s``, ``1m 5s`` or ``2h 3m``; negative durations clamp to zero."""
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def format_headline(
    *, is_working: bool, elapsed_seconds: float | None, bold: Callable[[str], str]
) -> str:
    """``Thinking · 12s`` or ``Working · 1m 5s``, the word bolded by the adapter."""
    label = bold("Working" if is_working else "Thinking")
    return label if elapsed_seconds is None else f"{label} · {format_duration(elapsed_seconds)}"


def format_draft(text: str) -> str:
    """The draft on one line, clipped, so a single quote marker covers all of it."""
    flat = " ".join(text.split())
    return flat[:DRAFT_MAX_CHARS] + "…" if len(flat) > DRAFT_MAX_CHARS else flat


def has_running_tool(content: Sequence[ContentBlock]) -> bool:
    """Whether any tool call in the turn is still waiting on its result."""
    return any(isinstance(block, ToolUseBlock) and block.status == "pending" for block in content)


def format_tool_lines(
    content: Sequence[ContentBlock], *, finished_ids: Sequence[str] = ()
) -> tuple[str, ...]:
    """Finished calls, then running ones, within MAX_TOOL_LINES; running ones win.

    Finished calls read in the order they finished, per ``finished_ids``; any
    it does not list come first, in call order. Older calls fold into one
    ``+N earlier`` line on top. Lines carry no backtick, so an adapter can
    fence them in a code block as they are.
    """
    calls = [block for block in content if isinstance(block, ToolUseBlock)]
    running = [call for call in calls if call.status == "pending"][-MAX_TOOL_LINES:]
    budget = MAX_TOOL_LINES - len(running)
    finish_order = {tool_id: i for i, tool_id in enumerate(finished_ids)}
    finished = sorted(
        (call for call in calls if call.status != "pending"),
        key=lambda call: finish_order.get(call.id, -1),
    )
    finished = finished[-budget:] if budget else []
    hidden = len(calls) - len(running) - len(finished)
    lines = [f"+{hidden} earlier"] if hidden else []
    lines += [_tool_line(call) for call in (*finished, *running)]
    return tuple(line.replace("`", "'") for line in lines)


def _tool_line(call: ToolUseBlock) -> str:
    is_finished = call.status != "pending"
    narration = _BUILT_IN.get(call.name) if call.type == "agent.tool_use" else None
    if narration is not None:
        icon = narration.icon
        label = narration.finished if is_finished else narration.running
    else:
        icon, label = _humanize(call.name)
        label = _clip(label)
        if call.mcp_server_name:
            label = f"{label} ({call.mcp_server_name})"
    if is_finished:
        icon = _FAILED_ICON if call.status == "failed" else _DONE_ICON
    return f"{icon} {label}"


def _humanize(name: str) -> tuple[str, str]:
    """``search_issues`` -> the lookup icon and ``Search issues``."""
    words = [word for word in _WORD_BREAK.split(name) if word] or [name]
    icon = next(
        (_VERB_ICONS[word.lower()] for word in words if word.lower() in _VERB_ICONS), _WRITE_ICON
    )
    # Lowercase a camelCase word's capital, but leave acronyms such as SQL alone.
    words = [word.lower() if word.istitle() else word for word in words]
    words[0] = words[0][:1].upper() + words[0][1:]
    return icon, " ".join(words)


def _clip(text: str) -> str:
    return text if len(text) <= _LABEL_MAX_CHARS else text[: _LABEL_MAX_CHARS - 1] + "…"
