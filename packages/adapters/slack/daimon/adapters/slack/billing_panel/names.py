"""Name the /billing panel's top spenders and a looked-up person from `users.info`.

`users.info` answers with the person's profile, deactivated people included;
its display name (else real name, else username) is shown as escaped plain
text and remembered (`daimon.core.platform_names`). Lookups run concurrently
under one short timeout, and whoever has not answered keeps the name stored
from an earlier message or panel. Someone Slack never named to us is shown as
a `<@U…>` mention, which the modal renders as their name and notifies nobody.
"""

from __future__ import annotations

import dataclasses
import re
import uuid
from typing import Any, cast

import aiohttp
import structlog
from daimon.core.billing_panel import TOP_SPENDERS_SHOWN, BillingPanelState
from daimon.core.platform_names import KnownName, remember_user_names, resolve_names
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger()

# The panel waits at most this long for the users.info lookups, all together.
NAME_LOOKUP_TIMEOUT_S = 2.0
_USER_ID = re.compile(r"^[UW][A-Z0-9]{2,}$")


def is_user_id(value: str) -> bool:
    """Whether ``value`` is a Slack user id, which a `<@…>` mention can carry."""
    return _USER_ID.fullmatch(value) is not None


def known_name(user: dict[str, Any]) -> KnownName:
    """A `users.info` user's name: display name, else real name; the username as handle."""
    profile = cast("dict[str, Any]", user.get("profile") or {})
    display = profile.get("display_name") or profile.get("real_name") or user.get("real_name")
    handle = user.get("name")
    return KnownName(
        display_name=display if isinstance(display, str) else None,
        handle=handle if isinstance(handle, str) else None,
    )


async def users_info_name(client: AsyncWebClient, user_id: str) -> KnownName | None:
    """The person's name from `users.info`, or None when Slack can't say."""
    if not is_user_id(user_id):
        return None
    try:
        response = await client.users_info(user=user_id)  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
    except (TimeoutError, SlackApiError, aiohttp.ClientError) as exc:
        log.info("slack.billing.name_lookup_failed", error=type(exc).__name__)
        return None
    user = response.get("user")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]  # SlackResponse is untyped
    if not isinstance(user, dict):
        return None
    name = known_name(cast("dict[str, Any]", user))
    return name if name.label else None


async def name_shown_spenders(
    client: AsyncWebClient,
    state: BillingPanelState,
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    timeout_s: float = NAME_LOOKUP_TIMEOUT_S,
) -> BillingPanelState:
    """The state with the shown top spenders named from `users.info`, else their stored name.

    The names Slack gave are remembered in the background.
    """
    shown = state.member_rows[:TOP_SPENDERS_SHOWN]
    if not shown:
        return state
    stored = {row.platform_user_id: row.display_name for row in shown if row.display_name}

    async def live(user_id: str) -> KnownName | None:
        return await users_info_name(client, user_id)

    labels, found = await resolve_names(
        [row.platform_user_id for row in shown],
        live=live,
        stored=stored,
        timeout_s=timeout_s,
        log_event="slack.billing.name_lookup_timed_out",
    )
    if found:
        remember_user_names(sessionmaker, tenant_id=tenant_id, platform="slack", names=found)
    rows = tuple(
        dataclasses.replace(row, display_name=labels.get(row.platform_user_id)) for row in shown
    )
    return dataclasses.replace(state, member_rows=rows + state.member_rows[TOP_SPENDERS_SHOWN:])


async def lookup_name(
    client: AsyncWebClient,
    user_id: str,
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    stored: str | None,
    timeout_s: float = NAME_LOOKUP_TIMEOUT_S,
) -> str | None:
    """The picked person's name for the lookup reply: live, else stored, else None."""

    async def live(uid: str) -> KnownName | None:
        return await users_info_name(client, uid)

    labels, found = await resolve_names(
        [user_id],
        live=live,
        stored={user_id: stored} if stored else {},
        timeout_s=timeout_s,
        log_event="slack.billing.name_lookup_timed_out",
    )
    if found:
        remember_user_names(sessionmaker, tenant_id=tenant_id, platform="slack", names=found)
    return labels.get(user_id)
