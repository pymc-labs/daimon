"""The Settings group: the registry's features as chat and the web show them.

`/settings` in Discord, Slack and Teams, the agent's `get_settings` and `change_setting`
tools and the web app all read with `read_settings` and write with `change_setting`,
which wrap `daimon.core.features.service` (`read_features`, `change_feature`).
The chat platforms draw the same `SettingsPanel` (`load_panel`); each control carries one
``key:level:value`` choice, applied by `apply_choice`. Levels are the tiers people name:
``server`` (the workspace), ``agent``, ``channel`` and ``thread``. A switch's
``disabled`` is Off plus a Lock, final below its level.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal, cast, get_args

from daimon.core.admin_actions import DelegatedAction
from daimon.core.channel_changes import ChannelChangeRefused, Expected
from daimon.core.config import Settings
from daimon.core.errors import StaleChannelValue
from daimon.core.features.registry import DEFAULT, FEATURES, Tier
from daimon.core.features.resolver import as_cascade
from daimon.core.features.service import (
    FeatureActor,
    FeatureView,
    TierSetting,
    admin_word,
    change_feature,
    read_features,
)
from daimon.core.features.service import change_refusal as feature_change_refusal
from daimon.core.features.snapshot import FeaturePlace, resolve_one
from daimon.core.platform_words import space_for, words_for
from daimon.core.scope_cascade import CascadeMode
from daimon.core.stores.identity import find_platform_principal
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

SettingKey = Literal["approvals", "participation", "progress_plans", "cost_footer"]
SettingLevel = Literal["server", "agent", "channel", "thread"]
SETTING_KEYS: Final[tuple[SettingKey, ...]] = get_args(SettingKey)
LEVELS: Final[tuple[SettingLevel, ...]] = get_args(SettingLevel)
INHERIT: Final = "inherit"
OVERRIDES_OFF: Final = "Saved but ignored until the operator enables approval overrides."
Sessions = async_sessionmaker[AsyncSession]
SettingsActor = FeatureActor
_TIER: Final[dict[SettingLevel, Tier]] = {
    "server": "workspace",
    "agent": "agent",
    "channel": "channel",
    "thread": "thread",
}
_LEVEL: Final[dict[str, SettingLevel]] = {tier: level for level, tier in _TIER.items()}
_SWITCHES: Final = ("participation", "progress_plans")


@dataclass(frozen=True)
class SettingSpec:
    """One key as people see it: its name, its values (stored value to word), its levels."""

    key: SettingKey
    name: str
    labels: Mapping[str, str]
    levels: tuple[SettingLevel, ...]
    platforms: frozenset[str]
    admin_action: DelegatedAction | None = None

    @property
    def values(self) -> tuple[str, ...]:
        return tuple(self.labels)

    def label(self, value: str) -> str:
        return self.labels.get(value, value)


def _spec(key: SettingKey) -> SettingSpec:
    feature = FEATURES[key]
    labels = dict(feature.values)
    if key in _SWITCHES:
        labels = {"on": "On", "off": "Off", "disabled": "Disabled"}
    return SettingSpec(
        key,
        feature.name,
        {**labels, INHERIT: "Inherit"},
        tuple(level for level, tier in _TIER.items() if tier in feature.tiers),
        frozenset(feature.platforms),
        feature.authority.action,
    )


SPECS: Final[Mapping[SettingKey, SettingSpec]] = {key: _spec(key) for key in SETTING_KEYS}


@dataclass(frozen=True)
class SettingsPlace:
    """A channel and perhaps a thread in it (a Slack thread is its root timestamp), and
    the agent answering there when the caller knows it."""

    platform: str
    channel_id: str | None
    thread_id: str | None = None
    agent_id: str | None = None

    @classmethod
    def of(
        cls,
        platform: str,
        channel_id: str | None,
        thread_id: str | None,
        agent_id: str | None = None,
    ) -> SettingsPlace:
        """From a turn's or a command's ids; a thread equal to its channel is none."""
        place = FeaturePlace.of(uuid.UUID(int=0), platform, channel_id, thread_id)
        return cls(platform, place.channel_id, place.thread_id, agent_id)

    def levels(self) -> tuple[SettingLevel, ...]:
        present = (True, self.agent_id is not None, self.channel_id is not None, self.thread_id)
        return tuple(level for level, here in zip(LEVELS, present, strict=True) if here)

    @property
    def thread_key(self) -> str | None:
        """The thread as the stores key it: a Slack thread is ``<channel>:<ts>``."""
        if self.thread_id is None or self.platform != "slack":
            return self.thread_id
        return f"{self.channel_id}:{self.thread_id}"

    def target(self, level: SettingLevel) -> str | None:
        """The approval gate's rule key for `level`; None is the server."""
        return {"channel": self.channel_id, "thread": self.thread_key}.get(level)

    def feature_place(self, tenant_id: uuid.UUID) -> FeaturePlace:
        return FeaturePlace(
            tenant_id, self.platform, self.channel_id, self.thread_id, agent_id=self.agent_id
        )


def switch_default(settings: Settings, key: str, platform: str) -> CascadeMode:
    """A switch where no level sets it: the deployment tier or the registry default; off
    for good where the feature cannot run (participation without its adapter)."""
    place = FeaturePlace(uuid.UUID(int=0), platform)
    return as_cascade(resolve_one(FEATURES[key], settings, place, ())).mode


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
    """``server``, ``agent``, ``channel``, ``thread``, ``deployment`` or ``default``."""
    available: bool
    active: bool
    """False for approvals until the operator enables approval overrides."""
    note: str
    """One sentence a person reads: what is in effect and where it comes from."""
    locked: bool = False
    """True when a level's Lock makes the effective value final below it."""

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


def level_word(level: SettingLevel, platform: str | None) -> str:
    """The level as the platform's people say it: the server is a workspace on Slack."""
    return space_for(platform) if level == "server" else level


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
        (level not in LEVELS, "Choose the server, agent, channel or thread level.", "bad_level"),
        (
            place.platform not in spec.platforms,
            f"{spec.name} isn't available here, so nothing was changed.",
            "wrong_platform",
        ),
        (
            level not in spec.levels,
            f"{spec.name} can't be set for one {level}. Change it for the {space} or a channel.",
            "wrong_level",
        ),
        (
            level not in place.levels(),
            f"This conversation has no {level_word(cast(SettingLevel, level), place.platform)}"
            " level to change.",
            "no_such_level",
        ),
        (
            level == "thread" and value == "disabled",
            f"{spec.name} can't be disabled for one thread. Use off to turn it off here.",
            "thread_disabled",
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


def _legacy(setting: TierSetting) -> str:
    """A tier's own value as people pick it: Off plus a Lock is ``disabled``."""
    if setting.value == DEFAULT:
        return INHERIT
    return "disabled" if setting.locked and setting.value == "off" else setting.value


def _feature_value(value: str) -> tuple[str, bool]:
    """A picked value as (feature value, locked)."""
    if value == "disabled":
        return "off", True
    return (DEFAULT if value == INHERIT else value), False


def _feature_text(value: str) -> str:
    """A picked value as `TierSetting.text`, for `expected`."""
    found, locked = _feature_value(value)
    return f"{found}:locked" if locked else found


def _setting_view(feature: FeatureView, place: SettingsPlace, settings: Settings) -> SettingView:
    key = cast(SettingKey, feature.spec.key)
    spec, resolved = SPECS[key], feature.resolved
    if key in _SWITCHES:
        cascade = as_cascade(resolved)
        effective, decider = cascade.mode.value, cascade.tier
    else:
        effective, decider = resolved.value, resolved.tier
    decided = _LEVEL.get(decider, decider)
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
            LevelValue(_LEVEL[own.tier], _legacy(own))
            for own in feature.tiers
            if own.tier in _LEVEL and _LEVEL[own.tier] in place.levels()
        ),
        effective=effective,
        effective_label=spec.label(effective),
        decided_by=decided,
        available=available,
        active=active,
        note=note if active or not available else f"{note} {OVERRIDES_OFF}",
        locked=resolved.locked,
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
    features = await read_features(
        sessionmaker, settings, place.feature_place(tenant_id), keys=keys
    )
    return tuple(_setting_view(feature, place, settings) for feature in features)


# ---------- Changing ----------


async def change_refusal(
    sessionmaker: Sessions,
    settings: Settings,
    actor: SettingsActor,
    *,
    key: SettingKey,
    level: SettingLevel,
    platform: str,
    agent_id: str | None = None,
) -> ChannelChangeRefused | None:
    """Why the actor may not change `key` at `level`, an early answer before a card or a
    control is shown; `change_setting` decides again when it writes."""
    place = FeaturePlace(actor.tenant_id, platform, agent_id=agent_id)
    return await feature_change_refusal(
        sessionmaker, settings, actor, key=key, tier=_TIER[level], place=place
    )


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
    locked: bool = False,
) -> SettingChange:
    """Change one key at one level of `place`, refused before any write. `locked` (an
    admin's, above the thread level) makes the value final below the level.

    Raises `ChannelChangeRefused` (its message is for people) or `StaleChannelValue` when
    `expected` is not the level's own value now."""
    if (refusal := setting_refusal(key, level, value, place=place)) is not None:
        raise refusal
    spec, lvl = SPECS[cast(SettingKey, key)], cast(SettingLevel, level)
    found, lock = _feature_value(value)
    try:
        change = await change_feature(
            sessionmaker,
            settings,
            actor,
            key=spec.key,
            tier=_TIER[lvl],
            value=found,
            place=place.feature_place(actor.tenant_id),
            locked=lock or locked,
            expected=Expected(_feature_text(expected.value)) if expected is not None else None,
        )
    except StaleChannelValue as exc:
        raise StaleChannelValue(spec.key, _legacy(TierSetting(_TIER[lvl], *_split(exc)))) from exc
    view = _setting_view(change.view, place, settings)
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
    return SettingChange(spec.key, lvl, _legacy(change.before), value, view, note)


def _split(exc: StaleChannelValue) -> tuple[str, bool]:
    text = str(exc.current)
    return text.removesuffix(":locked"), text.endswith(":locked")


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
        return admin or (level == "thread" and FEATURES[view.key].authority.thread_anyone)

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
    plain = not view.available
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
