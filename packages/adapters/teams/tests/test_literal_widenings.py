"""Core's `Platform` and `init_sentry`'s `process` Literals accept "teams" at runtime."""

from __future__ import annotations

import inspect
import uuid
from datetime import UTC, datetime
from typing import Literal

import pytest
from daimon.core.observability import init_sentry
from daimon.core.stores.domain import PlatformPrincipalRow
from pydantic import TypeAdapter, ValidationError


def _process_annotation() -> object:
    """`init_sentry`'s `process` Literal, evaluated alone (the rest of the signature can't be)."""
    annotation = inspect.signature(init_sentry).parameters["process"].annotation
    if isinstance(annotation, str):
        annotation = eval(annotation, {"Literal": Literal})
    return annotation


def test_teams_platform_round_trips_through_platform_principal_row() -> None:
    row = PlatformPrincipalRow.model_validate(
        {
            "id": uuid.uuid4(),
            "tenant_id": uuid.uuid4(),
            "platform": "teams",
            "external_id": str(uuid.UUID(int=2)),
            "account_id": uuid.uuid4(),
            "created_at": datetime.now(UTC),
        }
    )
    assert row.platform == "teams"


def test_teams_process_round_trips_through_init_sentry_annotation() -> None:
    adapter: TypeAdapter[str] = TypeAdapter(_process_annotation())
    assert adapter.validate_python("teams") == "teams"


def test_teams_process_round_trip_is_runtime_checked() -> None:
    """A value outside the Literal raises, so the round trip above is a real check."""
    adapter: TypeAdapter[str] = TypeAdapter(_process_annotation())
    with pytest.raises(ValidationError):
        adapter.validate_python("definitely-not-a-process")
