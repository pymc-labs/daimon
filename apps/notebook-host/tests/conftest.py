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


@pytest.fixture(autouse=True)
def private_shared_temp_dirs(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Point remove_uid_files at a per-test dir, never the machine's /tmp or /dev/shm."""
    from notebook_host import jail

    monkeypatch.setattr(jail, "SHARED_TEMP_DIRS", (tmp_path_factory.mktemp("shared-tmp"),))


def set_unjailed_test_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Opt a test into the unjailed-spawn break-glass.

    CI runs as an unprivileged user, so ``can_apply_jail()`` is False there
    and the production default (fail-closed, D-05) refuses to spawn — every
    publish would 503 until a test opts out. Call this from any test that is
    exercising something other than the jail itself.

    Deliberately NOT folded into the autouse fixture above: the production
    default must stay observable so `test_settings_defaults_match_documented_values`
    keeps asserting ``allow_unjailed_spawn is False`` against a clean
    environment, and a future test that ought to exercise fail-closed must
    not silently inherit the break-glass. One named call site per test is
    greppable; an invisible global is not.
    """
    monkeypatch.setenv("DAIMON_NOTEBOOK__ALLOW_UNJAILED_SPAWN", "true")
