"""The turn leg a test run selects: one real `TurnSettings` read from the environment.

CI runs the turn-relevant suites on three legs. The default leg sets nothing and
gets the product default (mux, channel backends off). The mux-backends leg sets
``DAIMON_TURN__PATH=mux`` and ``DAIMON_TURN__CHANNEL_BACKENDS=true``. The legacy
leg sets ``DAIMON_TURN__PATH=legacy``. Fixtures that build turn dependencies
(`daimon.testing.turn_fakes.deps_for`, the parity drivers) take their path and
backend flags from `turn_leg()` instead of hard-coding them, so the environment
moves them with the product settings loader and not around it.

A test that only makes sense on one path pins it explicitly, using
`MUX_LEG` / `LEGACY_LEG` (and the ``*_pinned_mux`` / ``*_pinned_legacy`` name
suffix) so a reader sees the pin and the other leg is not silently converted.
"""

from __future__ import annotations

from daimon.core.config import TurnSettings, load_turn_settings
from mux.contracts.ids import Scope

LEGACY_LEG = TurnSettings(path="legacy")
MUX_LEG = TurnSettings(path="mux", channel_backends=True)


def turn_leg() -> TurnSettings:
    """The selected leg, read fresh so a monkeypatched environment is honoured.

    Reads the process environment only: a developer's ``.env`` must not move a
    test run onto a leg the command line did not ask for.
    """
    return load_turn_settings(_env_file=None)


LEG_SCOPE = Scope(
    tenant_id="leg-test-tenant",
    account_id="leg-test-account",
    principal_id="test-host",
    authorization_id="offline-admitted-turn",
)


def leg_scope() -> Scope | None:
    """The authorized scope a direct driver call carries on this leg.

    The mux path refuses a turn without the caller's authorized scope; the legacy
    path takes none. A test that calls the driver directly passes this, so one
    test body exercises whichever path the leg selected.
    """
    return LEG_SCOPE if is_mux_leg() else None


def is_mux_leg() -> bool:
    """True when the run selected the mux path."""
    return turn_leg().path == "mux"
