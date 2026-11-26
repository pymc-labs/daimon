"""The backend flags a test run selects: one real `TurnSettings` read from the environment.

Tests run on the code default, backend selection on. A CI leg may turn the
emergency stops off (``DAIMON_TURN__CHANNEL_BACKENDS=false``,
``DAIMON_TURN__AGENT_BACKENDS=false``). Fixtures that build turn dependencies
(`daimon.testing.turn_fakes.deps_for`, the parity drivers) take their backend
flags from `turn_leg()` instead of hard-coding them, so the environment moves
them with the product settings loader and not around it.

A test of behaviour that needs the backends on whatever the leg pins `MUX_LEG`
(and the ``*_pinned_mux`` name suffix) so a reader sees the pin.
"""

from __future__ import annotations

from daimon.core.config import TurnSettings, load_turn_settings
from mux.contracts.ids import Scope

MUX_LEG = TurnSettings(channel_backends=True, agent_backends=True)


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
"""The authorized scope a test that calls the driver directly passes."""
