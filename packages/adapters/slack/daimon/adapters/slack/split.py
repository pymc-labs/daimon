"""Answer splitting for Slack's native markdown block (12 000-char ceiling)."""

from __future__ import annotations

from daimon.core.message_split import split_fenced
from daimon.core.slack_mrkdwn import SLACK_LIMIT


def split_for_slack_safe(text: str, limit: int = SLACK_LIMIT) -> list[str]:
    """Split *text* into chunks of at most *limit* chars, repairing code fences."""
    return split_fenced(text, limit)
