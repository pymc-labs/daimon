"""The `/settings` panel as data: rows, choices and words, rendered alike on every platform.

Discord, Slack and Teams each draw this model in their own controls (a select menu per
row, a static select, a set of buttons), so the rows, the choices and the sentences
are the same everywhere. A choice is encoded as ``key:level:value``; a press is decoded
with `decode_choice` and applied with `daimon.core.scope_settings.change_setting`,
which re-checks who may make it.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

from daimon.core.platform_words import words_for
from daimon.core.scope_settings import (
    LEVELS,
    SETTING_KEYS,
    SPECS,
    SettingChange,
    SettingKey,
    SettingLevel,
    SettingsPlace,
    SettingView,
    admin_word,
    level_word,
)

TITLE: Final = "Settings"
MAY_CHANGE_NONE: Final = "Only {admin} can change these. Ask {admin}."
PICK: Final = "Pick a new value below. It applies from the next message."

MayChange = Callable[[SettingKey, SettingLevel], bool]
_KEYS: Final[dict[str, SettingKey]] = {key: key for key in SETTING_KEYS}
_LEVELS: Final[dict[str, SettingLevel]] = {level: level for level in LEVELS}


@dataclass(frozen=True)
class SettingsChoice:
    value: str
    """``key:level:value``, decoded by `decode_choice`."""
    label: str


@dataclass(frozen=True)
class SettingsRow:
    key: SettingKey
    name: str
    summary: str
    """What is in effect and where it comes from, one sentence."""
    choices: tuple[SettingsChoice, ...]
    """Empty when the viewer may change no level of it here."""
    placeholder: str


@dataclass(frozen=True)
class SettingsPanel:
    title: str
    subline: str
    rows: tuple[SettingsRow, ...]


def may_change_for(*, admin: bool, native_admin: bool) -> MayChange:
    """Which controls to show a viewer. A press is checked again when it is applied.

    Approvals are a native admin's; a thread's switches are anyone's; the rest an admin's.
    """

    def may(key: SettingKey, level: SettingLevel) -> bool:
        if key == "approvals":
            return native_admin
        if level == "thread" and key in ("participation", "progress_plans"):
            return True
        return admin

    return may


def encode_choice(key: SettingKey, level: SettingLevel, value: str) -> str:
    return f"{key}:{level}:{value}"


def decode_choice(raw: object) -> tuple[SettingKey, SettingLevel, str] | None:
    """A pressed choice, or None when it names no key, level or value of the group."""
    if not isinstance(raw, str):
        return None
    parts = raw.split(":")
    if len(parts) != 3:
        return None
    key, level, value = parts
    key_ = _KEYS.get(key)
    level_ = _LEVELS.get(level)
    if key_ is None or level_ is None or value not in SPECS[key_].values:
        return None
    return key_, level_, value


def _choices(
    view: SettingView, *, place: SettingsPlace, may_change: MayChange
) -> tuple[SettingsChoice, ...]:
    if not view.available:
        return ()
    spec = SPECS[view.key]
    found: list[SettingsChoice] = []
    for item in view.levels:
        if item.level not in spec.levels or not may_change(view.key, item.level):
            continue
        word = level_word(item.level, place.platform).capitalize()
        for value in spec.values:
            if value == item.own:
                continue
            if item.level == "thread" and value == "disabled":
                continue
            found.append(
                SettingsChoice(
                    encode_choice(view.key, item.level, value), f"{word}: {spec.label(value)}"
                )
            )
    return tuple(found)


def _summary(view: SettingView, place: SettingsPlace) -> str:
    own = [
        f"{level_word(item.level, place.platform).capitalize()}: {SPECS[view.key].label(item.own)}"
        for item in view.levels
        if item.level in SPECS[view.key].levels
    ]
    if not view.available or view.key == "cost_footer":
        return view.note
    return f"{view.note}\n{'. '.join(own)}."


def build_settings_panel(
    views: Sequence[SettingView], *, place: SettingsPlace, may_change: MayChange
) -> SettingsPanel:
    """The panel for one viewer at one place. Pure."""
    rows = tuple(
        SettingsRow(
            key=view.key,
            name=view.name,
            summary=_summary(view, place),
            choices=_choices(view, place=place, may_change=may_change),
            placeholder=f"Change {view.name.lower()}",
        )
        for view in views
    )
    if any(row.choices for row in rows):
        subline = PICK
    else:
        admin = admin_word(place.platform)
        subline = MAY_CHANGE_NONE.format(admin=admin)
    return SettingsPanel(title=TITLE, subline=subline, rows=rows)


def moved_to_settings(platform: str) -> str:
    """The pointer a retired control adds to its answer: where the setting lives now."""
    if platform not in ("discord", "slack", "teams"):
        return "Next time, use Settings."
    said = words_for(platform).say("settings", place="channel")
    return f"Next time, {said}."


def change_text(change: SettingChange) -> str:
    """What a press answers: the change's sentence."""
    return change.note


__all__ = [
    "PICK",
    "TITLE",
    "MayChange",
    "SettingsChoice",
    "SettingsPanel",
    "SettingsRow",
    "build_settings_panel",
    "change_text",
    "decode_choice",
    "encode_choice",
    "may_change_for",
    "moved_to_settings",
]
