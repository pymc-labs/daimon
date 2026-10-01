"""Capabilities are negotiated once, then read-only."""

from __future__ import annotations

import pytest
from mux.capabilities import Capabilities
from pydantic import ValidationError


def test_frozen_model_rejects_mutation() -> None:
    caps = Capabilities(
        can_steer=True,
        can_schedule=True,
        durable_fs=True,
        self_hosted_sandbox=True,
    )
    with pytest.raises(ValidationError):
        caps.can_steer = False  # type: ignore[misc]


def test_requires_all_four_flags() -> None:
    with pytest.raises(ValidationError):
        Capabilities(can_steer=True)  # type: ignore[call-arg]
