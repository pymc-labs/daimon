"""The legacy on/off/disabled words of the per-place switches. `disabled` is Off + Locked;
resolution is `daimon.core.features`."""

from __future__ import annotations

from enum import StrEnum


class CascadeMode(StrEnum):
    ON = "on"
    OFF = "off"
    DISABLED = "disabled"
