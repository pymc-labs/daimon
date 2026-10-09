"""Mux errors. Failures propagate as exceptions, never sentinel returns.

The taxonomy lives in `mux.contracts.errors`; this module is its stable
import path.
"""

from __future__ import annotations

from mux.contracts.errors import (
    PROVIDER_ERROR_CATEGORIES,
    BindingConflict,
    ContinuityLost,
    ExtensionVersionError,
    InvalidConfig,
    MigrationUnsupported,
    MuxError,
    OperationConflict,
    ProviderError,
    ProviderErrorCategory,
    ScopeViolation,
    UnsupportedCapability,
)

__all__ = [
    "PROVIDER_ERROR_CATEGORIES",
    "BindingConflict",
    "ContinuityLost",
    "ExtensionVersionError",
    "InvalidConfig",
    "MigrationUnsupported",
    "MuxError",
    "OperationConflict",
    "ProviderError",
    "ProviderErrorCategory",
    "ScopeViolation",
    "UnsupportedCapability",
]
