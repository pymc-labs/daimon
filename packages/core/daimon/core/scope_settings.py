"""The Settings group: four per-place settings, one reader and one writer for every surface.

`/settings` in Discord, Slack and Teams, the agent's `get_settings` and `change_setting`
tools and the web app's Settings section all read with `read_settings` and write with
`change_setting`, so a setting has the same names, values, levels and rules everywhere.

Each key keeps its storage and its own resolver:

- ``approvals``: the access policy's approval gate (`daimon.core.approval_gate`); the most
  specific level wins both ways. Only a native platform admin, checked live and uncached
  (`approval_admin`), may change it at any level. It takes effect only when the operator
  enables approval overrides; until then it is saved but ignored.
- ``participation`` and ``progress_plans``: the scope tables (`daimon.core.channel_switches`);
  `disabled` is final below the level that sets it. A thread's is any member's, a channel's
  or the server's an admin's.
- ``cost_footer``: the tenant's flag (`daimon.core.stores.tenants`), server level only, an
  admin's (the agent-settings delegated action).

Every change is recorded in `channel_changes`. Pure helpers (`setting_refusal`, the specs)
decide before any read; the writer re-checks under the store's lock.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal, cast, get_args

from daimon.core.access_policy import Approvals
from daimon.core.admin_actions import DelegatedAction
from daimon.core.approval_admin import approval_admin
from daimon.core.approval_gate import (
    ApprovalChange,
    resolve_approval_gate,
    set_approval_gate,
)
from daimon.core.authz import Subject
from daimon.core.channel_changes import ChannelActor, ChannelChangeRefused, Expected, after_write
from daimon.core.channel_switches import SWITCH_PLATFORMS, SwitchTarget, set_switch, switch_refusal
from daimon.core.config import Settings
from daimon.core.errors import DaimonError, StaleChannelValue
from daimon.core.platform_words import NEUTRAL_ADMIN, space_for, words_for
from daimon.core.scope_cascade import CascadeMode, CascadeScope, ResolvedCascade, resolve_modes
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.channel_changes import WORKSPACE_ID, WriteHook, record_change
from daimon.core.stores.domain import CHAT_PLATFORMS, ChangeSurface
from daimon.core.stores.switch_modes import Switch, get_switch_modes
from daimon.core.stores.tenants import get_cost_footer, set_cost_footer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

SettingKey = Literal["approvals", "participation", "progress_plans", "cost_footer"]
SettingLevel = Literal["server", "channel", "thread"]
SettingValue = Literal["on", "off", "disabled", "inherit", "shown", "hidden"]

SETTING_KEYS: Final[tuple[SettingKey, ...]] = get_args(SettingKey)
LEVELS: Final[tuple[SettingLevel, ...]] = get_args(SettingLevel)
INHERIT: Final = "inherit"

_SCOPES: Final[Mapping[SettingLevel, CascadeScope]] = MappingProxyType(
    {
        "server": CascadeScope.WORKSPACE,
        "channel": CascadeScope.CHANNEL,
        "thread": CascadeScope.THREAD,
    }
)


@dataclass(frozen=True)
class SettingSpec:
    """One key of the group: its words, values, levels and where it works."""

    key: SettingKey
    name: str
    """The row's name, the same on every surface."""
    labels: Mapping[str, str]
    """Stored value to the word a person reads, in the order the controls list them."""
    levels: tuple[SettingLevel, ...]
    platforms: frozenset[str]
    admin_action: DelegatedAction | None = None
    """The delegated action an admin for the key's admin levels holds (switches, footer)."""

    @property
    def values(self) -> tuple[str, ...]:
        return tuple(self.labels)

    def label(self, value: str) -> str:
        return self.labels.get(value, value)


_SWITCH_LABELS: Final = MappingProxyType(
    {"on": "On", "off": "Off", "disabled": "Disabled", INHERIT: "Inherit"}
)

SPECS: Final[Mapping[SettingKey, SettingSpec]] = MappingProxyType(
    {
        "approvals": SettingSpec(
            key="approvals",
            name="Approvals",
            labels=MappingProxyType({"on": "Ask", "off": "Allow writes", INHERIT: "Inherit"}),
            levels=LEVELS,
            platforms=frozenset(CHAT_PLATFORMS),
        ),
        "participation": SettingSpec(
            key="participation",
            name="Thread participation",
            labels=_SWITCH_LABELS,
            levels=LEVELS,
            platforms=SWITCH_PLATFORMS["participation"],
            admin_action=DelegatedAction.CHANNEL_PERMISSIONS,
        ),
        "progress_plans": SettingSpec(
            key="progress_plans",
            name="Progress plans",
            labels=_SWITCH_LABELS,
            levels=LEVELS,
            platforms=SWITCH_PLATFORMS["progress_plans"],
        ),
        "cost_footer": SettingSpec(
            key="cost_footer",
            name="Cost footer",
            labels=MappingProxyType({"shown": "Shown", "hidden": "Hidden"}),
            levels=("server",),
            platforms=frozenset(CHAT_PLATFORMS),
            admin_action=DelegatedAction.AGENT_SETTINGS,
        ),
    }
)

OVERRIDES_OFF: Final = "Saved but ignored until the operator enables approval overrides."


@dataclass(frozen=True)
class SettingsPlace:
    """Where settings are read and changed: a channel and, inside it, perhaps a thread.

    `thread_id` is the platform's own thread id (a Slack thread's root timestamp), or None
    when the place is the channel itself. `channel_id` is None only for the server alone.
    """

    platform: str
    channel_id: str | None
    thread_id: str | None = None

    @classmethod
    def of(cls, platform: str, channel_id: str | None, thread_id: str | None) -> SettingsPlace:
        """A place from a turn's or a command's ids; a thread equal to its channel is none."""
        if thread_id is not None and platform == "slack" and ":" in thread_id:
            channel, _, thread_id = thread_id.partition(":")
            channel_id = channel_id or channel
        if not thread_id or thread_id == channel_id or channel_id is None:
            thread_id = None
        return cls(platform, channel_id or None, thread_id)

    def levels(self) -> tuple[SettingLevel, ...]:
        """The levels this place has: always the server, then its channel and thread."""
        found: list[SettingLevel] = ["server"]
        if self.channel_id is not None:
            found.append("channel")
            if self.thread_id is not None:
                found.append("thread")
        return tuple(found)

    @property
    def switch_thread_id(self) -> str | None:
        """The thread's id as the switch rows key it: a Slack thread is ``<channel>:<ts>``."""
        if self.thread_id is None:
            return None
        if self.platform == "slack" and self.channel_id is not None:
            return f"{self.channel_id}:{self.thread_id}"
        return self.thread_id

    def approvals_target(self, level: SettingLevel) -> str | None:
        """The approval gate's rule key for `level`; None is the server."""
        if level == "server":
            return None
        if level == "channel":
            return self.channel_id
        return self.switch_thread_id


@dataclass(frozen=True)
class SettingsDefaults:
    """What a key is when no level sets it: the operator's settings."""

    participation: CascadeMode
    progress_plans: CascadeMode
    approvals_active: bool

    @classmethod
    def from_settings(cls, settings: Settings, platform: str | None) -> SettingsDefaults:
        """Participation is disabled where the platform's adapter is not set up."""
        configured = (
            settings.teams
            if platform == "teams"
            else settings.discord
            if platform == "discord"
            else None
        )
        return cls(
            participation=(
                settings.thread_participation.mode
                if configured is not None
                else CascadeMode.DISABLED
            ),
            progress_plans=settings.progress_plans.mode,
            approvals_active=settings.tool_safety.gate_overrides_active,
        )

    def switch(self, key: Switch) -> CascadeMode:
        return self.participation if key == "participation" else self.progress_plans


@dataclass(frozen=True)
class LevelValue:
    level: SettingLevel
    own: str
    """The level's own value; ``inherit`` when it sets none."""


@dataclass(frozen=True)
class SettingView:
    """One key at one place: every level's own value and what is in effect."""

    key: SettingKey
    name: str
    levels: tuple[LevelValue, ...]
    """The levels the key has at this place, broadest first."""
    effective: str
    effective_label: str
    decided_by: str
    """The level in effect (``server``, ``channel``, ``thread``) or ``default``."""
    available: bool
    """False on a platform the key does not run on."""
    active: bool
    """False for approvals until the operator enables approval overrides."""
    note: str
    """One sentence a person reads: what is in effect and where it comes from."""

    def own(self, level: SettingLevel) -> str | None:
        return next((item.own for item in self.levels if item.level == level), None)


@dataclass(frozen=True)
class SettingChange:
    key: SettingKey
    level: SettingLevel
    before: str
    after: str
    view: SettingView
    """The key at the place after the change."""
    note: str
    """One sentence to read back: what was saved and what is in effect."""


AdminCheck = Callable[[DelegatedAction | None], Awaitable[bool]]


@dataclass(frozen=True)
class SettingsActor:
    """Who changes a setting, from where, and how their standing is checked.

    `native_user_id` is the chat identity whose native admin standing is checked live for
    approvals; None (a web person with no linked chat identity) can never change them.
    `admin_for` answers whether the person is an admin for a delegated action, as the
    surface already decides it (a live platform check, the web role).
    """

    tenant_id: uuid.UUID
    account_id: uuid.UUID | None
    surface: ChangeSurface
    native_user_id: str | None
    admin_for: AdminCheck
    via_agent_key: bool = False
    on_write: WriteHook | None = None


def level_word(level: SettingLevel, platform: str | None) -> str:
    """The level as the platform's people say it: the server is a workspace on Slack."""
    return space_for(platform) if level == "server" else level


def admin_word(platform: str | None) -> str:
    return words_for(platform).admin if platform in CHAT_PLATFORMS else NEUTRAL_ADMIN


def _refused(
    message: str, kind: Literal["forbidden", "invalid"], reason: str
) -> ChannelChangeRefused:
    return ChannelChangeRefused(message, kind=kind, reason=reason)


def setting_refusal(
    key: str, level: str, value: str, *, place: SettingsPlace
) -> ChannelChangeRefused | None:
    """Why `value` cannot be set for `key` at `level` here, before any read; None when it may.

    Checks the names, the platform and the level only: who may change it is the writer's.
    """
    spec = SPECS.get(cast(SettingKey, key))
    if spec is None:
        names = ", ".join(SETTING_KEYS)
        return _refused(
            f"There is no setting called {key}. Choose one of: {names}.", "invalid", "unknown_key"
        )
    if value not in spec.values:
        allowed = ", ".join(spec.values)
        return _refused(f"{spec.name} can be {allowed}.", "invalid", "bad_value")
    if level not in LEVELS:
        return _refused("Choose the server, channel or thread level.", "invalid", "bad_level")
    lvl = level
    if place.platform not in spec.platforms:
        return _refused(
            f"{spec.name} isn't available here, so nothing was changed.",
            "invalid",
            "wrong_platform",
        )
    if lvl not in spec.levels:
        space = space_for(place.platform)
        return _refused(
            f"{spec.name} is set for the whole {space} only. Change it at the {space} level.",
            "invalid",
            "server_only",
        )
    if lvl not in place.levels():
        return _refused(
            f"This conversation has no {level_word(lvl, place.platform)} level to change.",
            "invalid",
            "no_such_level",
        )
    return None


# ---------- Reading ----------


def _approvals_view(
    rules: Mapping[SettingLevel, str],
    gate_scope: str,
    effective: Approvals,
    *,
    place: SettingsPlace,
    defaults: SettingsDefaults,
) -> SettingView:
    spec = SPECS["approvals"]
    levels = tuple(LevelValue(level, rules.get(level, INHERIT)) for level in place.levels())
    shown = "on" if effective == INHERIT else effective
    decided = "default" if effective == INHERIT else gate_scope.split(":", 1)[0]
    note = _effective_note(spec, shown, decided, place.platform)
    if not defaults.approvals_active:
        note = f"{note} {OVERRIDES_OFF}"
    return SettingView(
        key="approvals",
        name=spec.name,
        levels=levels,
        effective=shown,
        effective_label=spec.label(shown),
        decided_by=decided,
        available=place.platform in spec.platforms,
        active=defaults.approvals_active,
        note=note,
    )


def _effective_note(spec: SettingSpec, value: str, decided: str, platform: str | None) -> str:
    label = spec.label(value)
    if decided == "default":
        return f"{spec.name}: {label}, the default."
    if decided == "deployment":
        return f"{spec.name}: {label} for all of Daimon."
    return (
        f"{spec.name}: {label}, set for this {level_word(cast(SettingLevel, decided), platform)}."
    )


def _tier_level(tier: str) -> str:
    return {"workspace": "server", "deployment": "deployment"}.get(tier, tier)


def _switch_view(
    key: Switch,
    resolved: ResolvedCascade,
    own: Mapping[SettingLevel, CascadeMode | None],
    *,
    place: SettingsPlace,
) -> SettingView:
    spec = SPECS[key]
    available = place.platform in spec.platforms
    levels = tuple(
        LevelValue(
            level, INHERIT if own.get(level) is None else cast(CascadeMode, own[level]).value
        )
        for level in place.levels()
    )
    decided = _tier_level(resolved.tier)
    note = (
        _effective_note(spec, resolved.mode.value, decided, place.platform)
        if available
        else f"{spec.name} isn't available here."
    )
    return SettingView(
        key=key,
        name=spec.name,
        levels=levels,
        effective=resolved.mode.value,
        effective_label=spec.label(resolved.mode.value),
        decided_by=decided,
        available=available,
        active=available,
        note=note,
    )


def _footer_view(shown: bool, *, place: SettingsPlace) -> SettingView:
    spec = SPECS["cost_footer"]
    value = "shown" if shown else "hidden"
    return SettingView(
        key="cost_footer",
        name=spec.name,
        levels=(LevelValue("server", value),),
        effective=value,
        effective_label=spec.label(value),
        decided_by="server",
        available=True,
        active=True,
        note=_effective_note(spec, value, "server", place.platform),
    )


async def _read_one(
    session: AsyncSession,
    key: SettingKey,
    *,
    tenant_id: uuid.UUID,
    place: SettingsPlace,
    defaults: SettingsDefaults,
) -> SettingView:
    if key == "cost_footer":
        return _footer_view(await get_cost_footer(session, tenant_id=tenant_id), place=place)
    if key == "approvals":
        policy = await load_access_policy(session, tenant_id=tenant_id)
        gate = resolve_approval_gate(
            policy,
            platform=place.platform,
            channel_id=place.channel_id,
            thread_id=place.thread_id,
        )
        rules: dict[SettingLevel, str] = {"server": policy.approvals}
        for level in ("channel", "thread"):
            target = place.approvals_target(level)
            rule = policy.channel_rules.get(target) if target is not None else None
            if rule is not None:
                rules[level] = rule.approvals
        return _approvals_view(rules, gate.scope, gate.value, place=place, defaults=defaults)
    switch = key
    deployment = defaults.switch(switch)
    modes = await get_switch_modes(
        session,
        switch,
        tenant_id=tenant_id,
        platform=place.platform,
        channel_id=place.channel_id,
        thread_id=place.switch_thread_id,
    )
    own: dict[SettingLevel, CascadeMode | None] = {
        "server": modes.workspace,
        "channel": modes.channel,
        "thread": modes.thread,
    }
    return _switch_view(switch, resolve_modes(deployment, modes), own, place=place)


async def read_settings(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    place: SettingsPlace,
    defaults: SettingsDefaults,
    keys: tuple[SettingKey, ...] = SETTING_KEYS,
) -> tuple[SettingView, ...]:
    """Every key at `place`, in the group's order. Anyone may read them."""
    async with sessionmaker() as session:
        return tuple(
            [
                await _read_one(session, key, tenant_id=tenant_id, place=place, defaults=defaults)
                for key in keys
            ]
        )


# ---------- Changing ----------


def _change_note(
    spec: SettingSpec, level: SettingLevel, value: str, view: SettingView, platform: str
) -> str:
    where = level_word(level, platform)
    saved = (
        f"{spec.name} now inherits for this {where}."
        if value == INHERIT
        else f"{spec.name} is now {spec.label(value)} for this {where}."
    )
    if view.decided_by != level or value == INHERIT:
        saved = f"{saved} {view.note}"
    elif spec.key == "approvals" and not view.active:
        saved = f"{saved} {OVERRIDES_OFF}"
    return saved


async def _require_native_admin(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    actor: SettingsActor,
    platform: str,
) -> Subject:
    """A live, uncached native admin check of the actor's chat identity, or a refusal."""
    if actor.via_agent_key or not actor.native_user_id:
        raise _needs_native_admin(platform)
    subject = await approval_admin(
        sessionmaker, settings, tenant_id=actor.tenant_id, user_id=actor.native_user_id
    )
    if not subject.is_admin:
        raise _needs_native_admin(platform)
    return subject


def _needs_native_admin(platform: str) -> ChannelChangeRefused:
    admin = admin_word(platform)
    return _refused(
        f"Only {admin} can change approvals. Ask {admin}.", "forbidden", "native_admin_required"
    )


def _needs_admin(spec: SettingSpec, platform: str) -> ChannelChangeRefused:
    admin = admin_word(platform)
    return _refused(
        f"Only {admin} can change {spec.name.lower()} here. Ask {admin}.",
        "forbidden",
        "admin_required",
    )


async def may_change_approvals(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    actor: SettingsActor,
    platform: str,
) -> bool:
    """Whether the actor is a native admin right now (live), for a surface's early answer."""
    return (
        await change_refusal(
            sessionmaker, settings, actor, key="approvals", level="server", platform=platform
        )
        is None
    )


async def change_refusal(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    actor: SettingsActor,
    *,
    key: SettingKey,
    level: SettingLevel,
    platform: str,
) -> ChannelChangeRefused | None:
    """Why the actor may not change `key` at `level`, before a card is shown; None when they may.

    `change_setting` decides again when it writes, so this is an early answer only.
    """
    spec = SPECS[key]
    if key == "approvals":
        try:
            await _require_native_admin(sessionmaker, settings, actor, platform)
        except ChannelChangeRefused as exc:
            return exc
        return None
    if level == "thread" and key != "cost_footer":
        return None
    if not await _admin(actor, spec):
        return _needs_admin(spec, platform)
    return None


async def change_setting(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    actor: SettingsActor,
    *,
    key: str,
    level: str,
    value: str,
    place: SettingsPlace,
    expected: Expected[str] | None = None,
) -> SettingChange:
    """Change one key at one level of `place`; refused before any write.

    Raises `ChannelChangeRefused` (the message is for people) or `StaleChannelValue`
    when `expected` is not the level's own value now.
    """
    refusal = setting_refusal(key, level, value, place=place)
    if refusal is not None:
        raise refusal
    spec = SPECS[cast(SettingKey, key)]
    lvl = cast(SettingLevel, level)
    defaults = SettingsDefaults.from_settings(settings, place.platform)
    if spec.key == "approvals":
        before = await _change_approvals(
            sessionmaker, settings, actor, level=lvl, value=value, place=place, expected=expected
        )
    elif spec.key == "cost_footer":
        before = await _change_footer(sessionmaker, actor, spec, value, place, expected)
    else:
        before = await _change_switch(
            sessionmaker, actor, spec, lvl, value, place, defaults, expected
        )
    (view,) = await read_settings(
        sessionmaker, tenant_id=actor.tenant_id, place=place, defaults=defaults, keys=(spec.key,)
    )
    return SettingChange(
        key=spec.key,
        level=lvl,
        before=before,
        after=value,
        view=view,
        note=_change_note(spec, lvl, value, view, place.platform),
    )


async def _change_approvals(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    actor: SettingsActor,
    *,
    level: SettingLevel,
    value: str,
    place: SettingsPlace,
    expected: Expected[str] | None,
) -> str:
    subject = await _require_native_admin(sessionmaker, settings, actor, place.platform)
    try:
        change = await set_approval_gate(
            sessionmaker,
            tenant_id=actor.tenant_id,
            platform=place.platform,
            target=place.approvals_target(level),
            value=cast(Approvals, value),
            subject=subject,
            record=ApprovalChange(
                channel_id=place.channel_id if level != "server" else None,
                thread_id=place.switch_thread_id if level == "thread" else None,
                actor_account_id=actor.account_id,
                surface=actor.surface,
                on_write=actor.on_write,
            ),
            expected=cast(Approvals, expected.value) if expected is not None else None,
        )
    except StaleChannelValue:
        raise
    except DaimonError as exc:
        if isinstance(exc, ChannelChangeRefused):
            raise
        raise _needs_native_admin(place.platform) from exc
    return change.before or INHERIT


async def _admin(actor: SettingsActor, spec: SettingSpec) -> bool:
    return not actor.via_agent_key and await actor.admin_for(spec.admin_action)


async def _change_footer(
    sessionmaker: async_sessionmaker[AsyncSession],
    actor: SettingsActor,
    spec: SettingSpec,
    value: str,
    place: SettingsPlace,
    expected: Expected[str] | None,
) -> str:
    if not await _admin(actor, spec):
        raise _needs_admin(spec, place.platform)
    shown = value == "shown"
    async with sessionmaker.begin() as session:
        before = "shown" if await get_cost_footer(session, tenant_id=actor.tenant_id) else "hidden"
        if expected is not None and expected.value != before:
            raise StaleChannelValue("cost_footer", before)
        await set_cost_footer(session, tenant_id=actor.tenant_id, shown=shown)
        await record_change(
            session,
            tenant_id=actor.tenant_id,
            platform=place.platform,
            channel_id=WORKSPACE_ID,
            setting="cost_footer",
            before=before,
            after=value,
            actor_account_id=actor.account_id,
            surface=actor.surface,
        )
        await after_write(session, _channel_actor(actor, is_admin=True), before != value)
    return before


def _channel_actor(actor: SettingsActor, *, is_admin: bool) -> ChannelActor:
    return ChannelActor(
        tenant_id=actor.tenant_id,
        account_id=actor.account_id,
        subject=Subject(
            is_admin=is_admin,
            via_agent_key=actor.via_agent_key,
            platform_user_id=actor.native_user_id,
        ),
        surface=actor.surface,
        on_write=actor.on_write,
    )


async def _change_switch(
    sessionmaker: async_sessionmaker[AsyncSession],
    actor: SettingsActor,
    spec: SettingSpec,
    level: SettingLevel,
    value: str,
    place: SettingsPlace,
    defaults: SettingsDefaults,
    expected: Expected[str] | None,
) -> str:
    switch = cast(Switch, spec.key)
    scope = _SCOPES[level]
    mode = None if value == INHERIT else CascadeMode(value)
    is_admin = level != "thread" and await _admin(actor, spec)
    channel_actor = _channel_actor(actor, is_admin=is_admin)
    deployment = defaults.switch(switch)
    refusal = switch_refusal(
        switch,
        channel_actor,
        platform=place.platform,
        deployment=deployment,
        scope=scope,
        mode=mode,
    )
    if refusal is not None:
        if refusal.reason == "admin_required":
            raise _needs_admin(spec, place.platform) from refusal
        raise refusal
    if level == "server":
        target = SwitchTarget.workspace()
    elif level == "channel":
        target = SwitchTarget.channel(cast(str, place.channel_id))
    else:
        target = SwitchTarget.thread(cast(str, place.switch_thread_id), place.channel_id)
    change = await set_switch(
        sessionmaker,
        channel_actor,
        switch,
        platform=place.platform,
        deployment=deployment,
        target=target,
        mode=mode,
        expected=(
            Expected(None if expected.value == INHERIT else CascadeMode(expected.value))
            if expected is not None
            else None
        ),
    )
    return INHERIT if change.before is None else change.before.value


__all__ = [
    "INHERIT",
    "LEVELS",
    "OVERRIDES_OFF",
    "SETTING_KEYS",
    "SPECS",
    "AdminCheck",
    "LevelValue",
    "SettingChange",
    "SettingKey",
    "SettingLevel",
    "SettingSpec",
    "SettingValue",
    "SettingView",
    "SettingsActor",
    "SettingsDefaults",
    "SettingsPlace",
    "admin_word",
    "change_setting",
    "level_word",
    "change_refusal",
    "may_change_approvals",
    "read_settings",
    "setting_refusal",
]
