"""Tests for daimon.testing.crypto.make_fernet."""

from __future__ import annotations

import pytest
from cryptography.fernet import InvalidToken, MultiFernet
from daimon.testing.crypto import make_fernet


def test_make_fernet_returns_a_working_multifernet() -> None:
    fernet = make_fernet()
    assert isinstance(fernet, MultiFernet), "credential stores take a MultiFernet"
    assert fernet.decrypt(fernet.encrypt(b"secret")) == b"secret", "must round-trip plaintext"


def test_make_fernet_generates_a_fresh_key_per_call() -> None:
    token = make_fernet().encrypt(b"secret")
    with pytest.raises(InvalidToken):
        make_fernet().decrypt(token)
