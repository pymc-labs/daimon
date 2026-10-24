"""Domain state and wire grammar for the multi-step wizard.

This module lives in `daimon.core`, not in either adapter, for the same
reason `daimon.core.credential_requests` does: the MCP server (which posts
a wizard's first screen) and the Discord bot (which dispatches every tap on
it) cannot import each other, so the shared encode/decode logic has to sit
one level up.

Discord's `DynamicItem` dispatch matches an incoming `custom_id` with
`pattern.fullmatch`, not a prefix search, and — critically — it fires
*every* registered dynamic-item template whose pattern fullmatches, with no
early break once one has matched. Two overlapping templates therefore
double-dispatch a single tap. That is why `NAV_CUSTOM_ID_PATTERN`,
`SELECT_CUSTOM_ID_PATTERN`, and `SUBMIT_CUSTOM_ID_PATTERN` must be provably
pairwise disjoint, and why `build_custom_id` refuses to mint an id that
neither pattern would route.

There are THREE templates, not one, because three different widget classes
dispatch them in the bot process: ordinary navigation and option buttons
(`NAV_CUSTOM_ID_PATTERN`), the multi-select component (`SELECT_CUSTOM_ID_PATTERN`),
and the submit button (`SUBMIT_CUSTOM_ID_PATTERN`), which is routed
separately so the turn-spawning path has its own isolated dispatch entry
point rather than sharing one with ordinary navigation. A button item and a
select item are different discord.py `DynamicItem` subclasses, so the
button and select grammars in particular must be provably disjoint from
each other: the `_sel` action suffix appears only in
`SELECT_CUSTOM_ID_TEMPLATE` and nowhere in `NAV_CUSTOM_ID_TEMPLATE`, so a
select tap can never accidentally dispatch the button item class (or vice
versa).

The `wz:` prefix is deliberately disjoint from `daimon.core.credential_requests`'
`ztc:` prefix and from the sibling message-feedback feature's `mfb:` prefix
(when that module exists) — no wizard custom_id can ever fullmatch another
feature's dispatch pattern, and no other feature's custom_id can fullmatch
one of these.

A `WizardState.current_step` equal to `len(spec.steps)` (one past the last
real step index) means the review screen — the summary-and-submit screen
shown after every question has an answer, not a step in `spec.steps` itself.
"""

from __future__ import annotations

import re
import secrets
import string
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

WIZARD_CUSTOM_ID_PREFIX: Final[str] = "wz:"
SHORT_ID_LEN: Final[int] = 8
MAX_CUSTOM_ID_CHARS: Final[int] = 100

NAV_CUSTOM_ID_TEMPLATE: Final[str] = (
    r"wz:(?P<short_id>[A-Za-z0-9]{8}):(?P<action>next|back|s\d{1,2}_c\d{1,2}|s\d{1,2}_enter|s\d{1,2}_custom)"
)
NAV_CUSTOM_ID_PATTERN: Final[re.Pattern[str]] = re.compile(NAV_CUSTOM_ID_TEMPLATE)

SELECT_CUSTOM_ID_TEMPLATE: Final[str] = r"wz:(?P<short_id>[A-Za-z0-9]{8}):(?P<action>s\d{1,2}_sel)"
SELECT_CUSTOM_ID_PATTERN: Final[re.Pattern[str]] = re.compile(SELECT_CUSTOM_ID_TEMPLATE)

SUBMIT_CUSTOM_ID_TEMPLATE: Final[str] = r"wz:(?P<short_id>[A-Za-z0-9]{8}):submit"
SUBMIT_CUSTOM_ID_PATTERN: Final[re.Pattern[str]] = re.compile(SUBMIT_CUSTOM_ID_TEMPLATE)


class WizardStatus(StrEnum):
    """Lifecycle of a wizard run, keyed by its short id."""

    OPEN = "open"
    SUBMITTED = "submitted"
    ABANDONED = "abandoned"


class ActionKind(StrEnum):
    """The taps a wizard interaction dispatcher can receive."""

    SELECT_CHOICE = "select_choice"
    SET_MULTI = "set_multi"
    ADD_CUSTOM = "add_custom"
    NEXT = "next"
    BACK = "back"
    SUBMIT = "submit"


@dataclass(frozen=True)
class WizardState:
    """The persisted, platform-neutral state of one wizard run."""

    short_id: str
    current_step: int = 0
    answers: dict[str, list[str]] = field(default_factory=dict[str, list[str]])
    status: WizardStatus = WizardStatus.OPEN


@dataclass(frozen=True)
class WizardAction:
    """A decoded tap or text submission applied against a `WizardState`."""

    kind: ActionKind
    step_index: int | None = None
    values: list[str] = field(default_factory=list[str])
    text: str | None = None


def new_short_id() -> str:
    """Return a fresh `SHORT_ID_LEN`-character base62 short id."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(SHORT_ID_LEN))


def build_custom_id(short_id: str, action: str) -> str:
    """Return the wire `custom_id` for `short_id`/`action`.

    Raises `ValueError` when the result exceeds `MAX_CUSTOM_ID_CHARS` or when
    neither `NAV_CUSTOM_ID_PATTERN`, `SELECT_CUSTOM_ID_PATTERN`, nor
    `SUBMIT_CUSTOM_ID_PATTERN` would fullmatch it — a renderer must never
    emit an id its own dispatcher cannot route.
    """
    custom_id = f"{WIZARD_CUSTOM_ID_PREFIX}{short_id}:{action}"
    if len(custom_id) > MAX_CUSTOM_ID_CHARS:
        raise ValueError(
            f"custom_id {custom_id!r} is {len(custom_id)} characters, "
            f"exceeding the {MAX_CUSTOM_ID_CHARS}-character cap"
        )
    if not (
        NAV_CUSTOM_ID_PATTERN.fullmatch(custom_id)
        or SELECT_CUSTOM_ID_PATTERN.fullmatch(custom_id)
        or SUBMIT_CUSTOM_ID_PATTERN.fullmatch(custom_id)
    ):
        raise ValueError(
            f"action {action!r} does not fullmatch any wizard custom_id grammar "
            f"and would never be dispatched by the bot process"
        )
    return custom_id
