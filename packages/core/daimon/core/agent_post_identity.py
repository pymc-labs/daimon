"""Platform-independent rules for agent-authored posts."""

from __future__ import annotations

import re

DISCORD_AGENT_WEBHOOK_NAME = "Daimon agents"


def discord_username(name: str) -> str:
    """Return a usable Discord webhook username from an agent name."""
    clean = re.sub("discord|clyde", "", name, flags=re.IGNORECASE).replace("```", "")
    return clean.strip()[:80] or "Agent"


def fallback_name_prefix(name: str, content: str) -> str:
    """Label the first answer chunk when platform identity override is unavailable."""
    safe_name = discord_username(name).replace("\\", "\\\\").replace("*", "\\*")
    safe_name = safe_name.replace("@", "@\u200b").replace("`", "\\`")
    return f"**{safe_name}** {content}"


def is_our_discord_webhook(
    *,
    application_id: int | None,
    channel_id: int | None,
    our_application_id: int,
    target_channel_id: int,
) -> bool:
    return application_id == our_application_id and channel_id == target_channel_id
