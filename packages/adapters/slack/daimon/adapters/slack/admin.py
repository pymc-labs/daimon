"""Slack admin write-gate.

``resolve_is_admin`` is the Slack analog of Discord's ``is_member_guild_admin``
+ ``require_manage_guild``: it calls ``users.info`` (I/O shell) then delegates
to the pure ``_is_admin_signal`` decision function.

Fail-closed: a transient ``SlackApiError`` from ``users.info`` is
logged and returns ``False`` — it NEVER propagates and never grants admin.
The caller resolves once per interaction; no cross-interaction cache.
"""

from __future__ import annotations

from typing import Any

import structlog
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

log = structlog.get_logger()


def _is_admin_signal(user: dict[str, Any]) -> bool:
    """Return True if the Slack user dict carries any admin signal.

    Reads ``is_admin``, ``is_owner``, and ``is_primary_owner`` (all three per
    A3 — read every field Slack exposes for elevated privilege). Pure: no I/O.
    """
    return bool(user.get("is_admin") or user.get("is_owner") or user.get("is_primary_owner"))


async def resolve_is_admin(client: AsyncWebClient, *, user_id: str) -> bool:
    """Return True if the Slack user is a workspace admin, fail-closed.

    Calls ``users.info`` via the injected per-event client; never caches the
    result on a module or runtime.  On ``SlackApiError`` logs a warning
    and returns ``False`` — this is the ONE deliberate sentinel-return at the
    adapter boundary, justified by the fail-closed security requirement.

    Args:
        client:  Per-event ``AsyncWebClient`` (injected; never cached).
        user_id: Slack user ID from the verified Socket Mode payload.

    Returns:
        ``True`` if the user is a workspace admin, owner, or primary owner;
        ``False`` otherwise, including on ``users.info`` failure.
    """
    try:
        resp = await client.users_info(user=user_id)  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
    except SlackApiError as exc:
        log.warning("slack.is_admin.lookup_failed", user=user_id, exc_info=exc)
        return False  # fail-closed
    u: dict[str, Any] = resp["user"]  # pyright: ignore[reportUnknownVariableType, reportAssignmentType]  # SlackResponse subscript is untyped
    return _is_admin_signal(u)
