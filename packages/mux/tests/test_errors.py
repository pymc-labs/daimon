"""The error taxonomy."""

from __future__ import annotations

import inspect

from mux import errors
from mux.errors import MuxError, ProviderError


def test_every_error_is_a_mux_error() -> None:
    classes = [c for _, c in inspect.getmembers(errors, inspect.isclass)]
    assert all(issubclass(c, MuxError) for c in classes if issubclass(c, Exception))


def test_provider_error_keeps_the_native_code() -> None:
    error = ProviderError("rate_limited", retryable=True, native_code="429", operation_id="op")
    assert (error.category, error.retryable, error.native_code) == ("rate_limited", True, "429")
    assert "429" in str(error)
