"""Permission self-check -- pure function comparing guild permissions against required set."""

from __future__ import annotations

import discord

REQUIRED_PERMISSIONS: frozenset[str] = frozenset(
    {
        "send_messages",
        "send_messages_in_threads",
        "create_public_threads",
        "manage_threads",
        "read_message_history",
        "embed_links",
    }
)


def check_missing_permissions(permissions: discord.Permissions) -> list[str]:
    """Return missing permission names. Empty list means all required permissions are present."""
    missing: list[str] = []
    for perm_name in sorted(REQUIRED_PERMISSIONS):
        if not getattr(permissions, perm_name, False):
            missing.append(perm_name)
    return missing
