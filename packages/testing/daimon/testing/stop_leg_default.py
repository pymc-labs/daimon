"""Run the test suites on the emergency-stop leg unless the command line picks one.

Production defaults to mux with channel and agent backends on. The suites that
are not mux-ready by construction (they call drivers without an authorized
scope, or assert legacy wire shapes) keep running exactly as before on the
legacy leg: every turn flag off, as an operator sets it to stop. Loaded from the
root `addopts`, before any settings are read. An explicitly set variable always
wins, so the mux CI leg (and a developer's `DAIMON_TURN__PATH=mux`) still selects
the production default. Tests of the defaults themselves delete these variables.
"""

from __future__ import annotations

import os

STOP_LEG = {
    "DAIMON_TURN__PATH": "legacy",
    "DAIMON_TURN__CHANNEL_BACKENDS": "false",
    "DAIMON_TURN__AGENT_BACKENDS": "false",
}


def pytest_configure() -> None:
    if "DAIMON_TURN__PATH" in os.environ:
        return
    for name, value in STOP_LEG.items():
        os.environ.setdefault(name, value)
