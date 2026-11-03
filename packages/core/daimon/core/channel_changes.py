"""Change one channel setting from any surface: authorize, compare, write, record.

The web app, the chat tools and the panels call these, so each change is checked
the same way and lands in `channel_changes` with its before and after values. Each
function takes the actor's `Subject` and the surface it acts from. An `Expected`
value is what the caller last read: when the setting differs under the lock,
`StaleChannelValue` is raised and nothing is written (the web answers 409). Callers
that never showed a value (a chat tool) pass none.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from anthropic import AsyncAnthropic
from daimon.core.access_policy import ChannelReaders, ChannelWriters
from daimon.core.authz import Subject
from daimon.core.channel_budget import BudgetSpec, may_set_channel_budget
from daimon.core.channel_rules import (
    AgentRuleChange,
    ChannelRuleRefused,
    RuleChange,
    set_agent_rule,
    set_channel_rule,
)
from daimon.core.errors import DaimonError, StaleChannelValue
from daimon.core.neutral_resources import NeutralResourceHost
from daimon.core.permissions import ChannelRule, channel_rule, runs_only_in
from daimon.core.rule_views import (
    BindingRefusal,
    binding_refusal,
    clear_refusal,
    render_binding_refusal,
)
from daimon.core.scope import ChannelScopeRef, DeploymentDefault
from daimon.core.stores import channel_budgets as budget_store
from daimon.core.stores.access_policy import load_access_policy, lock_access_policy
from daimon.core.stores.channel_changes import WriteHook, record_change, write_channel_default
from daimon.core.stores.domain import BudgetWindow, ChangeSurface, ChannelBudgetRow
from daimon.core.stores.scoped_config_read import get_scope
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

ChannelChangeRefusal = Literal["forbidden", "invalid", "not_found"]


class ChannelChangeRefused(DaimonError):
    """A change the actor may not make, or one a rule refuses; the message is person-facing.

    `reason` is the finer cause a surface may word its own way (a `BindingRefusal`).
    """

    def __init__(
        self, message: str, *, kind: ChannelChangeRefusal, reason: str | None = None
    ) -> None:
        super().__init__(message)
        self.kind: ChannelChangeRefusal = kind
        self.reason = reason


@dataclass(frozen=True)
class ChannelActor:
    """Who changes a channel setting, and from where."""

    tenant_id: uuid.UUID
    account_id: uuid.UUID | None
    subject: Subject
    surface: ChangeSurface
    on_write: WriteHook | None = None
    """Runs in each change's transaction once it is written (a surface's audit row)."""


async def after_write(session: AsyncSession, actor: ChannelActor, changed: bool) -> None:
    if changed and actor.on_write is not None:
        await actor.on_write(session)


@dataclass(frozen=True)
class Expected[T]:
    """The value a caller last read; a change to anything else is refused as stale."""

    value: T


@dataclass(frozen=True)
class BudgetValue:
    """A budget as a caller reads and compares it."""

    limit_usd: Decimal
    window: BudgetWindow
    starts_at: datetime | None = None
    ends_at: datetime | None = None

    @classmethod
    def of(cls, row: ChannelBudgetRow | None) -> BudgetValue | None:
        if row is None:
            return None
        return cls(row.limit_usd, row.window, row.starts_at, row.ends_at)

    def as_json(self) -> dict[str, JsonValue]:
        return {
            "limit_usd": f"{self.limit_usd:.2f}",
            "window": self.window,
            "starts_at": self.starts_at.isoformat() if self.starts_at else None,
            "ends_at": self.ends_at.isoformat() if self.ends_at else None,
        }


def _budget_json(value: BudgetValue | None) -> JsonValue:
    return None if value is None else value.as_json()


NEEDS_ADMIN = "Only an admin can change this channel."
CHOOSE_RELEASE_OR_KEEP = "Choose whether this channel's own agents keep their rule or are released."


def require_channel_admin(actor: ChannelActor) -> None:
    """Channel settings are admins' alone; an agent key never counts as one."""
    if not actor.subject.is_admin or actor.subject.via_agent_key:
        raise ChannelChangeRefused(NEEDS_ADMIN, kind="forbidden")


def refused_binding(reason: BindingRefusal, agent_name: str | None) -> ChannelChangeRefused:
    return ChannelChangeRefused(
        f"{render_binding_refusal(reason, agent_name=agent_name)} Nothing was changed.",
        kind="invalid",
        reason=reason,
    )


# ---------- Default agent ----------


@dataclass(frozen=True)
class DefaultChange:
    """The channel's own default agent before and after a change."""

    before: str | None
    after: str | None


async def _own_default(
    session: AsyncSession, *, tenant_id: uuid.UUID, channel_id: str
) -> str | None:
    row = await get_scope(
        session, scope=ChannelScopeRef(tenant_id=tenant_id, channel_id=channel_id)
    )
    return row.agent_name if row is not None else None


async def set_default_agent(
    sessionmaker: async_sessionmaker[AsyncSession],
    actor: ChannelActor,
    *,
    platform: str,
    channel_id: str,
    agent_name: str,
    agent_names: Sequence[str] = (),
    expected: Expected[str | None] | None = None,
) -> DefaultChange:
    """Make `agent_name` the channel's default. `agent_names` are its other names (pins).

    Refused when an agent rule would make it refuse every turn here
    (`binding_refusal`), checked under the policy lock with the write.
    """
    require_channel_admin(actor)
    names = tuple(dict.fromkeys((agent_name, *agent_names)))
    async with sessionmaker.begin() as session:
        await lock_access_policy(session, tenant_id=actor.tenant_id)
        policy = await load_access_policy(session, tenant_id=actor.tenant_id)
        current = await _own_default(session, tenant_id=actor.tenant_id, channel_id=channel_id)
        if expected is not None and expected.value != current:
            raise StaleChannelValue("default_agent", current)
        refusal = binding_refusal(policy, agent_names=names, channel_id=channel_id)
        if refusal is not None:
            raise refused_binding(refusal, agent_name)
        prior = await write_channel_default(
            session,
            tenant_id=actor.tenant_id,
            platform=platform,
            channel_id=channel_id,
            agent_name=agent_name,
            actor_account_id=actor.account_id,
            surface=actor.surface,
            set_by_admin=True,
        )
        await after_write(session, actor, prior != agent_name)
    return DefaultChange(prior, agent_name)


async def clear_default_agent(
    sessionmaker: async_sessionmaker[AsyncSession],
    actor: ChannelActor,
    *,
    platform: str,
    channel_id: str,
    expected: Expected[str | None] | None = None,
) -> DefaultChange:
    """Drop the channel's own default; it falls to the workspace default.

    A channel only its own agents read keeps one of them (`clear_refusal`).
    """
    require_channel_admin(actor)
    async with sessionmaker.begin() as session:
        await lock_access_policy(session, tenant_id=actor.tenant_id)
        policy = await load_access_policy(session, tenant_id=actor.tenant_id)
        current = await _own_default(session, tenant_id=actor.tenant_id, channel_id=channel_id)
        if expected is not None and expected.value != current:
            raise StaleChannelValue("default_agent", current)
        refusal = clear_refusal(policy, channel_id=channel_id)
        if refusal is not None:
            raise refused_binding(refusal, None)
        prior = await write_channel_default(
            session,
            tenant_id=actor.tenant_id,
            platform=platform,
            channel_id=channel_id,
            agent_name=None,
            actor_account_id=actor.account_id,
            surface=actor.surface,
        )
        await after_write(session, actor, prior is not None)
    return DefaultChange(prior, None)


# ---------- Agents kept to the channel ----------


async def keep_agent(
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    actor: ChannelActor,
    *,
    platform: str,
    channel_id: str,
    agent_name: str,
    default: DeploymentDefault,
    expected: Expected[Sequence[str]] | None = None,
) -> AgentRuleChange:
    """Keep `agent_name` to this channel: its rule names the channel alone."""
    require_channel_admin(actor)
    return await _set_kept(
        anthropic,
        sessionmaker,
        actor,
        platform=platform,
        channel_id=channel_id,
        agent_name=agent_name,
        runs_in=(channel_id,),
        default=default,
        expected=expected,
    )


async def release_agent(
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    actor: ChannelActor,
    *,
    platform: str,
    channel_id: str,
    agent_name: str,
    default: DeploymentDefault,
    expected: Expected[Sequence[str]] | None = None,
) -> AgentRuleChange:
    """Stop keeping `agent_name` here: its rule is cleared, so it may run anywhere."""
    require_channel_admin(actor)
    async with sessionmaker() as session:
        policy = await load_access_policy(session, tenant_id=actor.tenant_id)
    kept = sorted(runs_only_in(policy, channel_id))
    if expected is not None and sorted(expected.value) != kept:
        raise StaleChannelValue("kept_agents", kept)
    if agent_name not in kept:
        raise ChannelChangeRefused(f"{agent_name} isn't kept to this channel.", kind="invalid")
    return await _set_kept(
        anthropic,
        sessionmaker,
        actor,
        platform=platform,
        channel_id=channel_id,
        agent_name=agent_name,
        runs_in=None,
        default=default,
        expected=expected,
    )


async def _set_kept(
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    actor: ChannelActor,
    *,
    platform: str,
    channel_id: str,
    agent_name: str,
    runs_in: tuple[str, ...] | None,
    default: DeploymentDefault,
    expected: Expected[Sequence[str]] | None,
) -> AgentRuleChange:
    try:
        return await set_agent_rule(
            anthropic,
            sessionmaker,
            tenant_id=actor.tenant_id,
            platform=platform,
            agent_name=agent_name,
            runs_in=runs_in,
            subject=actor.subject,
            default=default,
            actor_account_id=actor.account_id,
            surface=actor.surface,
            expected_kept=(channel_id, expected.value) if expected is not None else None,
            on_write=actor.on_write,
        )
    except ChannelRuleRefused as exc:
        kind: ChannelChangeRefusal = (
            "forbidden"
            if exc.reason == "admin_required"
            else "not_found"
            if exc.reason == "agent_not_found"
            else "invalid"
        )
        raise ChannelChangeRefused(str(exc), kind=kind, reason=exc.reason) from exc


# ---------- Who can read and post ----------


async def set_access(
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    actor: ChannelActor,
    *,
    platform: str,
    channel_id: str,
    readers: ChannelReaders | None,
    writers: ChannelWriters | None,
    default: DeploymentDefault,
    expected: Expected[ChannelRule] | None = None,
    copy: bool = False,
    copy_from: str | None = None,
    channel_label: str | None = None,
    public_url: str | None = None,
    release_agents: bool = False,
    keep_rules: bool = False,
    resource_host: NeutralResourceHost | None = None,
) -> RuleChange:
    """Set who reads and who posts, with the own-agents copy and release steps
    `set_channel_rule` runs; raises `ChannelRuleRefused` as it does.

    Moving readers off own while agents run here alone needs a choice: `release_agents`
    drops their rules, `keep_rules` keeps them; neither (or both) is refused."""
    if release_agents and keep_rules:
        raise ChannelChangeRefused(CHOOSE_RELEASE_OR_KEEP, kind="invalid", reason="choose")
    if not release_agents and not keep_rules and readers not in (None, "own"):
        async with sessionmaker() as session:
            policy = await load_access_policy(session, tenant_id=actor.tenant_id)
        if channel_rule(policy, channel_id).readers == "own" and runs_only_in(policy, channel_id):
            raise ChannelChangeRefused(CHOOSE_RELEASE_OR_KEEP, kind="invalid", reason="choose")
    return await set_channel_rule(
        anthropic,
        sessionmaker,
        tenant_id=actor.tenant_id,
        platform=platform,
        channel_id=channel_id,
        readers=readers,
        writers=writers,
        subject=actor.subject,
        default=default,
        actor_account_id=actor.account_id,
        copy=copy,
        copy_from=copy_from,
        channel_label=channel_label,
        public_url=public_url,
        release_agents=release_agents,
        resource_host=resource_host,
        surface=actor.surface,
        expected_rule=expected.value if expected is not None else None,
        on_write=actor.on_write,
    )


# ---------- Budget ----------


def _require_budget_admin(actor: ChannelActor, channel_id: str) -> None:
    if not may_set_channel_budget(actor.subject, channel_id):
        raise ChannelChangeRefused(NEEDS_ADMIN, kind="forbidden")


async def _current_budget(
    session: AsyncSession,
    actor: ChannelActor,
    *,
    platform: str,
    channel_id: str,
    expected: Expected[BudgetValue | None] | None,
) -> BudgetValue | None:
    """The budget now, under the tenant lock; stale against `expected` raises."""
    await lock_access_policy(session, tenant_id=actor.tenant_id)
    current = BudgetValue.of(
        await budget_store.get_channel_budget(
            session, tenant_id=actor.tenant_id, platform=platform, channel_id=channel_id
        )
    )
    if expected is not None and expected.value != current:
        raise StaleChannelValue("budget", _budget_json(current))
    return current


async def _record_budget(
    session: AsyncSession,
    actor: ChannelActor,
    *,
    platform: str,
    channel_id: str,
    before: BudgetValue | None,
    after: BudgetValue | None,
) -> None:
    await record_change(
        session,
        tenant_id=actor.tenant_id,
        platform=platform,
        channel_id=channel_id,
        setting="budget",
        before=_budget_json(before),
        after=_budget_json(after),
        actor_account_id=actor.account_id,
        surface=actor.surface,
    )
    await after_write(session, actor, before != after)


async def set_budget(
    sessionmaker: async_sessionmaker[AsyncSession],
    actor: ChannelActor,
    *,
    platform: str,
    channel_id: str,
    spec: BudgetSpec,
    expected: Expected[BudgetValue | None] | None = None,
) -> ChannelBudgetRow:
    """Set or replace the channel's budget (`parse_budget_spec` validated `spec`)."""
    _require_budget_admin(actor, channel_id)
    async with sessionmaker.begin() as session:
        before = await _current_budget(
            session, actor, platform=platform, channel_id=channel_id, expected=expected
        )
        row = await budget_store.set_channel_budget(
            session,
            tenant_id=actor.tenant_id,
            platform=platform,
            channel_id=channel_id,
            limit_usd=spec.limit_usd,
            window=spec.window,
            starts_at=spec.starts_at,
            ends_at=spec.ends_at,
            set_by_account_id=actor.account_id,
        )
        await _record_budget(
            session,
            actor,
            platform=platform,
            channel_id=channel_id,
            before=before,
            after=BudgetValue.of(row),
        )
    return row


async def raise_budget(
    sessionmaker: async_sessionmaker[AsyncSession],
    actor: ChannelActor,
    *,
    platform: str,
    channel_id: str,
    amount_usd: Decimal,
    expected: Expected[BudgetValue | None] | None = None,
) -> ChannelBudgetRow:
    """Add `amount_usd` to the channel's limit; refused when it has no budget."""
    _require_budget_admin(actor, channel_id)
    if amount_usd <= 0:
        raise ChannelChangeRefused("Raise the budget by more than $0.", kind="invalid")
    async with sessionmaker.begin() as session:
        before = await _current_budget(
            session, actor, platform=platform, channel_id=channel_id, expected=expected
        )
        row = await budget_store.raise_channel_budget(
            session,
            tenant_id=actor.tenant_id,
            platform=platform,
            channel_id=channel_id,
            amount_usd=amount_usd,
        )
        if row is None:
            raise ChannelChangeRefused("This channel has no budget to raise.", kind="not_found")
        await _record_budget(
            session,
            actor,
            platform=platform,
            channel_id=channel_id,
            before=before,
            after=BudgetValue.of(row),
        )
    return row


async def clear_budget(
    sessionmaker: async_sessionmaker[AsyncSession],
    actor: ChannelActor,
    *,
    platform: str,
    channel_id: str,
    expected: Expected[BudgetValue | None] | None = None,
) -> bool:
    """Remove the channel's budget; False when it had none."""
    _require_budget_admin(actor, channel_id)
    async with sessionmaker.begin() as session:
        before = await _current_budget(
            session, actor, platform=platform, channel_id=channel_id, expected=expected
        )
        cleared = await budget_store.delete_channel_budget(
            session, tenant_id=actor.tenant_id, platform=platform, channel_id=channel_id
        )
        await _record_budget(
            session, actor, platform=platform, channel_id=channel_id, before=before, after=None
        )
    return cleared


__all__ = [
    "BudgetValue",
    "ChannelActor",
    "ChannelChangeRefusal",
    "ChannelChangeRefused",
    "DefaultChange",
    "Expected",
    "StaleChannelValue",
    "clear_budget",
    "clear_default_agent",
    "keep_agent",
    "raise_budget",
    "release_agent",
    "set_access",
    "set_budget",
    "set_default_agent",
]
