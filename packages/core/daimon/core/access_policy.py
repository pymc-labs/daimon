"""Tenant access policy: who may start turns, and the channel and agent rules.

Pure -- no I/O. The store (`daimon.core.stores.access_policy`) loads it, and
`daimon.core.permissions` says what the rules mean. Every field defaults to
open, so a tenant without a policy row behaves as before the policy existed.

Ids are platform-native strings (Discord snowflakes, Slack ids, Teams Entra
object and conversation ids) for the tenant's own platform. Channels are named by id, never by name.
"""

from __future__ import annotations

from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ChannelReaders = Literal["any", "inside", "own"]
"""Whose turns may read a channel: any turn, a turn inside it, or its own
agents' turns inside it (agents whose `AgentRule.runs_in` names it alone)."""

ChannelWriters = Literal["any", "own", "none"]
"""Who may start turns and post in a channel: anyone, its own agents only, or
nobody, admins and daimon's notices included."""


class ChannelRule(BaseModel):
    """Who may read and write one channel, Discord thread, Slack thread
    (``channel:ts``) or Discord category."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    readers: ChannelReaders = "any"
    writers: ChannelWriters = "any"

    @model_validator(mode="after")
    def _own_on_both(self) -> ChannelRule:
        # Only a channel kept to its own agents has own agents to write, and
        # one kept to them is written by them or by nobody.
        if (self.writers == "own") != (self.readers == "own") and self.writers != "none":
            raise ValueError("`own` goes on both readers and writers, or readers with writers none")
        return self


OPEN_RULE = ChannelRule()


class AgentRule(BaseModel):
    """Where one agent may run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    runs_in: tuple[str, ...] | None = None
    """The only channels (and threads under them) it runs in. None runs it
    wherever the cascade sends it; an empty tuple runs it nowhere."""


# The shape before rules: five id lists. Still read, so a row an older build
# wrote keeps working; every write stores rules.
_LEGACY_KEYS = frozenset(
    {
        "protected_channel_ids",
        "protected_category_ids",
        "sealed_channel_ids",
        "isolated_channel_ids",
        "agent_channel_pins",
    }
)


def _ids(data: dict[str, Any], key: str) -> list[str]:
    value: object = data.pop(key, [])
    ids = list(cast("list[object]", value)) if isinstance(value, list | tuple) else None
    if ids is None or not all(isinstance(i, str) for i in ids):
        raise ValueError(f"{key} must be a list of ids")
    return cast("list[str]", ids)


def _from_lists(data: dict[str, Any]) -> dict[str, Any]:
    if {"channel_rules", "category_rules", "agent_rules"} & data.keys():
        raise ValueError("a policy holds rules or the id lists before them, not both")
    data = dict(data)
    protected = _ids(data, "protected_channel_ids")
    sealed = _ids(data, "sealed_channel_ids")
    isolated = _ids(data, "isolated_channel_ids")
    if set(isolated) - set(sealed):
        raise ValueError("isolated channels must also be sealed")
    rules: dict[str, dict[str, str]] = {}
    for channel_id in dict.fromkeys((*protected, *sealed, *isolated)):
        own = channel_id in isolated
        readers = "own" if own else "inside" if channel_id in sealed else "any"
        writers = "none" if channel_id in protected else "own" if own else "any"
        rules[channel_id] = {"readers": readers, "writers": writers}
    data["channel_rules"] = rules
    data["category_rules"] = {
        category_id: {"writers": "none"} for category_id in _ids(data, "protected_category_ids")
    }
    pins: object = data.pop("agent_channel_pins", {})
    if not isinstance(pins, dict):
        raise ValueError("agent_channel_pins must map agent names to channel ids")
    data["agent_rules"] = {
        name: {"runs_in": pin} for name, pin in cast("dict[object, object]", pins).items()
    }
    return data


class TenantAccessPolicy(BaseModel):
    """One tenant's access policy. Renaming a field means reading the old name too."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Platform user ids allowed to start a turn. Empty means everyone may.
    invoker_user_ids: tuple[str, ...] = ()
    # Channel, Discord thread or Slack ``channel:ts`` id -> its rule; a thread
    # also follows its channel's. Open rules are not kept.
    channel_rules: dict[str, ChannelRule] = Field(default_factory=dict)
    # Discord category id -> its rule; only `writers: none` applies.
    category_rules: dict[str, ChannelRule] = Field(default_factory=dict)
    # Whether turns started from a DM get read-only memory mounts.
    dm_memory_read_only: bool = False
    # Agent name -> its rule. An agent not named here runs wherever the cascade sends it.
    agent_rules: dict[str, AgentRule] = Field(default_factory=dict)
    # Teams guests (Entra object ids, lower case) treated as members of the
    # organisation; any other guest is answered as from another organisation.
    # Only read while `teams.restrict_guests` is on.
    member_guest_ids: tuple[str, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def _read_lists(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        fields = cast("dict[str, Any]", data)
        return _from_lists(fields) if _LEGACY_KEYS & fields.keys() else fields

    @field_validator("channel_rules", "category_rules")
    @classmethod
    def _drop_open(cls, rules: dict[str, ChannelRule]) -> dict[str, ChannelRule]:
        return {key: rule for key, rule in rules.items() if rule != OPEN_RULE}

    @field_validator("agent_rules")
    @classmethod
    def _drop_unset(cls, rules: dict[str, AgentRule]) -> dict[str, AgentRule]:
        return {name: rule for name, rule in rules.items() if rule.runs_in is not None}


OPEN_ACCESS_POLICY = TenantAccessPolicy()


def is_invoker_allowed(
    policy: TenantAccessPolicy, *, external_user_id: str, is_admin: bool
) -> bool:
    """Whether this user may start a turn. Admins always may, so they can't lock themselves out."""
    if is_admin or not policy.invoker_user_ids:
        return True
    return external_user_id in policy.invoker_user_ids


DM_SCOPE_PREFIX = "dm:"
