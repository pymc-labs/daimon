"""Capability errors name the backend and the missing capability."""

from __future__ import annotations

from mux.errors import CapabilityUnavailableError, MuxError


def test_error_carries_backend_and_capability() -> None:
    err = CapabilityUnavailableError(backend="google", capability="durable_fs")
    assert err.backend == "google"
    assert err.capability == "durable_fs"
    assert "google" in str(err)
    assert isinstance(err, MuxError)
