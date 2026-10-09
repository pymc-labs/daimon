"""Who may act on an answer: the people who could have asked the agent there.

Ask a human and message feedback both act on an answer daimon posted, and both
are open to exactly the people the tenant lets start a turn at that answer's
place (`authorize(START_TURN)`: protection and the invoker allowlist). The
identity reads are read-only: clicking a button must not mint an identity
record, so someone who never ran a turn has no stored role and is a plain
member. A user group a stored admin role names is looked up again, with no
database session open, because a slow Slack must not hold a connection.

External Slack Connect members never get here: app.py refuses their
block_actions before dispatch, and each view_submission evaluator refuses
theirs.
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import Literal

from daimon.adapters.slack.channel_admin_groups import user_group_members
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import Action, Subject, authorize, build_turn_place
from daimon.core.channel_admins import StoredAdmin, confirm_stored_subject, read_stored_admin
from daimon.core.stores.access_policy import AccessPolicyUnreadable, load_access_policy
from daimon.core.stores.identity import find_platform_principal
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "PlaceAccess",
    "check_place_access",
    "may_start_turn_at",
    "resolve_clicker",
    "stored_clicker",
]


async def stored_clicker(
    session: AsyncSession, *, tenant_id: uuid.UUID, user_id: str
) -> tuple[StoredAdmin, uuid.UUID | None]:
    """The clicker as their stored role describes them, and their account id."""
    principal = await find_platform_principal(
        session, tenant_id=tenant_id, platform="slack", external_id=user_id
    )
    account_id = principal.account_id if principal is not None else None
    if account_id is None:
        return StoredAdmin(is_admin=False, platform="slack", platform_user_id=user_id), None
    stored = await read_stored_admin(
        session,
        tenant_id=tenant_id,
        platform="slack",
        account_id=account_id,
        platform_user_id=user_id,
    )
    return stored, account_id


def may_start_turn_at(
    policy: TenantAccessPolicy, subject: Subject, *, channel_id: str, thread_ts: str
) -> bool:
    """START_TURN at the answer's place."""
    return bool(
        authorize(
            policy,
            subject=subject,
            action=Action.START_TURN,
            place=build_turn_place(channel_id=channel_id, thread_id=thread_ts),
        )
    )


@dataclasses.dataclass(frozen=True)
class PlaceAccess:
    """``decision`` is ``unreadable`` when the tenant's policy could not be read."""

    decision: Literal["allowed", "refused", "unreadable"]
    account_id: uuid.UUID | None
    policy: TenantAccessPolicy | None


async def resolve_clicker(
    runtime: SlackRuntime, client: AsyncWebClient, *, tenant_id: uuid.UUID, user_id: str
) -> tuple[Subject, uuid.UUID | None]:
    """The clicker as authorization sees them, and their account id.

    For a caller that decides access inside its own write transaction: the
    group lookup is network I/O and has to happen before that opens.
    """
    async with runtime.sessionmaker() as session:
        stored, account_id = await stored_clicker(session, tenant_id=tenant_id, user_id=user_id)
    subject = await confirm_stored_subject(
        stored, user_group_members(runtime, client, tenant_id=tenant_id)
    )
    return subject, account_id


async def check_place_access(
    runtime: SlackRuntime,
    client: AsyncWebClient,
    *,
    tenant_id: uuid.UUID,
    user_id: str,
    channel_id: str,
    thread_ts: str,
) -> PlaceAccess:
    """Decide whether ``user_id`` may act on an answer at ``channel_id``/``thread_ts``."""
    async with runtime.sessionmaker() as session:
        try:
            policy = await load_access_policy(session, tenant_id=tenant_id)
        except AccessPolicyUnreadable:
            return PlaceAccess(decision="unreadable", account_id=None, policy=None)
        stored, account_id = await stored_clicker(session, tenant_id=tenant_id, user_id=user_id)
    subject = await confirm_stored_subject(
        stored, user_group_members(runtime, client, tenant_id=tenant_id)
    )
    allowed = may_start_turn_at(policy, subject, channel_id=channel_id, thread_ts=thread_ts)
    return PlaceAccess(
        decision="allowed" if allowed else "refused", account_id=account_id, policy=policy
    )
