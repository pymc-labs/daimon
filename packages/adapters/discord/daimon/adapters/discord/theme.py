"""Shared Discord embed palette. Stdlib-only — no discord or adapter imports."""

from __future__ import annotations

COLOR_BLURPLE = 0x5865F2  # Discord blurple — neutral / informational default
COLOR_RED = 0xED4245  # Discord red — destructive / error
COLOR_GREEN = 0x57F287  # Discord green — success / complete
COLOR_GREYPLE = 0x99AAB5  # Discord greyple — neutral-empty / no-data
COLOR_THINKING = 0x95A5A6  # Turn — thinking phase (turn only)
COLOR_TOOL_RUNNING = 0x3498DB  # Turn — tool executing (turn only)
COLOR_PAUSED = 0xFEE75C  # Routine — paused (routines only)


def dim(text: str) -> str:
    """Wrap text in Discord's small/subtext markdown for dimmed (non-winning) rows."""
    return f"-# {text}"


def code(text: str) -> str:
    """Wrap text in backticks for inline identifier display."""
    return f"`{text}`"
