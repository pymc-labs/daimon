"""Tenant access policy: who may invoke the agent and where it may write or read.

Pure -- no I/O. The store (`daimon.core.stores.access_policy`) loads it; the
turn chokepoint and the MCP channel tools evaluate it with the predicates
below. Every field defaults to "open", so a tenant without a policy row
behaves exactly as before the policy existed.

Ids are platform-native strings (Discord snowflakes, Slack ids, Teams Entra
object and conversation ids) for the tenant's own platform. Channels are named by id, never by name.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
    # A marker on sealed channels whose own agents (those pinned to that
    # channel alone, `isolation_owner`) are the only ones answering and seen
    # there; see `daimon.core.channel_isolation`.
    isolated_channel_ids: tuple[str, ...] = ()
    # Agent name -> the only channels (and threads under them) it may run in.
    # An agent not named here runs wherever the cascade sends it.
    agent_channel_pins: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    # Teams guests (Entra object ids, lower case) treated as members of the
    # organisation; any other guest is answered as from another organisation.
    # Only read while `teams.restrict_guests` is on.
    member_guest_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _isolated_channels_are_sealed(self) -> TenantAccessPolicy:
        # Isolation adds to a seal; it never stands without one.
        unsealed = sorted(set(self.isolated_channel_ids) - set(self.sealed_channel_ids))
        if unsealed:
            raise ValueError(f"confidential channels must also be sealed: {', '.join(unsealed)}")
        return self


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


def isolated_channel_of(
    policy: TenantAccessPolicy, channel_id: str | None, parent_channel_id: str | None = None
) -> str | None:
    """The isolated channel a place lies in (a thread counts as its parent), or None."""
    for candidate in (parent_channel_id, channel_id):
        if candidate is not None and candidate in policy.isolated_channel_ids:
            return candidate
    return None


def is_isolated(
    policy: TenantAccessPolicy, *, channel_id: str, parent_channel_id: str | None = None
) -> bool:
    """Whether `channel_id`, or the channel a thread sits under, is isolated."""
    return isolated_channel_of(policy, channel_id, parent_channel_id) is not None


def isolation_owner(policy: TenantAccessPolicy, agent_names: tuple[str | None, ...]) -> str | None:
    """The isolated channel this agent belongs to, or None for every other agent.

    An agent belongs to isolated channel C when it is pinned to C alone: every
    pin on any of its names (pass them all, as for `is_outside_agent_pin`)
    lies in C, and at least one names C.
    """
    pinned = [
        policy.agent_channel_pins[name]
        for name in agent_names
        if name is not None and name in policy.agent_channel_pins
    ]
    channels = {channel for pin in pinned for channel in pin}
    if len(channels) != 1:
        return None
    (channel,) = channels
    return channel if channel in policy.isolated_channel_ids else None


def source_seal_ids(
    policy: TenantAccessPolicy, *, channel_id: str, thread_id: str | None
) -> frozenset[str]:
    """Every id that seals a turn: its channel and a thread sealed on its own.

    A Discord thread is sealed by its id, a Slack one as channel_id:thread_ts.
    All of them are recorded, so unsealing one later leaves the others holding.
    """
    return frozenset(
        candidate
        for candidate in (
            channel_id,
            thread_id,
            f"{channel_id}:{thread_id}" if thread_id is not None else None,
        )
        if candidate is not None and candidate in policy.sealed_channel_ids
    )


def is_sealed_source(policy: TenantAccessPolicy, *, channel_id: str, thread_id: str | None) -> bool:
    """Whether a turn from `channel_id` (optionally `thread_id` under it) is inside a seal.

    Covers a sealed channel, a thread under one, a sealed Discord thread by its
    own id, and a Slack thread sealed on its own as ``channel_id:thread_ts``.
    """
    return is_sealed(policy, channel_id=thread_id or channel_id, parent_channel_id=channel_id) or (
        thread_id is not None and f"{channel_id}:{thread_id}" in policy.sealed_channel_ids
    )


def is_outside_agent_pin(
    policy: TenantAccessPolicy,
    *,
    agent_names: tuple[str | None, ...],
    channel_id: str | None,
    parent_channel_id: str | None = None,
) -> bool:
    """Whether a pinned agent is being asked to run outside its channels.

    Pass every name the responder answers to (the cascade's name and the
    agent's own metadata name); a pin on any of them applies. A turn with no
    channel (a DM, a headless run) is outside every pin. This predicate has no
    admin exemption; callers grant one only where the output reaches the admin
    alone (an admin's DM or hub turn -- see "Trust model" in
    docs/architecture.md), and channel sends stay confined to the pin.
    """
    for name in agent_names:
        if name is None or name not in policy.agent_channel_pins:
            continue
        pinned = policy.agent_channel_pins[name]
        if channel_id is not None and channel_id in pinned:
            continue
        if parent_channel_id is not None and parent_channel_id in pinned:
            continue
        return True
    return False


DM_SCOPE_PREFIX = "dm:"


def is_dm_source_sealed(
    policy: TenantAccessPolicy,
    *,
    source_channel_id: str | None,
    source_thread_id: str | None,
    source_thread_keys: Sequence[str] = (),
) -> bool:
    """Whether any recorded source of a DM conversation is sealed now.

    Fails closed: a conversation without recorded provenance (written before
    it was stored) cannot prove its source unsealed, so any seal in the
    tenant counts. For a Discord thread the parent can't be recovered from
    the old source URL.
    """
    if not policy.sealed_channel_ids:
        return False
    if source_channel_id is None:
        return True
    if is_sealed_source(policy, channel_id=source_channel_id, thread_id=source_thread_id):
        return True
    sealed = set(policy.sealed_channel_ids)
    return any(key in sealed for key in source_thread_keys)
