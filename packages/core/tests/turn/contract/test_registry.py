"""A turn profile Daimon can run must be in the contract suite."""

from __future__ import annotations

from importlib.metadata import entry_points

from daimon.core.turn.io import _TURN_CODECS  # pyright: ignore[reportPrivateUsage]

from .registry import BACKENDS


def runnable_profiles() -> set[str]:
    """Every profile with turn IO: the built-in one, each installed provider runtime,
    and anything already registered in this process."""
    installed = {entry.name for entry in entry_points(group="daimon.turn_runtimes")}
    return {"anthropic.managed_agents", *installed, *_TURN_CODECS}


def test_every_runnable_profile_has_a_contract_backend() -> None:
    covered = {profile for backend in BACKENDS.values() for profile in backend.profiles}
    missing = runnable_profiles() - covered
    assert not missing, (
        f"turn profiles with no contract backend: {sorted(missing)}. Add a fake under "
        "packages/core/tests/turn/contract/ and an entry in registry.py."
    )


def test_every_pending_and_skipped_case_exists() -> None:
    """A renamed case cannot leave a stale xfail or skip behind."""
    from . import test_contract

    cases = {name.removeprefix("test_") for name in dir(test_contract) if name.startswith("test_")}
    for backend in BACKENDS.values():
        assert set(backend.pending) <= cases, (backend.name, set(backend.pending) - cases)
        assert set(backend.not_applicable) <= cases, backend.name
        assert not set(backend.pending) & set(backend.not_applicable), backend.name
        assert all(reason.strip() for reason in backend.pending.values()), backend.name
