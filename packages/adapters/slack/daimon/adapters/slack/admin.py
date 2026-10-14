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

from dataclasses import dataclass
from typing import Any, Final, cast

import aiohttp
import structlog
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

log = structlog.get_logger()

#: Slack's own name for the person a member is sent to when a change needs
#: elevated permission. Copy never says "admin" in the abstract.
ADMIN_NOUN: Final = "a workspace admin"

#: Longest display name rendered into a prompt attribute.
_DISPLAY_NAME_MAX: Final = 80


def _is_admin_signal(user: dict[str, Any]) -> bool:
    """Return True if the Slack user dict carries any admin signal.

    Reads ``is_admin``, ``is_owner``, and ``is_primary_owner``: every field
    Slack exposes for elevated privilege. Pure: no I/O.
    """
    return bool(user.get("is_admin") or user.get("is_owner") or user.get("is_primary_owner"))


@dataclass(frozen=True)
class SlackRequester:
    """What one ``users.info`` call says about the person who sent a turn."""

    is_admin: bool
    #: ``profile.display_name``, else ``profile.real_name``, else ``name``;
    #: ``None`` when Slack returned none of them.
    display_name: str | None


def _display_name(user: dict[str, Any]) -> str | None:
    """First non-blank of profile display_name, profile real_name, user name."""
    raw_profile = user.get("profile")
    profile: dict[str, Any] = (
        cast("dict[str, Any]", raw_profile) if isinstance(raw_profile, dict) else {}
    )
    candidates: tuple[object, ...] = (
        profile.get("display_name"),
        profile.get("real_name"),
        user.get("name"),
    )
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()[:_DISPLAY_NAME_MAX]
    return None


async def resolve_requester(client: AsyncWebClient, *, user_id: str) -> SlackRequester | None:
    """Return the requester's admin signal and display name, or ``None`` if the lookup failed.

    Calls ``users.info`` via the injected per-event client; never caches the
    result on a module or runtime. This is the one place that calls
    ``users.info`` and catches its failures — ``resolve_admin_status`` and
    ``resolve_is_admin`` below are implemented on top of it so there remains
    exactly one network call and one catch site.

    Both failure classes are caught. ``SlackApiError`` covers an API-level
    refusal (a missing ``users:read`` scope, a rate limit). ``aiohttp``
    transport errors and timeouts cover the network itself, and are NOT
    ``SlackApiError`` subclasses — before they were caught here, a transient
    connection reset propagated out of the mention path and killed the turn.

    Unlike ``resolve_is_admin``, this distinguishes "not an admin" from "the
    lookup itself failed" (``None``) — callers that must not treat a transient
    Slack error as a demotion need that distinction.

    Args:
        client:  Per-event ``AsyncWebClient`` (injected; never cached).
        user_id: Slack user ID from the verified Socket Mode payload.

    Returns:
        A ``SlackRequester`` from ``users.info``, or ``None`` if the lookup
        failed at either the API or the transport layer.
    """
    try:
        resp = await client.users_info(user=user_id)  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
    except (TimeoutError, SlackApiError, aiohttp.ClientError) as exc:
        log.warning("slack.is_admin.lookup_failed", user=user_id, exc_info=exc)
        return None
    u: dict[str, Any] = resp["user"]  # pyright: ignore[reportUnknownVariableType, reportAssignmentType]  # SlackResponse subscript is untyped
    return SlackRequester(is_admin=_is_admin_signal(u), display_name=_display_name(u))


async def resolve_admin_status(client: AsyncWebClient, *, user_id: str) -> bool | None:
    """Return the workspace-admin signal, or ``None`` if the lookup failed.

    Thin wrapper over ``resolve_requester`` for callers that only need the
    admin signal: ``True``/``False`` from ``users.info``, ``None`` when the
    lookup failed at the API or transport layer.
    """
    requester = await resolve_requester(client, user_id=user_id)
    return None if requester is None else requester.is_admin


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
