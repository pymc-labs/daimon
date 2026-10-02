"""Pure Adaptive Card builders for the channel settings dialog. No I/O.

The setup panel lives in the 1:1 chat, so the dialog first asks which
channel, then shows that channel's environment and, for server admins, its
isolation and channel admins. Every submit names its `op` and the channel.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from daimon.core.channel_environments import (
    ENVIRONMENT_OPTION_INHERIT,
    EnvironmentPicker,
    environment_option_value,
)
from daimon.core.channel_isolation import ChannelIsolationStatus
from daimon.core.channel_isolation_setup import END_ISOLATION_WARNING
from microsoft_teams.cards import (
    Action,
    AdaptiveCard,
    CardElement,
    Choice,
    ChoiceSetInput,
    SubmitAction,
    SubmitData,
    TextBlock,
    TextInput,
)

CHANNEL_DIALOG: Final = "channel_settings"
MAX_CHANNEL_CHOICES: Final = 100
"""Channels the picker lists; a server admin with more types the id instead."""

ChannelOp = Literal["pick", "environment", "isolation", "admins"]
IsolationChoice = Literal["isolate", "copy", "end", "lift"]
ISOLATION_CHOICES: Final[Mapping[IsolationChoice, str]] = {
    "isolate": "Isolate",
    "copy": "Isolate with a copy",
    "end": "End isolation",
    "lift": "Lift seal and pins",
}
ISOLATION_NOTE: Final = (
    "Isolating makes the channel private, gives it an agent pinned to it alone and hides that "
    "agent everywhere else. Isolate with a copy makes that agent from the one answering now. "
    "Lift seal and pins also ends isolation and lets its agents answer elsewhere."
)
CHANNEL_ADMINS_NOTE: Final = (
    "Channel admins pick their channels' environment and default agent. Server admins run "
    "every channel. Enter Entra object ids, separated by commas; leave it empty to remove them."
)


@dataclass(frozen=True)
class ChannelSettings:
    """One channel as the dialog shows it to one caller."""

    channel_id: str
    label: str
    picker: EnvironmentPicker | None
    """None when there is nothing to pick, or the caller may not."""
    isolation: ChannelIsolationStatus | None
    """None for a caller who is not a server admin."""
    admin_user_ids: tuple[str, ...] | None
    """None for a caller who is not a server admin."""


def _text(text: str, *, bold: bool = False, subtle: bool = False) -> TextBlock:
    return TextBlock(
        text=re.sub(r"\n+", "\n\n", text),
        wrap=True,
        weight="Bolder" if bold else None,
        is_subtle=subtle or None,
        size="Small" if subtle else None,
    )


def _submit(title: str, op: ChannelOp, channel_id: str | None = None) -> SubmitAction:
    data: dict[str, str] = {"op": op}
    if channel_id is not None:
        data["channel"] = channel_id
    return SubmitAction(title=title, data=SubmitData(CHANNEL_DIALOG, data))


def channel_label(channel_id: str, name: str | None) -> str:
    return f"{name} ({channel_id})" if name else f"Channel {channel_id}"


def channel_picker_form(
    channels: Mapping[str, str | None], *, free_entry: bool, notice: str | None = None
) -> AdaptiveCard:
    """Which channel to change: the ones the caller may, or (server admins) any id."""
    body: list[CardElement] = [_text(notice)] if notice else []
    shown = list(channels.items())[:MAX_CHANNEL_CHOICES]
    if shown:
        choices = [Choice(title=channel_label(c, n), value=c) for c, n in shown]
        body.append(
            ChoiceSetInput(id="channel", label="Channel", value=shown[0][0], choices=choices)
        )
    if free_entry:
        body.append(
            TextInput(
                id="channel_id",
                label="Or a channel id",
                placeholder="19:…@thread.tacv2, or a thread's id",
            )
        )
    return AdaptiveCard(
        body=body, actions=[_submit("Next", "pick")], fallback_text="Channel settings"
    )


def _environment_section(settings: ChannelSettings) -> tuple[list[CardElement], list[Action]]:
    picker = settings.picker
    if picker is None:
        return [_text("**Environment**\n\nNo environment to pick.", subtle=True)], []
    current = picker.own or f"the default ({picker.inherited or 'none'})"
    choices = [Choice(title="Use the default", value=ENVIRONMENT_OPTION_INHERIT)]
    choices += [Choice(title=name, value=environment_option_value(name)) for name in picker.names]
    value = environment_option_value(picker.own) if picker.own else ENVIRONMENT_OPTION_INHERIT
    body: list[CardElement] = [
        _text(f"**Environment**\n\nRuns in {current}."),
        ChoiceSetInput(id="environment", label="Environment", value=value, choices=choices),
    ]
    return body, [_submit("Save environment", "environment", settings.channel_id)]


def _isolation_section(status: ChannelIsolationStatus) -> list[CardElement]:
    dedicated = ", ".join(status.dedicated_agent_names) or "none"
    state = "is isolated" if status.is_hidden else "is not isolated"
    offered: list[IsolationChoice] = ["end"] if status.is_hidden else ["isolate", "copy"]
    if status.is_liftable:
        offered.append("lift")
    notes = [ISOLATION_NOTE]
    if status.is_hidden:
        notes.append(f"Ending isolation: {END_ISOLATION_WARNING}")
    return [
        _text(
            f"**Isolation**\n\nThe channel {state}. Private: "
            f"{'yes' if status.is_private else 'no'} · Dedicated agent: {dedicated}"
        ),
        ChoiceSetInput(
            id="isolation",
            label="Change",
            value=offered[0],
            choices=[Choice(title=ISOLATION_CHOICES[c], value=c) for c in offered],
        ),
        _text(" ".join(notes), subtle=True),
    ]


def channel_settings_form(settings: ChannelSettings, *, notice: str | None = None) -> AdaptiveCard:
    """The channel's settings, each with its own save; isolation and admins for server admins."""
    body: list[CardElement] = [_text(notice)] if notice else []
    body.append(_text(settings.label, bold=True))
    environment, actions = _environment_section(settings)
    body += environment
    channel = settings.channel_id
    if settings.isolation is not None:
        body += _isolation_section(settings.isolation)
        actions.append(_submit("Apply isolation", "isolation", channel))
    if settings.admin_user_ids is not None:
        body += [
            _text("**Channel admins**"),
            TextInput(
                id="admins",
                label="Entra object ids",
                value=", ".join(settings.admin_user_ids),
                placeholder="none",
            ),
            _text(CHANNEL_ADMINS_NOTE, subtle=True),
        ]
        actions.append(_submit("Save channel admins", "admins", channel))
    actions.append(_submit("Another channel", "pick"))
    return AdaptiveCard(body=body, actions=actions, fallback_text=settings.label)


def parse_admin_ids(text: str) -> tuple[str, ...]:
    """The ids typed in the admins field, split on commas and whitespace. Pure."""
    return tuple(part for part in re.split(r"[\s,;]+", text) if part)


def visible_channels(
    listed: Mapping[str, str | None], extra: Sequence[str]
) -> dict[str, str | None]:
    """Listed channels by name, then any `extra` id not listed. Pure."""
    channels = dict(sorted(listed.items(), key=lambda item: (item[1] or "~", item[0])))
    for channel_id in extra:
        channels.setdefault(channel_id, None)
    return channels
