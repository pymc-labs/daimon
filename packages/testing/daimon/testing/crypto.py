"""Fernet key material for tests that encrypt credentials."""

from __future__ import annotations

from cryptography.fernet import Fernet, MultiFernet


def make_fernet() -> MultiFernet:
    """A one-key `MultiFernet` with a freshly generated key, the shape
    `daimon.core` credential stores take."""
    return MultiFernet([Fernet(Fernet.generate_key())])
