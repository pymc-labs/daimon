"""Shared test fixtures for notebook-host tests."""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def monkeypatch_admin_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set DAIMON_NOTEBOOK__ADMIN_SECRET and clear other DAIMON_NOTEBOOK__ vars.

    Ensures tests start from known defaults without leaking host-env config.
    """
    # Clear any DAIMON_NOTEBOOK__ vars from the process env
    for key in list(os.environ.keys()):
        if key.startswith("DAIMON_NOTEBOOK__"):
            monkeypatch.delenv(key, raising=False)
    # Set the required secret
    monkeypatch.setenv("DAIMON_NOTEBOOK__ADMIN_SECRET", "test-secret")
