"""The legacy on/off/disabled words of the per-place switches. `disabled` is Off + Locked;
resolution is `daimon.core.features`."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal


class CascadeMode(StrEnum):
    ON = "on"
    OFF = "off"
    DISABLED = "disabled"


CascadeTier = Literal["deployment", "workspace", "agent", "channel", "thread"]


@dataclass(frozen=True)
class ResolvedCascade:
    mode: CascadeMode
    tier: CascadeTier
    """The tier whose value is in effect: the one to change to get a different answer."""
