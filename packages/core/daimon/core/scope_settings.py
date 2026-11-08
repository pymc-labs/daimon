"""The Settings group: four per-place settings, one reader and one writer for every surface.

`/settings` in Discord, Slack and Teams, the agent's `get_settings` and `change_setting`
tools and the web app all read with `read_settings` and write with `change_setting`.
The chat platforms draw the same `SettingsPanel` (`load_panel`); each control carries one
``key:level:value`` choice, applied by `apply_choice`.
Each key keeps its store and resolver:

- ``approvals``: the access policy's approval gate, most specific level wins. Only a
  native platform admin, checked live and uncached (`approval_admin`), may change it.
  It is saved but ignored until the operator enables approval overrides.
- ``participation`` and ``progress_plans``: the scope tables (`channel_switches`);
  `disabled` is final below its level. A thread's is anyone's, the rest an admin's.
- ``cost_footer``: the tenant's flag, server level only, the agent-settings admin's.

Every change is recorded in `channel_changes`.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal, cast, get_args

from daimon.core.access_policy import Approvals
from daimon.core.admin_actions import DelegatedAction
from daimon.core.approval_admin import approval_admin
from daimon.core.approval_gate import ApprovalChange, resolve_approval_gate, set_approval_gate
from daimon.core.authz import Subject
from daimon.core.channel_changes import ChannelActor, ChannelChangeRefused, Expected, after_write
from daimon.core.channel_switches import SWITCH_PLATFORMS, SwitchTarget, set_switch
from daimon.core.config import Settings
from daimon.core.errors import StaleChannelValue
from daimon.core.platform_words import NEUTRAL_ADMIN, space_for, words_for
from daimon.core.scope_cascade import CascadeMode, resolve_modes
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.channel_changes import WORKSPACE_ID, WriteHook, record_change
from daimon.core.stores.domain import CHAT_PLATFORMS, ChangeSurface
from daimon.core.stores.identity import find_platform_principal
from daimon.core.stores.switch_modes import Switch, get_switch_modes
from daimon.core.stores.tenants import get_cost_footer, set_cost_footer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

SettingKey = Literal["approvals", "participation", "progress_plans", "cost_footer"]
SettingLevel = Literal["server", "channel", "thread"]
SETTING_KEYS: Final[tuple[SettingKey, ...]] = get_args(SettingKey)
LEVELS: Final[tuple[SettingLevel, ...]] = get_args(SettingLevel)
INHERIT: Final = "inherit"
OVERRIDES_OFF: Final = "Saved but ignored until the operator enables approval overrides."
Sessions = async_sessionmaker[AsyncSession]


_CHAT = frozenset(CHAT_PLATFORMS)


@dataclass(frozen=True)
class SettingSpec:
    """One key: its name, its values (stored value to the word people read), where it works."""

    key: SettingKey
    name: str
    labels: Mapping[str, str]
    levels: tuple[SettingLevel, ...] = LEVELS
    platforms: frozenset[str] = _CHAT
    admin_action: DelegatedAction | None = None

    @property
    def values(self) -> tuple[str, ...]:
        return tuple(self.labels)

    def label(self, value: str) -> str:
        return self.labels.get(value, value)


_SWITCH = {"on": "On", "off": "Off", "disabled": "Disabled", INHERIT: "Inherit"}
SPECS: Final[Mapping[SettingKey, SettingSpec]] = {
    spec.key: spec
    for spec in (
        SettingSpec(
            "approvals", "Approvals", {"on": "Ask", "off": "Allow writes", INHERIT: "Inherit"}
        ),
        SettingSpec(
            "participation",
            "Thread participation",
            _SWITCH,
            platforms=SWITCH_PLATFORMS["participation"],
            admin_action=DelegatedAction.CHANNEL_PERMISSIONS,
        ),
        SettingSpec(
            "progress_plans",
            "Progress plans",
            _SWITCH,
            platforms=SWITCH_PLATFORMS["progress_plans"],
        ),
        SettingSpec(
            "cost_footer",
            "Cost footer",
            {"shown": "Shown", "hidden": "Hidden"},
            ("server",),
            admin_action=DelegatedAction.AGENT_SETTINGS,
        ),
    )
}


@dataclass(frozen=True)
class SettingsPlace:
    """A channel and perhaps a thread in it (a Slack thread is its root timestamp)."""

    platform: str
    channel_id: str | None
    thread_id: str | None = None

    @classmethod
    def of(cls, platform: str, channel_id: str | None, thread_id: str | None) -> SettingsPlace:
        """From a turn's or a command's ids; a thread equal to its channel is none."""
        if thread_id and platform == "slack" and ":" in thread_id:
            channel, _, thread_id = thread_id.partition(":")
            channel_id = channel_id or channel
        if not thread_id or thread_id == channel_id or not channel_id:
            thread_id = None
        return cls(platform, channel_id or None, thread_id)

    def levels(self) -> tuple[SettingLevel, ...]:
        return LEVELS[: 1 + (self.channel_id is not None) + (self.thread_id is not None)]

    @property
    def thread_key(self) -> str | None:
        """The thread as the stores key it: a Slack thread is ``<channel>:<ts>``."""
        if self.thread_id is None or self.platform != "slack":
            return self.thread_id
        return f"{self.channel_id}:{self.thread_id}"

    def target(self, level: SettingLevel) -> str | None:
        """The approval gate's rule key for `level`; None is the server."""
        return {"server": None, "channel": self.channel_id, "thread": self.thread_key}[level]


def switch_default(settings: Settings, key: Switch, platform: str) -> CascadeMode:
    """A switch where no level sets it; participation is off where its adapter is not set up."""
    if key == "progress_plans":
        return settings.progress_plans.mode
    adapter = {"teams": settings.teams, "discord": settings.discord}.get(platform)
    return settings.thread_participation.mode if adapter is not None else CascadeMode.DISABLED


@dataclass(frozen=True)
class LevelValue:
    level: SettingLevel
    own: str
    """The level's own value; ``inherit`` when it sets none."""


@dataclass(frozen=True)
class SettingView:
    """One key at one place: each level's own value and what is in effect."""

    key: SettingKey
    name: str
    levels: tuple[LevelValue, ...]
    effective: str
    effective_label: str
    decided_by: str
    """``server``, ``channel``, ``thread``, ``deployment`` or ``default``."""
    available: bool
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
    note: str
    """What was saved and what is in effect, to read back."""


@dataclass(frozen=True)
class SettingsActor:
    """Who changes a setting. `native_user_id` is the chat identity checked live for
    approvals (None, a web person with no linked chat account, never may); `admin_for`
    answers for a delegated action as the surface decides it."""

    tenant_id: uuid.UUID
    account_id: uuid.UUID | None
    surface: ChangeSurface
    native_user_id: str | None
    admin_for: Callable[[DelegatedAction | None], Awaitable[bool]]
    via_agent_key: bool = False
    on_write: WriteHook | None = None


def level_word(level: SettingLevel, platform: str | None) -> str:
    """The level as the platform's people say it: the server is a workspace on Slack."""
    return space_for(platform) if level == "server" else level


def admin_word(platform: str | None) -> str:
    return words_for(platform).admin if platform in CHAT_PLATFORMS else NEUTRAL_ADMIN


async def actor_account(
    sessionmaker: Sessions, *, tenant_id: uuid.UUID, platform: str, user_id: str
) -> uuid.UUID | None:
    """The account a chat person's change is recorded under; never creates one."""
    async with sessionmaker() as session:
        found = await find_platform_principal(
            session, tenant_id=tenant_id, platform=platform, external_id=user_id
        )
    return found.account_id if found is not None else None


async def chat_actor(
    sessionmaker: Sessions,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    user_id: str,
    admin_for: Callable[[DelegatedAction | None], Awaitable[bool]],
) -> SettingsActor:
    """A person pressing a chat control, recorded under their account."""
    account = await actor_account(
        sessionmaker, tenant_id=tenant_id, platform=platform, user_id=user_id
    )
    return SettingsActor(tenant_id, account, "panel", user_id, admin_for)


def _refused(
    message: str, reason: str, kind: Literal["forbidden", "invalid"] = "invalid"
) -> ChannelChangeRefused:
    return ChannelChangeRefused(message, kind=kind, reason=reason)


def setting_refusal(
    key: str, level: str, value: str, *, place: SettingsPlace
) -> ChannelChangeRefused | None:
    """Why the names, platform or level rule out the change, before any read; None if not."""
    spec = SPECS.get(cast(SettingKey, key))
    if spec is None:
        return _refused(
            f"There is no setting called {key}. Choose one of: {', '.join(SETTING_KEYS)}.",
            "unknown_key",
        )
    space = space_for(place.platform)
    for failed, message, reason in (
        (value not in spec.values, f"{spec.name} can be {', '.join(spec.values)}.", "bad_value"),
        (level not in LEVELS, "Choose the server, channel or thread level.", "bad_level"),
        (
            place.platform not in spec.platforms,
            f"{spec.name} isn't available here, so nothing was changed.",
            "wrong_platform",
        ),
        (
            level not in spec.levels,
            f"{spec.name} is set for the whole {space} only. Change it at the {space} level.",
            "server_only",
        ),
        (
            level not in place.levels(),
            f"This conversation has no {level_word(cast(SettingLevel, level), place.platform)}"
            " level to change.",
            "no_such_level",
        ),
    ):
        if failed:
            return _refused(message, reason)
    return None


# ---------- Reading ----------


def _effective_note(spec: SettingSpec, value: str, decided: str, platform: str) -> str:
    label = spec.label(value)
    if decided == "default":
        return f"{spec.name}: {label}, the default."
    if decided == "deployment":
        return f"{spec.name}: {label} for all of Daimon."
    return (
        f"{spec.name}: {label}, set for this {level_word(cast(SettingLevel, decided), platform)}."
    )


async def _read_one(
    session: AsyncSession,
    key: SettingKey,
    *,
    tenant_id: uuid.UUID,
    place: SettingsPlace,
    settings: Settings,
) -> SettingView:
    """Each key's own resolver gives its levels' own values, what is in effect and why."""
    own: Mapping[str, str | None]
    if key == "cost_footer":
        effective = "shown" if await get_cost_footer(session, tenant_id=tenant_id) else "hidden"
        own, decided = {"server": effective}, "server"
    elif key == "approvals":
        policy = await load_access_policy(session, tenant_id=tenant_id)
        gate = resolve_approval_gate(
            policy, platform=place.platform, channel_id=place.channel_id, thread_id=place.thread_id
        )
        rules = {level: policy.channel_rules.get(place.target(level) or "") for level in LEVELS}
        own = {level: rule.approvals if rule else None for level, rule in rules.items()}
        own = {**own, "server": policy.approvals}
        inherited = gate.value == INHERIT
        effective = "on" if inherited else gate.value
        decided = "default" if inherited else gate.scope.split(":", 1)[0]
    else:
        modes = await get_switch_modes(
            session,
            key,
            tenant_id=tenant_id,
            platform=place.platform,
            channel_id=place.channel_id,
            thread_id=place.thread_key,
        )
        own = {
            level: mode.value if mode else None
            for level, mode in zip(
                LEVELS, (modes.workspace, modes.channel, modes.thread), strict=True
            )
        }
        resolved = resolve_modes(switch_default(settings, key, place.platform), modes)
        effective = resolved.mode.value
        decided = {"workspace": "server"}.get(resolved.tier, resolved.tier)
    spec = SPECS[key]
    available = place.platform in spec.platforms
    active = settings.tool_safety.gate_overrides_active if key == "approvals" else available
    note = (
        _effective_note(spec, effective, decided, place.platform)
        if available
        else f"{spec.name} isn't available here."
    )
    return SettingView(
        key=key,
        name=spec.name,
        levels=tuple(
            LevelValue(level, own.get(level) or INHERIT)
            for level in place.levels()
            if level in spec.levels
        ),
        effective=effective,
        effective_label=spec.label(effective),
        decided_by=decided,
        available=available,
        active=active,
        note=note if active or not available else f"{note} {OVERRIDES_OFF}",
    )


async def read_settings(
    sessionmaker: Sessions,
    *,
    tenant_id: uuid.UUID,
    place: SettingsPlace,
    settings: Settings,
    keys: tuple[SettingKey, ...] = SETTING_KEYS,
) -> tuple[SettingView, ...]:
    """Every key at `place`, in the group's order. Anyone may read them."""
    async with sessionmaker() as session:
        return tuple(
            [
                await _read_one(session, key, tenant_id=tenant_id, place=place, settings=settings)
                for key in keys
            ]
        )


# ---------- Changing ----------


def _needs_admin(spec: SettingSpec, platform: str) -> ChannelChangeRefused:
    admin = admin_word(platform)
    what = "approvals" if spec.key == "approvals" else f"{spec.name.lower()} here"
    reason = "native_admin_required" if spec.key == "approvals" else "admin_required"
    return _refused(f"Only {admin} can change {what}. Ask {admin}.", reason, "forbidden")


async def _native_admin(
    sessionmaker: Sessions, settings: Settings, actor: SettingsActor, platform: str
) -> Subject:
    """A live, uncached native admin check of the actor's chat identity, or a refusal."""
    if not actor.via_agent_key and actor.native_user_id:
        subject = await approval_admin(
            sessionmaker, settings, tenant_id=actor.tenant_id, user_id=actor.native_user_id
        )
        if subject.is_admin:
            return subject
    raise _needs_admin(SPECS["approvals"], platform)


async def _admin(actor: SettingsActor, spec: SettingSpec) -> bool:
    return not actor.via_agent_key and await actor.admin_for(spec.admin_action)


async def change_refusal(
    sessionmaker: Sessions,
    settings: Settings,
    actor: SettingsActor,
    *,
    key: SettingKey,
    level: SettingLevel,
    platform: str,
) -> ChannelChangeRefused | None:
    """Why the actor may not change `key` at `level`, an early answer before a card or a
    control is shown; `change_setting` decides again when it writes."""
    spec = SPECS[key]
    try:
        if key == "approvals":
            await _native_admin(sessionmaker, settings, actor, platform)
        elif (level != "thread" or key == "cost_footer") and not await _admin(actor, spec):
            raise _needs_admin(spec, platform)
    except ChannelChangeRefused as exc:
        return exc
    return None


async def change_setting(
    sessionmaker: Sessions,
    settings: Settings,
    actor: SettingsActor,
    *,
    key: str,
    level: str,
    value: str,
    place: SettingsPlace,
    expected: Expected[str] | None = None,
) -> SettingChange:
    """Change one key at one level of `place`, refused before any write.

    Raises `ChannelChangeRefused` (its message is for people) or `StaleChannelValue` when
    `expected` is not the level's own value now."""
    if (refusal := setting_refusal(key, level, value, place=place)) is not None:
        raise refusal
    spec, lvl = SPECS[cast(SettingKey, key)], cast(SettingLevel, level)
    if spec.key == "approvals":
        subject = await _native_admin(sessionmaker, settings, actor, place.platform)
        gate = await set_approval_gate(
            sessionmaker,
            tenant_id=actor.tenant_id,
            platform=place.platform,
            target=place.target(lvl),
            value=cast(Approvals, value),
            subject=subject,
            record=ApprovalChange(
                place.channel_id if lvl != "server" else None,
                place.thread_key if lvl == "thread" else None,
                actor.account_id,
                actor.surface,
                actor.on_write,
            ),
            expected=cast(Approvals, expected.value) if expected is not None else None,
        )
        before = gate.before or INHERIT
    elif spec.key == "cost_footer":
        before = await _change_footer(sessionmaker, actor, spec, value, place, expected)
    else:
        before = await _change_switch(
            sessionmaker, actor, spec, lvl, value, place, settings, expected
        )
    (view,) = await read_settings(
        sessionmaker, tenant_id=actor.tenant_id, place=place, settings=settings, keys=(spec.key,)
    )
    where = level_word(lvl, place.platform)
    note = (
        f"{spec.name} now inherits for this {where}."
        if value == INHERIT
        else f"{spec.name} is now {spec.label(value)} for this {where}."
    )
    if view.decided_by != lvl or value == INHERIT:
        note = f"{note} {view.note}"
    elif not view.active:
        note = f"{note} {OVERRIDES_OFF}"
    return SettingChange(spec.key, lvl, before, value, view, note)


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


async def _change_footer(
    sessionmaker: Sessions,
    actor: SettingsActor,
    spec: SettingSpec,
    value: str,
    place: SettingsPlace,
    expected: Expected[str] | None,
) -> str:
    if not await _admin(actor, spec):
        raise _needs_admin(spec, place.platform)
    async with sessionmaker.begin() as session:
        before = "shown" if await get_cost_footer(session, tenant_id=actor.tenant_id) else "hidden"
        if expected is not None and expected.value != before:
            raise StaleChannelValue("cost_footer", before)
        await set_cost_footer(session, tenant_id=actor.tenant_id, shown=value == "shown")
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


async def _change_switch(
    sessionmaker: Sessions,
    actor: SettingsActor,
    spec: SettingSpec,
    level: SettingLevel,
    value: str,
    place: SettingsPlace,
    settings: Settings,
    expected: Expected[str] | None,
) -> str:
    """`set_switch` applies the switch rules (admin levels, the `disabled` lock)."""
    switch = cast(Switch, spec.key)
    is_admin = level != "thread" and await _admin(actor, spec)
    target = {
        "server": SwitchTarget.workspace,
        "channel": lambda: SwitchTarget.channel(cast(str, place.channel_id)),
        "thread": lambda: SwitchTarget.thread(cast(str, place.thread_key), place.channel_id),
    }[level]()

    def mode(raw: str) -> CascadeMode | None:
        return None if raw == INHERIT else CascadeMode(raw)

    try:
        change = await set_switch(
            sessionmaker,
            _channel_actor(actor, is_admin=is_admin),
            switch,
            platform=place.platform,
            deployment=switch_default(settings, switch, place.platform),
            target=target,
            mode=mode(value),
            expected=Expected(mode(expected.value)) if expected is not None else None,
        )
    except ChannelChangeRefused as exc:
        if exc.reason == "admin_required":
            raise _needs_admin(spec, place.platform) from exc
        raise
    return INHERIT if change.before is None else change.before.value


# ---------- The `/settings` panel every chat platform draws ----------

TITLE: Final = "Settings"
PICK: Final = "Pick a new value below. It applies from the next message."
_KEYS: Final[dict[str, SettingKey]] = {key: key for key in SETTING_KEYS}
_LEVELS: Final[dict[str, SettingLevel]] = {level: level for level in LEVELS}


@dataclass(frozen=True)
class SettingsRow:
    key: SettingKey
    name: str
    summary: str
    """What is in effect and where it comes from, then each level's own value."""
    choices: tuple[tuple[str, str], ...]
    """``(encoded choice, label)``; empty when the viewer may change no level here."""
    placeholder: str


@dataclass(frozen=True)
class SettingsPanel:
    title: str
    subline: str
    rows: tuple[SettingsRow, ...]


def decode_choice(raw: object) -> tuple[SettingKey, SettingLevel, str] | None:
    """A pressed choice, or None when it names no key, level or value of the group."""
    parts = raw.split(":") if isinstance(raw, str) else []
    key, level, value = parts if len(parts) == 3 else ("", "", "")
    found_key, found_level = _KEYS.get(key), _LEVELS.get(level)
    if found_key is None or found_level is None or value not in SPECS[found_key].values:
        return None
    return found_key, found_level, value


def _row(view: SettingView, place: SettingsPlace, *, admin: bool, native: bool) -> SettingsRow:
    spec = SPECS[view.key]

    def may(level: SettingLevel) -> bool:
        """Which controls to show; a press is checked again when it is applied."""
        if view.key == "approvals":
            return native
        return admin or (level == "thread" and view.key != "cost_footer")

    def word(level: SettingLevel) -> str:
        return level_word(level, place.platform).capitalize()

    choices = tuple(
        (f"{view.key}:{item.level}:{value}", f"{word(item.level)}: {spec.label(value)}")
        for item in view.levels
        if view.available and may(item.level)
        for value in spec.values
        if value != item.own and not (item.level == "thread" and value == "disabled")
    )
    own = ". ".join(f"{word(item.level)}: {spec.label(item.own)}" for item in view.levels)
    plain = not view.available or view.key == "cost_footer"
    return SettingsRow(
        view.key,
        view.name,
        view.note if plain else f"{view.note}\n{own}.",
        choices,
        f"Change {view.name.lower()}",
    )


def build_settings_panel(
    views: Sequence[SettingView], *, place: SettingsPlace, admin: bool, native_admin: bool
) -> SettingsPanel:
    """The panel for a viewer who is (or is not) an admin and a native admin. Pure."""
    rows = tuple(_row(view, place, admin=admin, native=native_admin) for view in views)
    who = admin_word(place.platform)
    subline = (
        PICK if any(row.choices for row in rows) else f"Only {who} can change these. Ask {who}."
    )
    return SettingsPanel(TITLE, subline, rows)


async def load_panel(
    sessionmaker: Sessions, settings: Settings, actor: SettingsActor, place: SettingsPlace
) -> SettingsPanel:
    """Read the place's settings and build the panel for this viewer."""
    views = await read_settings(
        sessionmaker,
        tenant_id=actor.tenant_id,
        place=place,
        settings=settings,
    )
    native = await change_refusal(
        sessionmaker, settings, actor, key="approvals", level="server", platform=place.platform
    )
    return build_settings_panel(
        views, place=place, admin=await actor.admin_for(None), native_admin=native is None
    )


async def apply_choice(
    sessionmaker: Sessions,
    settings: Settings,
    actor: SettingsActor,
    raw: object,
    place: SettingsPlace,
) -> tuple[bool, str] | None:
    """Apply a pressed choice: ``(True, what changed)``, ``(False, why not)``, or None for a
    forged choice, which changes nothing and says nothing."""
    if (choice := decode_choice(raw)) is None:
        return None
    key, level, value = choice
    try:
        change = await change_setting(
            sessionmaker, settings, actor, key=key, level=level, value=value, place=place
        )
    except (ChannelChangeRefused, StaleChannelValue) as exc:
        return False, str(exc)
    return True, change.note


def moved_to_settings(platform: str) -> str:
    """The pointer a retired control adds to its answer: where the setting lives now."""
    if platform not in ("discord", "slack", "teams"):
        return "Next time, use Settings."
    return f"Next time, {words_for(platform).say('settings', place='channel')}."
