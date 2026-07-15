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

import discord

_EMPTY_HINT = (
    "_No routines yet._ Ask your default agent to create a routine, e.g. "
    "_'schedule a daily 9am stand-up summary'_."
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
        return f"{seconds // 60}m {suffix}"
    if seconds < 86400:
        return f"{seconds // 3600}h {suffix}"
    return f"{seconds // 86400}d {suffix}"


def build_panel_container(
    state: RoutinesPanelState, *, now: datetime
) -> discord.ui.Container[discord.ui.LayoutView]:
    """R3 timeline-forward container for the /routines panel.

    Structure:
    - ``## 📜 {trigger_message}`` header with
      ``-# {glyph} {state} · {agent} · {cron} · {tz}`` subtext
      (cron/agent demoted to subtext, NOT body groups)
    - hairline separator
    - one TextDisplay timeline group:
      ``⏱ **Next run in {delta}** — {local time}``
      with ``-# last run {delta} · {result glyph}`` beneath when prior
      run data exists (omitted for never-run routines)

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
        f"{glyph} {state_label(glyph)} · {selected.agent_name} · "
        f"{selected.routine.cron_expr} · {selected.routine.timezone}"
    )

    # Timeline body — two _humanize_delta calls (next run + last run)
    if selected.routine.next_fire_at is None:
        next_line = "⏱ **Next run** — not scheduled"
    else:
        next_delta = _humanize_delta(selected.routine.next_fire_at, now)
        local_ts = selected.routine.next_fire_at.strftime("%Y-%m-%d %H:%M %Z").strip()
        next_line = f"⏱ **Next run in {next_delta}** — {local_ts}"

    if selected.routine.last_fired_at is not None:
        last_delta = _humanize_delta(selected.routine.last_fired_at, now)
        result_glyph = "❌" if selected.routine.last_error is not None else "✅"
        last_line = f"-# last run {last_delta} · {result_glyph}"
        timeline_body = f"{next_line}\n{last_line}"
    else:
        timeline_body = next_line

    # Accent: only non-blurple states carry the color bar
    accent: int | None = color_int if color_int != theme.COLOR_BLURPLE else None

    return discord.ui.Container(
        layout.header(f"📜 {trigger}", subtext=subtext),
        layout.hairline(),
        discord.ui.TextDisplay(timeline_body),
        accent_colour=accent,
    )
