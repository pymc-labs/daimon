"""The scope cascade behind per-place switches: deployment, workspace, channel, thread.

Pure and free of I/O. Organic thread participation and progress plans both
resolve their mode here, so the two read the same way to the people who set
them. The most specific explicit value wins, except that `DISABLED` at any
tier is final for every tier below it. That is the difference between `OFF`
(nobody asked yet, but a narrower scope may turn it on) and `DISABLED` (an
admin or operator has said no, and asking does nothing).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from daimon.core.platform_words import words_for
from daimon.core.stores.domain import CHAT_PLATFORMS


class CascadeMode(StrEnum):
    ON = "on"
    OFF = "off"
    DISABLED = "disabled"


class CascadeScope(StrEnum):
    WORKSPACE = "workspace"
    CHANNEL = "channel"
    THREAD = "thread"


CascadeTier = Literal["deployment", "workspace", "channel", "thread"]


@dataclass(frozen=True)
class ResolvedCascade:
    mode: CascadeMode
    tier: CascadeTier
    """The tier whose value is in effect: the one to change to get a different answer."""


@dataclass(frozen=True)
class CascadeModes:
    """The explicit settings at each tier, `None` where nothing was set."""

    workspace: CascadeMode | None
    channel: CascadeMode | None
    thread: CascadeMode | None


def resolve_cascade(
    *,
    deployment: CascadeMode,
    workspace: CascadeMode | None,
    channel: CascadeMode | None,
    thread: CascadeMode | None,
) -> ResolvedCascade:
    """Walk the cascade broadest first: `DISABLED` stops the walk, else the last set value wins."""
    effective = ResolvedCascade(deployment, "deployment")
    tiers: tuple[tuple[CascadeMode | None, CascadeTier], ...] = (
        (workspace, "workspace"),
        (channel, "channel"),
        (thread, "thread"),
    )
    for value, tier in tiers:
        if effective.mode is CascadeMode.DISABLED:
            break
        if value is not None:
            effective = ResolvedCascade(value, tier)
    return effective


def resolve_modes(deployment: CascadeMode, modes: CascadeModes) -> ResolvedCascade:
    """`resolve_cascade` over one store read."""
    return resolve_cascade(
        deployment=deployment,
        workspace=modes.workspace,
        channel=modes.channel,
        thread=modes.thread,
    )


def resolve_above(
    scope: CascadeScope, deployment: CascadeMode, modes: CascadeModes
) -> ResolvedCascade:
    """Resolve only the tiers wider than `scope`: what a write there has to live under."""
    return resolve_cascade(
        deployment=deployment,
        workspace=modes.workspace if scope is not CascadeScope.WORKSPACE else None,
        channel=modes.channel if scope is CascadeScope.THREAD else None,
        thread=None,
    )


def tier_word(tier: str, platform: str | None) -> str:
    """The tier as this platform's people name it: the workspace tier is a server on
    Discord and an organisation on Teams, and the deployment tier is Daimon-wide."""
    if tier == "workspace" and platform in CHAT_PLATFORMS:
        return words_for(platform).space
    return "Daimon-wide" if tier == "deployment" else tier


def scope_label(scope: CascadeScope, scope_id: str | None, platform: str | None = None) -> str:
    return (
        f"the whole {tier_word('workspace', platform)}"
        if scope is CascadeScope.WORKSPACE
        else f"{scope.value} {scope_id}"
    )
