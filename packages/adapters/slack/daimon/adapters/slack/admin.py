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

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Final, cast

from daimon.core.admin_actions import DelegatedAction
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.native_admins import is_daimon_admin

if TYPE_CHECKING:
    from daimon.adapters.slack.runtime import SlackRuntime

import aiohttp
import structlog
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

log = structlog.get_logger()

#: Slack's own name for the person a member is sent to when a change needs
#: elevated permission. Copy never says "admin" in the abstract.
ADMIN_NOUN: Final = "a Daimon admin"

#: Longest display name rendered into a prompt attribute.
_DISPLAY_NAME_MAX: Final = 80


def _is_admin_signal(user: dict[str, Any]) -> bool:
    """Return True if the Slack user dict carries any admin signal.

    Reads ``is_admin``, ``is_owner``, and ``is_primary_owner``: every field
    Slack exposes for elevated privilege. Pure: no I/O.
    """
    return any(user.get(key) is True for key in ("is_admin", "is_owner", "is_primary_owner"))


@dataclass(frozen=True)
class SlackRequester:
    """What one ``users.info`` call says about the person who sent a turn."""

    #: True only for a verified host-workspace member carrying an admin signal;
    #: an external or unverified user is never an admin, whatever their own
    #: workspace says about them.
    is_admin: bool
    #: ``profile.display_name``, else ``profile.real_name``, else ``name``;
    #: ``None`` when Slack returned none of them.
    display_name: str | None
    #: ``users.info`` ``team_id``: the user's home workspace; ``None`` when absent.
    team_id: str | None
    #: ``users.info`` ``is_stranger``: a Slack Connect user from outside the host
    #: workspace sharing a channel with it.
    is_stranger: bool
    #: The workspace hosting the app, which the user must belong to.
    host_team_id: str

    @property
    def is_host_member(self) -> bool:
        """True for a verified member of the host workspace.

        A missing ``team_id`` is not verified: the claim must be positive.
        """
        return self.team_id == self.host_team_id and not self.is_stranger


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


async def _lookup_user(client: AsyncWebClient, *, user_id: str) -> dict[str, Any] | None:
    """Call ``users.info`` and return the user dict, or ``None`` if the lookup failed.

    The one network call and the one catch site: ``resolve_requester`` and
    ``resolve_is_admin`` are both built on it.

    Both failure classes are caught. ``SlackApiError`` covers an API-level
    refusal (a missing ``users:read`` scope, a rate limit). ``aiohttp``
    transport errors and timeouts cover the network itself, and are NOT
    ``SlackApiError`` subclasses.
    """
    try:
        resp = await client.users_info(user=user_id)  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
    except (TimeoutError, SlackApiError, aiohttp.ClientError) as exc:
        log.warning("slack.is_admin.lookup_failed", user=user_id, exc_info=exc)
        return None
    raw: object = resp.get("user")  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
    if not isinstance(raw, dict):
        return None
    user = cast(dict[str, Any], raw)
    if (
        user.get("id") != user_id
        or user.get("deleted") is not False
        or user.get("is_bot") is not False
    ):
        return None
    return user


async def resolve_requester(
    client: AsyncWebClient, *, user_id: str, host_team_id: str | None = None
) -> SlackRequester | None:
    """Return what ``users.info`` says about the requester, or ``None`` if the lookup failed.

    Calls ``users.info`` once via the injected per-event client; never caches
    the result. ``None`` (lookup failed) is distinct from "not an admin", so
    callers that must not treat a transient Slack error as a demotion can tell
    them apart.

    ``is_admin`` is True only when the user is a verified member of
    ``host_team_id`` and carries an admin signal.

    Args:
        client:       Per-event ``AsyncWebClient`` (injected; never cached).
        user_id:      Slack user ID from the verified Socket Mode payload.
        host_team_id: The workspace that hosts the app.
    """
    bound_team = client.team_id if isinstance(client, WorkspaceWebClient) else None
    if bound_team is not None and host_team_id is not None and bound_team != host_team_id:
        return None
    host_team_id = host_team_id or bound_team
    if not host_team_id:
        return None
    u = await _lookup_user(client, user_id=user_id)
    if u is None:
        return None
    raw_team = u.get("team_id")
    requester = SlackRequester(
        is_admin=False,
        display_name=_display_name(u),
        team_id=raw_team if isinstance(raw_team, str) and raw_team else None,
        is_stranger=bool(u.get("is_stranger")),
        host_team_id=host_team_id,
    )
    return replace(requester, is_admin=requester.is_host_member and _is_admin_signal(u))


async def resolve_admin_status(
    client: AsyncWebClient, *, user_id: str, host_team_id: str | None = None
) -> bool | None:
    """Return the host-workspace admin signal, or ``None`` if the lookup failed.

    Thin wrapper over ``resolve_requester``: ``True`` only for a verified host
    member with an admin signal, ``False`` otherwise, ``None`` when the lookup
    failed at the API or transport layer.
    """
    requester = await resolve_requester(client, user_id=user_id, host_team_id=host_team_id)
    return None if requester is None else requester.is_admin


class WorkspaceWebClient(AsyncWebClient):
    """A per-event client bound to the verified workspace, never caller input."""

    runtime: SlackRuntime
    team_id: str

    def bind_workspace(self, runtime: SlackRuntime, team_id: str) -> WorkspaceWebClient:
        self.runtime, self.team_id = runtime, team_id
        return self


async def resolve_is_platform_admin(
    client: AsyncWebClient, *, user_id: str, host_team_id: str | None = None
) -> bool:
    """A freshly verified host-workspace platform admin, never a grant."""
    status = await resolve_admin_status(client, user_id=user_id, host_team_id=host_team_id)
    return status is True


async def resolve_is_admin(
    client: AsyncWebClient,
    *,
    user_id: str,
    host_team_id: str | None = None,
    action: DelegatedAction | None = None,
) -> bool:
    """Live host-workspace authority plus an uncached, explicitly allow-listed grant."""
    requester = await resolve_requester(client, user_id=user_id, host_team_id=host_team_id)
    if requester is None or not requester.is_host_member:
        return False
    if not isinstance(action, DelegatedAction) or not isinstance(client, WorkspaceWebClient):
        return requester.is_admin
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=client.team_id)
    async with client.runtime.sessionmaker() as session:
        return await is_daimon_admin(
            session,
            tenant_id=tenant_id,
            platform="slack",
            platform_user_id=user_id,
            is_platform_admin=requester.is_admin,
        )
