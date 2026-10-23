"""Container builders for the /routines panel — V2 Components format.

Accent rule: ``derive_state`` returns ``(glyph, color_int)``.
The container carries ``accent_colour=color_int`` ONLY when
``color_int != theme.COLOR_BLURPLE``. Blurple-pending ("never run")
gets *no* accent per the F5 design-language "default = no accent" rule.
Non-default states — paused (yellow), errored (red), success (green) —
carry their color as the left-edge bar.
"""

from __future__ import annotations

from datetime import datetime

from daimon.adapters.discord import layout, theme
from daimon.adapters.discord.routines_panel.state import (
    Glyph,
    RoutinesPanelState,
    state_label,
)
from daimon.core.cron_words import schedule_words

import discord

ADMIN_ONLY = "Only a server admin can create routines."
INVALID_SCHEDULE = "That schedule or time zone isn't valid.\n\nCheck both and try again."
AGENT_UNAVAILABLE = "No agent with that name exists on this server."
SAVE_REFUSED = (
    "This agent's rules do not allow a routine without a channel destination. Nothing was saved."
)
GONE = "This routine no longer exists."
WRONG_TENANT = "This routine is from another server."
DELETE_REFUSED = "Only its creator or a server admin can delete it."
DELETE_CONFIRM = "Delete this routine? This cannot be undone."
DELETED = "Routine deleted."
CANCELLED = "Deletion cancelled."
FIELDS_REQUIRED = "Agent and message are required."
CONFIRM_USER_ONLY = "Only the person who opened this can use it."


def created_message(agent_name: str, cron_expr: str, timezone: str) -> str:
    return f"Routine created for {agent_name}, {schedule_words(cron_expr, timezone)}."


_EMPTY_HINT = (
    "No routines yet.\n\nServer admins can use New, or ask your agent: "
    '"Schedule a daily summary at 9am."'
)


def _humanize_delta(target: datetime, now: datetime) -> str:
    """Compact relative-time label. Positive = future, negative = past."""
    delta = target - now
    total_seconds = int(delta.total_seconds())
    sign = 1 if total_seconds >= 0 else -1
    seconds = abs(total_seconds)
    suffix = "from now" if sign > 0 else "ago"
    if seconds < 60:
        return f"{seconds}s {suffix}"
    if seconds < 3600:
        count = seconds // 60
        return f"{count} minute{'s' if count != 1 else ''} {suffix}"
    if seconds < 86400:
        count = seconds // 3600
        return f"{count} hour{'s' if count != 1 else ''} {suffix}"
    count = seconds // 86400
    return f"{count} day{'s' if count != 1 else ''} {suffix}"


def build_panel_container(
    state: RoutinesPanelState, *, now: datetime
) -> discord.ui.Container[discord.ui.LayoutView]:
    """Timeline-forward container for the /routines panel.

    The header shows status, agent and schedule. The body shows the next run
    and, when present, when the last run started.

    Accent rule (F5 "default = no accent"):
    ``color_int != theme.COLOR_BLURPLE`` → ``accent_colour=color_int``
    blurple-pending state → no accent (``accent_colour`` omitted / None).
    """
    if state.selected is None:
        # Empty roster branch: minimal container with a dim hint line.
        return discord.ui.Container(
            layout.header("📜 Routines"),
            layout.hairline(),
            discord.ui.TextDisplay(_EMPTY_HINT),
        )

    selected = state.selected
    glyph: Glyph = selected.glyph
    color_int: int = selected.color
    trigger = selected.routine.trigger_message[:40] or selected.routine.id.hex[:8]

    subtext = (
        f"{glyph} {state_label(glyph)}\n\n{selected.agent_name}, "
        f"{schedule_words(selected.routine.cron_expr, selected.routine.timezone)}"
    )

    # Timeline body.
    if selected.routine.next_fire_at is None:
        next_line = "Next run: not scheduled"
    else:
        next_at = selected.routine.next_fire_at
        next_line = f"Next run: {next_at.strftime('%b')} {next_at.day} at {next_at:%H:%M} UTC"

    if selected.routine.last_fired_at is not None:
        last_delta = _humanize_delta(selected.routine.last_fired_at, now)
        last_line = f"-# Last started {last_delta}"
        if selected.routine.last_error is not None:
            last_line += "\n\nError recorded"
        timeline_body = f"{next_line}\n\n{last_line}"
    else:
        timeline_body = next_line

    routine = selected.routine
    destination = (
        "Owner DM"
        if routine.destination_kind == "dm"
        else (routine.destination_id or "No automatic delivery")
    )
    timeline_body += f"\n\nDelivery: {destination}"
    if routine.delivery_status is not None:
        timeline_body += f"\nStatus: {routine.delivery_status}"
    if routine.delivery_note is not None:
        timeline_body += "\n" + routine.delivery_note.replace("_", " ")
    # An admin's per-routine opt-in; off for every routine unless set.
    if routine.destination_kind == "dm":
        timeline_body += "\n\nSandbox tools: off (DM routines can't use sandbox tools)"
    elif routine.sandbox_tools_enabled:
        timeline_body += "\n\nSandbox tools: on (admin opt-in)"
    else:
        timeline_body += "\n\nSandbox tools: off"

    # Accent: only non-blurple states carry the color bar
    accent: int | None = color_int if color_int != theme.COLOR_BLURPLE else None

    return discord.ui.Container(
        discord.ui.TextDisplay(f"## 📜 {trigger}\n\n-# {subtext}"),
        layout.hairline(),
        discord.ui.TextDisplay(timeline_body),
        accent_colour=accent,
    )
