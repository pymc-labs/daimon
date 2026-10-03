"""Pure Adaptive Card builders for the channel settings dialog. No I/O.

The setup panel lives in the 1:1 chat, so the dialog first asks which
channel, then shows that channel's environment and, for server admins, its
permissions and channel admins. Every submit names its `op` and the channel.
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
from daimon.core.channel_rules import READERS_LABELS, WRITERS_LABELS, ChannelRuleStatus
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

ChannelOp = Literal["pick", "environment", "rule", "admins"]
RuleExtra = Literal["none", "copy", "release"]
RULE_EXTRAS: Final[Mapping[RuleExtra, str]] = {
    "none": "Nothing else",
    "copy": "Keep it to a copy of the agent answering now",
    "release": "Release its agents",
}
PERMISSIONS_NOTE: Final = (
    "Who can read it: anyone; only this channel, so only turns here read its messages and "
    "conversations; or only its own agents, so also only agents kept here run and show here, "
    "and nowhere else. Its default agent becomes one, or a copy of the agent answering now. "
    "Who can post: anyone; only its own agents; or nobody, daimon included. Releasing its "
    "agents lets them run elsewhere, bringing what they remembered here."
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
    rule: ChannelRuleStatus | None
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


def _labelled[T: str](id_: str, label: str, labels: Mapping[T, str], value: T) -> ChoiceSetInput:
    choices = [Choice(title=title, value=key) for key, title in labels.items()]
    return ChoiceSetInput(id=id_, label=label, value=value, choices=choices)


def _rule_section(status: ChannelRuleStatus) -> list[CardElement]:
    extras: list[RuleExtra] = ["none"]
    if status.rule.readers != "own":
        extras.append("copy")
        if status.agents:
            extras.append("release")
    body: list[CardElement] = [
        _text(f"**Permissions**\n\n{status.line}"),
        _labelled("readers", "Who can read it", READERS_LABELS, status.rule.readers),
        _labelled("writers", "Who can post", WRITERS_LABELS, status.rule.writers),
    ]
    if len(extras) > 1:
        body.append(_labelled("extra", "And", {e: RULE_EXTRAS[e] for e in extras}, "none"))
    return [*body, _text(PERMISSIONS_NOTE, subtle=True)]


def channel_settings_form(settings: ChannelSettings, *, notice: str | None = None) -> AdaptiveCard:
    """The channel's settings, each with its own save; permissions and admins for server admins."""
    body: list[CardElement] = [_text(notice)] if notice else []
    body.append(_text(settings.label, bold=True))
    environment, actions = _environment_section(settings)
    body += environment
    channel = settings.channel_id
    if settings.rule is not None:
        body += _rule_section(settings.rule)
        actions.append(_submit("Save permissions", "rule", channel))
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
