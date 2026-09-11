"""Slack admin write-gate.

``resolve_is_admin`` is the Slack analog of Discord's ``is_member_guild_admin``
+ ``require_manage_guild``: it calls ``users.info`` (I/O shell) then delegates
to the pure ``_is_admin_signal`` decision function.

Fail-closed: a transient failure from ``users.info`` — whether an API-level
``SlackApiError`` or a transport-level ``aiohttp`` error — is logged and
returns ``False``. It NEVER propagates and never grants admin. The caller
resolves once per interaction; no cross-interaction cache.
"""

from __future__ import annotations

from typing import Any

import aiohttp
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


async def resolve_admin_status(client: AsyncWebClient, *, user_id: str) -> bool | None:
    """Return the workspace-admin signal, or ``None`` if the lookup failed.

    Calls ``users.info`` via the injected per-event client; never caches the
    result on a module or runtime. This is the one place that calls
    ``users.info`` and catches its failures — ``resolve_is_admin`` below is
    implemented on top of it so there remains exactly one network call and one
    catch site.

    Both failure classes are caught. ``SlackApiError`` covers an API-level
    refusal (a missing ``users:read`` scope, a rate limit). ``aiohttp``
    transport errors and timeouts cover the network itself, and are NOT
    ``SlackApiError`` subclasses — before they were caught here, a transient
    connection reset propagated out of the mention path and killed the turn.

    Unlike ``resolve_is_admin``, this distinguishes "not an admin" (``False``)
    from "the lookup itself failed" (``None``) — callers that must not treat
    a transient Slack error as a demotion need that distinction.

    Args:
        client:  Per-event ``AsyncWebClient`` (injected; never cached).
        user_id: Slack user ID from the verified Socket Mode payload.

    Returns:
        ``True``/``False`` from ``users.info``'s admin signal, or ``None`` if
        the lookup failed at either the API or the transport layer.
    """
    try:
        resp = await client.users_info(user=user_id)  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
    except (TimeoutError, SlackApiError, aiohttp.ClientError) as exc:
        log.warning("slack.is_admin.lookup_failed", user=user_id, exc_info=exc)
        return None
    u: dict[str, Any] = resp["user"]  # pyright: ignore[reportUnknownVariableType, reportAssignmentType]  # SlackResponse subscript is untyped
    return _is_admin_signal(u)


async def resolve_is_admin(client: AsyncWebClient, *, user_id: str) -> bool:
    """Return True if the Slack user is a workspace admin, fail-closed.

    Thin wrapper over ``resolve_admin_status`` that collapses "not an admin"
    and "lookup failed" into ``False`` — this is the ONE deliberate
    sentinel-return at the adapter boundary, justified by the fail-closed
    security requirement, and it is unchanged for this function's existing
    callers.

    Args:
        client:  Per-event ``AsyncWebClient`` (injected; never cached).
        user_id: Slack user ID from the verified Socket Mode payload.

    Returns:
        ``True`` if the user is a workspace admin, owner, or primary owner;
        ``False`` otherwise, including on any ``users.info`` failure.
    """
    return bool(await resolve_admin_status(client, user_id=user_id))
