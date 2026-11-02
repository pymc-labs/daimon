"""Words for the in-progress turn card, shared by the chat adapters.

Discord, Slack and Teams all draw a headline, the last few draft lines, the
progress plan and a block of tool lines from the same `TurnState`, in the
order `CARD_SECTIONS` gives; this module owns the words and that order so the
cards cannot drift, and each adapter adds only its own markup.

A tool line names the tool. Its arguments stay off the card, because they can
hold anything a model or a person typed, keys included. The one exception is
a short allowlist of the sandbox's own built-in tools, each showing one field
that says where it looked and cannot carry a secret on its own: the search
query, the web page's host name, the file's base name (`_DETAILS`).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal, cast
from urllib.parse import urlsplit

from daimon.core.turn.outcomes import current_outcome
from daimon.core.turn.state import ContentBlock, ToolUseBlock, daimon_tool_input
from mux.profiles import get_profile

MAX_TOOL_LINES = 6
DRAFT_LOG_LINES = 3
# Three draft lines share the card, so each is cut short.
DRAFT_LINE_MAX_CHARS = 120
MAX_PLAN_LINES = 6
PLAN_TOOL = "update_plan"
_LABEL_MAX_CHARS = 48
_DETAIL_MAX_CHARS = 40
_PLAN_STEP_MAX_CHARS = 60
# Em spaces: chat clients collapse runs of plain spaces but keep these.
SUMMARY_GAP = "\u2003\u2003"

# ☑️ and ⏳ rather than ✔️ and 🖋️: those two are dark glyphs that all but
# vanish on a dark theme.
_DONE_ICON = "☑️"
_FAILED_ICON = "🚫"
_WRITE_ICON = "⏳"
_LIST_ICON = "📜"
_LOOKUP_ICON = "🔍"

PlanStatus = Literal["pending", "in_progress", "completed"]
# The card's own words: done as a finished tool, in progress as a running one.
# Plan lines are drawn in code like the tool lines, since a chat client draws
# an emoji differently in code and in text. A pending step has no icon; three
# figure spaces hold its text in line with the steps that have one (in a
# monospace font an emoji takes two cells and its space one), no client
# collapses them, and they keep a leading "-" or "1." from becoming a list.
_PLAN_PENDING = "\u2007\u2007\u2007"
_PLAN_ICONS: Mapping[PlanStatus, str] = {
    "completed": f"{_DONE_ICON} ",
    "in_progress": f"{_WRITE_ICON} ",
    "pending": _PLAN_PENDING,
}


CardSection = Literal["updates", "plan", "tools"]
# Under the headline, top down: the agent's latest updates (the draft lines)
# first, as what a person reads for, then the plan, then the tool lines.
CARD_SECTIONS: tuple[CardSection, ...] = ("updates", "plan", "tools")
# The plan and the tool lines are drawn alike: a small header, then each line in code.
LabelledSection = Literal["plan", "tools"]
SECTION_LABELS: Mapping[LabelledSection, str] = {"plan": "Plan", "tools": "Details"}


def in_card_order[T](sections: Mapping[CardSection, T | None]) -> list[T]:
    """An adapter's drawn sections in `CARD_SECTIONS` order; a missing or None one drops out."""
    return [part for name in CARD_SECTIONS if (part := sections.get(name)) is not None]


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
_HOST_NAME = re.compile(r"[a-z0-9.-]{1,253}")


def _query_detail(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _host_detail(value: object) -> str | None:
    """The host alone: a URL's path, query and user info can carry a token."""
    if not isinstance(value, str):
        return None
    try:
        host = urlsplit(value.strip()).hostname
    except ValueError:
        return None
    return host if host is not None and _HOST_NAME.fullmatch(host) else None


def _base_name_detail(value: object) -> str | None:
    """The base name alone: the directories above it say nothing the person needs."""
    if not isinstance(value, str):
        return None
    return PurePosixPath(value.replace("\\", "/")).name or None


# Built-in tools whose line may show one field of their input, and how to read it.
_DETAILS: Mapping[str, tuple[tuple[str, ...], Callable[[object], str | None]]] = {
    "web_search": (("query",), _query_detail),
    "web_fetch": (("url",), _host_detail),
    "read": (("file_path", "path"), _base_name_detail),
    "write": (("file_path", "path"), _base_name_detail),
    "edit": (("file_path", "path"), _base_name_detail),
}


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


def backend_footer_text() -> str | None:
    """The current admitted backend, with no default or provider text reflection."""
    observation = current_outcome.get()
    if observation is None or observation.backend_profile is None:
        return None
    return get_profile(observation.backend_profile).provider


def format_summary(
    *, agent_name: str | None, elapsed_seconds: float, cost: str | None, left: str | None
) -> str:
    """The finished card's one line: ``Ada  12s  $0.042 used  $41.20 left``.

    Fields are set apart by space alone, never a dot or a bar; a missing one
    drops out with its gap.
    """
    fields = [
        agent_name or None,
        format_duration(elapsed_seconds),
        cost
        if cost in ("Cost unknown", "Cost partial")
        else f"{cost} used"
        if cost is not None
        else None,
        left,
        backend_footer_text(),
    ]
    return SUMMARY_GAP.join(field for field in fields if field)


def format_headline(
    *, is_working: bool, elapsed_seconds: float | None, bold: Callable[[str], str]
) -> str:
    """One short working label; timing belongs in card details."""
    del is_working, elapsed_seconds
    return bold("Working on it…")


def format_draft(text: str) -> str:
    """The draft on one line, cut at ``DRAFT_LINE_MAX_CHARS``, so one quote marker covers it."""
    return _one_line(text, DRAFT_LINE_MAX_CHARS)


def roll_draft(earlier: Sequence[str], latest: str | None, new: str) -> tuple[tuple[str, ...], str]:
    """Add ``new`` to the draft log: ``(earlier lines, latest line)``.

    The log keeps the last ``DRAFT_LOG_LINES`` lines, so at most that many
    including ``new``; a line equal to the latest one is not added twice.
    """
    if new == latest:
        return tuple(earlier), new
    lines = (*earlier, latest) if latest else tuple(earlier)
    return lines[-(DRAFT_LOG_LINES - 1) :], new


def draft_log(earlier: Sequence[str], latest: str | None) -> tuple[str, ...]:
    """The lines a card shows first under its headline, oldest first."""
    return (*earlier, latest) if latest else tuple(earlier)


def has_running_tool(content: Sequence[ContentBlock]) -> bool:
    """Whether any tool call in the turn is still waiting on its result."""
    return any(isinstance(block, ToolUseBlock) and block.status == "pending" for block in content)


def format_tool_lines(
    content: Sequence[ContentBlock], *, finished_ids: Sequence[str] = (), plan_shown: bool = False
) -> tuple[str, ...]:
    """Finished calls, then running ones, within MAX_TOOL_LINES; running ones win.

    Finished calls read in the order they finished, per ``finished_ids``; any
    it does not list come first, in call order. Older calls fold into one
    ``+N earlier`` line on top. Lines carry no backtick, so an adapter can
    fence them in a code block as they are. With ``plan_shown``, plan updates
    are drawn as the plan (`format_plan_lines`), so they get no line here.
    """
    calls = [
        block
        for block in content
        if isinstance(block, ToolUseBlock) and not (plan_shown and _is_plan_call(block))
    ]
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
        if (detail := _built_in_detail(call)) is not None:
            label = f"{label}: {detail}"
    else:
        icon, label = _humanize(call.name)
        label = _clip(label)
        if call.mcp_server_name:
            label = f"{label} ({call.mcp_server_name})"
    if is_finished:
        icon = _FAILED_ICON if call.status == "failed" else _DONE_ICON
    return f"{icon} {label}"


def tool_label(call: ToolUseBlock) -> tuple[str, str | None]:
    """A call's label and its detail, without icons, for a surface that draws its own."""
    narration = _BUILT_IN.get(call.name) if call.type == "agent.tool_use" else None
    if narration is not None:
        label = narration.finished if call.status != "pending" else narration.running
        return label, _built_in_detail(call)
    return _clip(_humanize(call.name)[1]), call.mcp_server_name


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


def _one_line(text: str, limit: int) -> str:
    """Printable characters on one line, cut at ``limit`` with an ellipsis."""
    flat = " ".join("".join(ch if ch.isprintable() else " " for ch in text).split())
    return flat[:limit] + "…" if len(flat) > limit else flat


def _built_in_detail(call: ToolUseBlock) -> str | None:
    """The one allowlisted field of a built-in call, on one short line, or None."""
    entry = _DETAILS.get(call.name)
    if entry is None:
        return None
    keys, read = entry
    value = next((call.input[key] for key in keys if key in call.input), None)
    detail = read(value)
    if detail is None:
        return None
    return _one_line(detail, _DETAIL_MAX_CHARS) or None


@dataclass(frozen=True, slots=True)
class PlanStep:
    step: str
    status: PlanStatus


def _is_plan_call(block: ToolUseBlock) -> bool:
    return daimon_tool_input(block, PLAN_TOOL) is not None


def plan_steps(content: Sequence[ContentBlock]) -> tuple[PlanStep, ...]:
    """The steps of the turn's latest plan update; a malformed step is left out."""
    for block in reversed(content):
        if not isinstance(block, ToolUseBlock):
            continue
        arguments = daimon_tool_input(block, PLAN_TOOL)
        if arguments is None:
            continue
        raw: object = arguments.get("steps")
        if not isinstance(raw, list):
            return ()
        steps: list[PlanStep] = []
        for item in raw:  # pyright: ignore[reportUnknownVariableType]  # model-written JSON
            if not isinstance(item, dict):
                continue
            entry = cast("dict[str, object]", item)
            text, status = entry.get("step"), entry.get("status")
            if isinstance(text, str) and text.strip() and status in _PLAN_ICONS:
                steps.append(PlanStep(text, cast("PlanStatus", status)))
        return tuple(steps)
    return ()


def format_plan_lines(content: Sequence[ContentBlock]) -> tuple[str, ...]:
    """The latest plan as a checklist within MAX_PLAN_LINES, oldest step first.

    Finished steps at the top fold into one ``N done`` line first, so the step
    in progress stays in view; a plan still too long ends in ``+N more``. Lines
    carry no backtick, so an adapter fences each in code, like the tool lines.
    """
    steps = list(plan_steps(content))
    folded = 0
    while len(steps) + (1 if folded else 0) > MAX_PLAN_LINES and steps[0].status == "completed":
        steps.pop(0)
        folded += 1
    lines = [f"{_DONE_ICON} {folded} done"] if folded else []
    lines += [
        f"{_PLAN_ICONS[step.status]}{_one_line(step.step, _PLAN_STEP_MAX_CHARS)}" for step in steps
    ]
    if len(lines) > MAX_PLAN_LINES:
        hidden = len(lines) - (MAX_PLAN_LINES - 1)
        lines = [*lines[: MAX_PLAN_LINES - 1], f"{_PLAN_PENDING}+{hidden} more"]
    return tuple(line.replace("`", "'") for line in lines)
