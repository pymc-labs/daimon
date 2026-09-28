"""Tenant access policy: who may invoke the agent and where it may write or read.

Pure -- no I/O. The store (`daimon.core.stores.access_policy`) loads it; the
turn chokepoint and the MCP channel tools evaluate it with the predicates
below. Every field defaults to "open", so a tenant without a policy row
behaves exactly as before the policy existed.

Ids are platform-native strings (Discord snowflakes, Slack ids, Teams Entra
object and conversation ids) for the tenant's own platform. Channels are named by id, never by name.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class TenantAccessPolicy(BaseModel):
    """One tenant's access policy. The shape is shared across lanes: add fields, never rename."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Platform user ids allowed to start a turn. Empty means everyone may.
    invoker_user_ids: tuple[str, ...] = ()
    # Channels (and every thread under them) the agent must never write into.
    protected_channel_ids: tuple[str, ...] = ()
    # Discord categories whose channels are protected as above.
    protected_category_ids: tuple[str, ...] = ()
    # Channels whose content is readable only from a turn inside them.
    sealed_channel_ids: tuple[str, ...] = ()
    # Whether turns started from a DM get read-only memory mounts.
    dm_memory_read_only: bool = False


OPEN_ACCESS_POLICY = TenantAccessPolicy()


def is_invoker_allowed(
    policy: TenantAccessPolicy, *, external_user_id: str, is_admin: bool
) -> bool:
    """Whether this user may start a turn. Admins always may, so they can't lock themselves out."""
    if is_admin or not policy.invoker_user_ids:
        return True
    return external_user_id in policy.invoker_user_ids


def is_write_protected(
    policy: TenantAccessPolicy,
    *,
    channel_id: str,
    parent_channel_id: str | None = None,
    category_id: str | None = None,
    category_unresolved: bool = False,
) -> bool:
    """Whether the agent must refuse to write to `channel_id`.

    Pass the parent channel for a thread and the category for a Discord
    channel; protection on either one covers the target. When the category
    couldn't be looked up, `category_unresolved` fails closed as soon as the
    policy protects any category.
    """
    if channel_id in policy.protected_channel_ids:
        return True
    if parent_channel_id is not None and parent_channel_id in policy.protected_channel_ids:
        return True
    if category_unresolved and policy.protected_category_ids:
        return True
    return category_id is not None and category_id in policy.protected_category_ids


def is_sealed(
    policy: TenantAccessPolicy, *, channel_id: str, parent_channel_id: str | None = None
) -> bool:
    """Whether `channel_id`, or the channel a thread sits under, is sealed."""
    if channel_id in policy.sealed_channel_ids:
        return True
    return parent_channel_id is not None and parent_channel_id in policy.sealed_channel_ids
