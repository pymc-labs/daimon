"""Every platform payload the contract checks cover, by name.

Importing this module registers the Slack and Discord entries. `CATALOGUE` maps a
name to a zero-argument builder returning `Built(platform, kind, payload)`.
"""

from __future__ import annotations

from . import discord_payloads, slack_payloads  # registering the entries is the import's job
from .registry import CATALOGUE, Builder, Built, Kind, Platform, entry

__all__ = [
    "CATALOGUE",
    "Built",
    "Builder",
    "Kind",
    "Platform",
    "discord_payloads",
    "entry",
    "slack_payloads",
]
