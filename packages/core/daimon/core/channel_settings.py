"""A channel's further settings, from any surface, each authorized, compared and recorded.

The sandbox, channel skills, tool rules, the provider pin, a channel's copy, the
workspace default agent and the overrides a channel's threads keep. Each follows
`daimon.core.channel_changes`: an `Expected` value that moved raises
`StaleChannelValue`, a refusal is `ChannelChangeRefused`, and the change is recorded
with the actor's `on_write` hook in the same transaction.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.core.access_policy import OPEN_RULE, TenantAccessPolicy
from daimon.core.agent_model_names import AmbiguousModel
from daimon.core.authz import Action, Place, authorize
from daimon.core.backend_refusals import refusal_card
from daimon.core.channel_backend import (
    AgentBackendRefused,
    BackendActor,
    BackendUnsupported,
    agent_selection,
    clear_channel_backend,
    current_backend,
    set_channel_backend,
)
from daimon.core.channel_changes import (
    NEEDS_ADMIN,
    ChannelActor,
    ChannelChangeRefused,
    Expected,
    after_write,
    refused_binding,
    require_channel_admin,
)
from daimon.core.channel_copies import ArchivedCopy, archive_channel_copy
from daimon.core.channel_environments import (
    authorize_environment_pick,
    build_limited_channels_confirm,
    build_limited_network_confirm,
    build_limited_network_refusal,
    save_scope_environment,
)
from daimon.core.channel_skills import REFUSALS, add_skill_to_channel, may_set_channel_skills
from daimon.core.errors import StaleChannelValue
from daimon.core.models_catalog import catalog_model
from daimon.core.permissions import channel_rule, with_channel_rule
from daimon.core.rule_views import binding_refusal
from daimon.core.scope import ChannelScopeRef, DeploymentDefault, TenantScopeRef
from daimon.core.specs import SkillRef
from daimon.core.stores import agent_tool_rules as tool_rules
from daimon.core.stores import mux_state
from daimon.core.stores import thread_agent_bindings as bindings
from daimon.core.stores.access_policy import (
    load_access_policy,
    lock_access_policy,
    set_access_policy,
)
from daimon.core.stores.channel_changes import WORKSPACE_ID, record_change, write_workspace_default
from daimon.core.stores.channel_skills import list_channel_skills, remove_channel_skill
from daimon.core.stores.domain import ChannelSkillRow, FeatureTarget, TenantBackendPolicy
from daimon.core.stores.feature_values import clear_feature_value, list_feature_values
from daimon.core.stores.platform_names import list_channel_names
from daimon.core.stores.scoped_config_read import get_scope
from mux.contracts.config import DEFAULT_BACKEND, BackendConfig, ConfigRevision
from mux.contracts.ids import ChannelRef
from mux.errors import InvalidConfig
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_MISSING_ENVIRONMENT = "No environment by that name. Nothing changed."
_NO_SKILL = "This channel doesn't add that skill. Nothing changed."
_TOOL_SAFETY_OFF = "Tool rules are read only while tool safety is off."
_BAD_TOOL = "Name a tool as connection/tool, from a connection the agent has."


class ConfirmNeeded(ChannelChangeRefused):
    """An allowed change that waits for the actor to confirm it; the message says why."""

    def __init__(self, message: str) -> None:
        super().__init__(message, kind="invalid", reason="needs_confirm")


@dataclass(frozen=True)
class ValueChange[T]:
    before: T
    after: T


# ---------- Sandbox ----------


async def set_environment(
    client: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    actor: ChannelActor,
    *,
    platform: str,
    channel_id: str | None,
    environment_name: str | None,
    default: DeploymentDefault,
    confirm_open_network: bool = False,
    hidden: frozenset[str] = frozenset(),
    expected: Expected[str | None] | None = None,
) -> ValueChange[str | None]:
    """Pick (None: clear) the channel's sandbox, or the workspace's when `channel_id` is None.

    `hidden` are names the actor's isolation hides; they read as missing. A pick that
    leaves a sealed channel on an open network raises `ConfirmNeeded` until confirmed.
    """
    async with sessionmaker() as session:
        pick = await authorize_environment_pick(
            session,
            client,
            tenant_id=actor.tenant_id,
            subject=actor.subject,
            channel_id=channel_id,
            environment_name=environment_name,
            default=default,
            records=sessionmaker,
        )
    if not pick.decision and pick.decision.reason != "not_a_reader":
        raise ChannelChangeRefused(NEEDS_ADMIN, kind="forbidden")
    if environment_name in hidden or pick.missing:
        raise ChannelChangeRefused(_MISSING_ENVIRONMENT, kind="not_found")
    if not pick.decision:
        raise ChannelChangeRefused(
            build_limited_network_refusal(environment_name=environment_name),
            kind="forbidden",
            reason="limited_network",
        )
    if pick.needs_confirm and not confirm_open_network:
        raise ConfirmNeeded(
            build_limited_network_confirm(environment_name=environment_name)
            if channel_id is not None
            else build_limited_channels_confirm(environment_name=environment_name)
        )
    name = pick.environment.name if pick.environment is not None else environment_name
    scope = (
        ChannelScopeRef(tenant_id=actor.tenant_id, channel_id=channel_id)
        if channel_id is not None
        else TenantScopeRef(tenant_id=actor.tenant_id)
    )
    async with sessionmaker.begin() as session:
        row = await get_scope(session, scope=scope)
        current = row.environment_name if row is not None else None
        if expected is not None and expected.value != current:
            raise StaleChannelValue("environment", current)
        previous = await save_scope_environment(
            session,
            tenant_id=actor.tenant_id,
            channel_id=channel_id,
            environment_name=name,
            actor_account_id=actor.account_id,
            platform=platform,
            surface=actor.surface,
        )
        await after_write(session, actor, previous != name)
    return ValueChange(previous, name)


# ---------- Skills ----------


async def add_skill(
    client: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    actor: ChannelActor,
    *,
    platform: str,
    channel_id: str,
    skill: str,
    default: DeploymentDefault,
    platform_skills: Mapping[str, Sequence[SkillRef]],
    expected: Expected[Sequence[str]] | None = None,
) -> ChannelSkillRow:
    """Add `skill` for the agent answering here. Raises SkillsListTruncatedError when unread."""
    _require_skill_admin(actor, channel_id)
    async with sessionmaker.begin() as session:
        await _compare_skills(session, actor, platform, channel_id, expected)
        added = await add_skill_to_channel(
            session,
            client,
            tenant_id=actor.tenant_id,
            platform=platform,
            channel_id=channel_id,
            skill=skill,
            default=default,
            actor_account_id=actor.account_id,
            platform_skills=platform_skills,
            surface=actor.surface,
            records=sessionmaker,
        )
        if isinstance(added, str):
            raise ChannelChangeRefused(REFUSALS[added], kind="invalid", reason=added)
        await after_write(session, actor, True)
    return added


async def remove_skill(
    sessionmaker: async_sessionmaker[AsyncSession],
    actor: ChannelActor,
    *,
    platform: str,
    channel_id: str,
    skill_id: str,
    expected: Expected[Sequence[str]] | None = None,
) -> None:
    _require_skill_admin(actor, channel_id)
    async with sessionmaker.begin() as session:
        await _compare_skills(session, actor, platform, channel_id, expected)
        removed = await remove_channel_skill(
            session,
            tenant_id=actor.tenant_id,
            platform=platform,
            channel_id=channel_id,
            skill_id=skill_id,
            actor_account_id=actor.account_id,
            surface=actor.surface,
        )
        if not removed:
            raise ChannelChangeRefused(_NO_SKILL, kind="not_found")
        await after_write(session, actor, True)


def _require_skill_admin(actor: ChannelActor, channel_id: str) -> None:
    if actor.subject.via_agent_key or not may_set_channel_skills(actor.subject, channel_id):
        raise ChannelChangeRefused(NEEDS_ADMIN, kind="forbidden")


async def _compare_skills(
    session: AsyncSession,
    actor: ChannelActor,
    platform: str,
    channel_id: str,
    expected: Expected[Sequence[str]] | None,
) -> None:
    if expected is None:
        return
    rows = await list_channel_skills(
        session, tenant_id=actor.tenant_id, platform=platform, channel_id=channel_id
    )
    current = sorted(r.skill_id for r in rows)
    if sorted(expected.value) != current:
        raise StaleChannelValue("skills", list(current))


# ---------- Tool rules ----------


def tool_server_url(agent: BetaManagedAgentsAgent, tool_key: str) -> str:
    """The connection URL `tool_key` (connection/tool) names on `agent`. Pure."""
    server, sep, tool = tool_key.partition("/")
    if sep and server and tool and "/" not in tool:
        for entry in agent.mcp_servers:
            if entry.name == server:
                return entry.url
    raise ChannelChangeRefused(_BAD_TOOL, kind="invalid", reason="bad_tool")


async def set_tool_rule(
    sessionmaker: async_sessionmaker[AsyncSession],
    actor: ChannelActor,
    *,
    tool_safety: bool,
    channel_id: str,
    agent: BetaManagedAgentsAgent,
    tool_key: str,
    rule: tool_rules.Rule | None,
    scope: tool_rules.Scope = "chat",
    expected_revision: int | None = None,
) -> tool_rules.AgentToolRule:
    """Set (None: clear) `agent`'s rule for `tool_key` in this channel.

    Refused while tool safety is off; `expected_revision` is the rule's own compare.
    The rule's history table records it.
    """
    if not tool_safety:
        raise ChannelChangeRefused(_TOOL_SAFETY_OFF, kind="invalid", reason="tool_safety_off")
    if actor.subject.via_agent_key or not authorize(
        _OPEN_POLICY,
        subject=actor.subject,
        action=Action.SET_CHANNEL_AGENT_TOOL_RULE,
        place=Place(channel_id=channel_id),
    ):
        raise ChannelChangeRefused(NEEDS_ADMIN, kind="forbidden")
    async with sessionmaker.begin() as session:
        existing = await tool_rules.get_rule(
            session,
            tenant_id=actor.tenant_id,
            agent_id=agent.id,
            tool_key=tool_key,
            channel_id=channel_id,
        )
        url = (
            existing.server_url
            if existing is not None and rule is None
            else tool_server_url(agent, tool_key)
        )
        try:
            row = await tool_rules.set_rule(
                session,
                tenant_id=actor.tenant_id,
                agent_id=agent.id,
                agent_name=agent.name,
                tool_key=tool_key,
                channel_id=channel_id,
                server_url=url,
                rule=rule,
                scope=scope,
                set_by=str(actor.account_id),
                actor=actor.subject,
                expected_revision=expected_revision,
            )
        except tool_rules.StaleAgentToolRule as exc:
            raise StaleChannelValue(
                "tool_rule", existing.revision if existing is not None else 0
            ) from exc
        await after_write(session, actor, True)
    return row


# ---------- Provider pin ----------


async def provider_pin(
    session: AsyncSession, *, tenant_id: uuid.UUID, platform: str, channel_id: str
) -> ConfigRevision | None:
    """The provider this channel pins, or None when it follows its agents'."""
    ref = ChannelRef(tenant_id=str(tenant_id), platform=platform, channel_id=channel_id)
    latest = await current_backend(session, ref)
    if latest is None or await mux_state.config_origin(session, ref, latest.local) != "channel":
        return None
    return latest


def pin_json(pin: ConfigRevision | None) -> dict[str, object] | None:
    """A pin as a change record and the web carry it. Pure."""
    if pin is None:
        return None
    return {"backend": pin.backend, "model": pin.model, "local": pin.local}


async def clear_provider_pin(
    sessionmaker: async_sessionmaker[AsyncSession],
    actor: ChannelActor,
    *,
    platform: str,
    channel_id: str,
    expected_local: int,
) -> None:
    """Clear the channel's provider pin; `expected_local` is the pin the actor read."""
    require_channel_admin(actor)
    if actor.account_id is None:
        raise ChannelChangeRefused(NEEDS_ADMIN, kind="forbidden")
    ref = ChannelRef(tenant_id=str(actor.tenant_id), platform=platform, channel_id=channel_id)
    async with sessionmaker.begin() as session:
        pin = await provider_pin(
            session, tenant_id=actor.tenant_id, platform=platform, channel_id=channel_id
        )
        if pin is None or pin.local != expected_local:
            raise StaleChannelValue("provider_pin", pin.local if pin is not None else None)
        await clear_channel_backend(
            session,
            ref,
            actor=BackendActor(actor.tenant_id, actor.account_id, actor.subject),
            expected=pin,
        )
        await _record(session, actor, platform, channel_id, "provider_pin", pin_json(pin), None)
        await after_write(session, actor, True)


async def set_provider_pin(
    sessionmaker: async_sessionmaker[AsyncSession],
    actor: ChannelActor,
    *,
    platform: str,
    channel_id: str,
    value: str,
    policy: TenantBackendPolicy,
    expected_local: int | None,
) -> ConfigRevision:
    """Pin the channel to the catalog model `value` (`provider:id`); `expected_local` is the
    pin the actor read, None for none. Every agent answers here on it until it is cleared.
    """
    require_channel_admin(actor)
    if actor.account_id is None:
        raise ChannelChangeRefused(NEEDS_ADMIN, kind="forbidden")
    try:
        model = catalog_model(value, tenant=policy)
        selection = agent_selection(model.provider, model.id)
    except (AgentBackendRefused, AmbiguousModel) as exc:
        line = (
            refusal_card(exc)[0]
            if isinstance(exc, AgentBackendRefused)
            else "That name fits several models. Pick one from the list."
        )
        raise ChannelChangeRefused(f"{line} Nothing was changed.", kind="invalid") from exc
    if model.provider == DEFAULT_BACKEND:
        raise ChannelChangeRefused(
            "A pin names a model on another provider. Nothing was changed.", kind="invalid"
        )
    ref = ChannelRef(tenant_id=str(actor.tenant_id), platform=platform, channel_id=channel_id)
    async with sessionmaker.begin() as session:
        before = await provider_pin(
            session, tenant_id=actor.tenant_id, platform=platform, channel_id=channel_id
        )
        if (before.local if before is not None else None) != expected_local:
            raise StaleChannelValue("provider_pin", before.local if before is not None else None)
        try:
            written = await set_channel_backend(
                session,
                ref,
                BackendConfig(
                    backend=selection.backend, profile=selection.profile, model=selection.model
                ),
            )
        except (BackendUnsupported, InvalidConfig) as exc:
            raise ChannelChangeRefused(
                "That model can't be pinned here. Nothing was changed.", kind="invalid"
            ) from exc
        await _record(
            session,
            actor,
            platform,
            channel_id,
            "provider_pin",
            pin_json(before),
            pin_json(written),
        )
        await after_write(session, actor, True)
    return written


# ---------- Channel copy ----------


async def retire_channel_copy(
    client: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    actor: ChannelActor,
    *,
    platform: str,
    channel_id: str,
    name: str,
    default: DeploymentDefault,
    expected_ma_agent_id: str | None = None,
) -> ArchivedCopy:
    """Archive this channel's own copy `name`; raises `ChannelCopyArchiveRefused` as it does.

    The copy must have been made for `channel_id`; another channel's copy is refused.
    """
    require_channel_admin(actor)
    archived = await archive_channel_copy(
        client,
        sessionmaker,
        tenant_id=actor.tenant_id,
        name=name,
        closing_channel_id=channel_id,
        subject=actor.subject,
        default=default,
        expected_ma_agent_id=expected_ma_agent_id,
    )
    async with sessionmaker.begin() as session:
        await _record(session, actor, platform, channel_id, "channel_copy", name, None)
        await after_write(session, actor, True)
    return archived


# ---------- Workspace default agent ----------


async def set_workspace_default_agent(
    sessionmaker: async_sessionmaker[AsyncSession],
    actor: ChannelActor,
    *,
    platform: str,
    agent_name: str | None,
    agent_names: Sequence[str] = (),
    expected: Expected[str | None] | None = None,
) -> ValueChange[str | None]:
    """Set (None: clear) the agent every channel without its own default uses."""
    require_channel_admin(actor)
    async with sessionmaker.begin() as session:
        await lock_access_policy(session, tenant_id=actor.tenant_id)
        policy = await load_access_policy(session, tenant_id=actor.tenant_id)
        row = await get_scope(session, scope=TenantScopeRef(tenant_id=actor.tenant_id))
        current = row.agent_name if row is not None else None
        if expected is not None and expected.value != current:
            raise StaleChannelValue("default_agent", current)
        if agent_name is not None:
            names = tuple(dict.fromkeys((agent_name, *agent_names)))
            refusal = binding_refusal(policy, agent_names=names, channel_id=None)
            if refusal is not None:
                raise refused_binding(refusal, agent_name)
        prior = await write_workspace_default(
            session,
            tenant_id=actor.tenant_id,
            platform=platform,
            agent_name=agent_name,
            actor_account_id=actor.account_id,
            surface=actor.surface,
            set_by_admin=True,
        )
        await after_write(session, actor, prior != agent_name)
    return ValueChange(prior, agent_name)


# ---------- Thread overrides ----------

ThreadOverrideKind = Literal["participation", "progress_plans", "thread_rule", "handoff"]
_THREAD_FEATURES: tuple[ThreadOverrideKind, ...] = ("participation", "progress_plans")


@dataclass(frozen=True)
class ThreadOverride:
    thread_id: str
    kind: ThreadOverrideKind
    value: str
    """The mode, the rule's readers and writers, or the agent a thread was handed to."""


async def list_thread_overrides(
    session: AsyncSession, *, tenant_id: uuid.UUID, platform: str, channel_id: str
) -> list[ThreadOverride]:
    """Every thread under `channel_id` with its own setting, one entry per setting."""
    rows = await list_feature_values(
        session, tenant_id=tenant_id, keys=_THREAD_FEATURES, tier="thread", channel_id=channel_id
    )
    rows.sort(key=lambda row: row.thread_key)
    found = [
        ThreadOverride(row.thread_key, kind, row.value)
        for kind in _THREAD_FEATURES
        for row in rows
        if row.key == kind
    ]
    policy = await load_access_policy(session, tenant_id=tenant_id)
    for key in await _thread_rule_keys(session, tenant_id, platform, channel_id, policy):
        rule = channel_rule(policy, key)
        found.append(ThreadOverride(key, "thread_rule", f"{rule.readers}/{rule.writers}"))
    for binding in await bindings.list_handoff_bindings(
        session, tenant_id=tenant_id, platform=platform, parent_channel_id=channel_id
    ):
        found.append(ThreadOverride(binding.thread_id, "handoff", binding.responder_name))
    return found


async def _thread_rule_keys(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str,
    policy: TenantAccessPolicy,
) -> list[str]:
    """Thread rule keys under `channel_id`: Slack's carry it, Discord's name it as parent."""
    keys = [k for k in policy.channel_rules if k != channel_id]
    if platform == "slack":
        return sorted(k for k in keys if k.startswith(f"{channel_id}:"))
    if platform != "discord" or not keys:
        return []
    children = {
        row.channel_id
        for row in await list_channel_names(session, tenant_id=tenant_id, platform=platform)
        if row.parent_id == channel_id
    }
    return sorted(k for k in keys if k in children)


async def clear_thread_override(
    sessionmaker: async_sessionmaker[AsyncSession],
    actor: ChannelActor,
    *,
    platform: str,
    channel_id: str,
    thread_id: str,
    kind: ThreadOverrideKind,
) -> None:
    """Drop one thread's own setting so it follows its channel again. Admins only."""
    require_channel_admin(actor)
    async with sessionmaker.begin() as session:
        listed = await list_thread_overrides(
            session, tenant_id=actor.tenant_id, platform=platform, channel_id=channel_id
        )
        match = next((o for o in listed if o.thread_id == thread_id and o.kind == kind), None)
        if match is None:
            raise StaleChannelValue(kind, None)
        if kind in _THREAD_FEATURES:
            await clear_feature_value(
                session,
                tenant_id=actor.tenant_id,
                target=FeatureTarget("thread", channel_id=channel_id, thread_key=thread_id),
                key=kind,
            )
        elif kind == "thread_rule":
            await lock_access_policy(session, tenant_id=actor.tenant_id)
            policy = await load_access_policy(session, tenant_id=actor.tenant_id)
            await set_access_policy(
                session,
                tenant_id=actor.tenant_id,
                policy=with_channel_rule(policy, thread_id, OPEN_RULE),
            )
        else:
            await bindings.update_lifecycle(
                session,
                tenant_id=actor.tenant_id,
                platform=platform,
                parent_channel_id=channel_id,
                thread_id=thread_id,
                archived=True,
            )
        setting = {
            "participation": "thread_participation",
            "progress_plans": "thread_progress_plans",
            "thread_rule": "thread_rule",
            "handoff": "thread_handoff",
        }[kind]
        await record_change(
            session,
            tenant_id=actor.tenant_id,
            platform=platform,
            channel_id=channel_id,
            setting=setting,  # pyright: ignore[reportArgumentType]
            before={"thread": thread_id, "value": match.value},
            after=None,
            actor_account_id=actor.account_id,
            surface=actor.surface,
        )
        await after_write(session, actor, True)


async def _record(
    session: AsyncSession,
    actor: ChannelActor,
    platform: str,
    channel_id: str | None,
    setting: Literal["provider_pin", "channel_copy"],
    before: object,
    after: object,
) -> None:
    await record_change(
        session,
        tenant_id=actor.tenant_id,
        platform=platform,
        channel_id=channel_id or WORKSPACE_ID,
        setting=setting,
        before=before,  # pyright: ignore[reportArgumentType]
        after=after,  # pyright: ignore[reportArgumentType]
        actor_account_id=actor.account_id,
        surface=actor.surface,
    )


_OPEN_POLICY = TenantAccessPolicy()

__all__ = [
    "ConfirmNeeded",
    "ThreadOverride",
    "ValueChange",
    "add_skill",
    "clear_provider_pin",
    "set_provider_pin",
    "clear_thread_override",
    "list_thread_overrides",
    "pin_json",
    "provider_pin",
    "remove_skill",
    "retire_channel_copy",
    "set_environment",
    "set_tool_rule",
    "set_workspace_default_agent",
    "tool_server_url",
]
