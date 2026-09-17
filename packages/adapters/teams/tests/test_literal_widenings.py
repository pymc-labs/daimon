"""Round-trip proofs for the two core Literal widenings.

``Platform`` gains ``"teams"`` so identity/mapping rows can name the new
adapter, and ``init_sentry``'s ``process`` Literal gains it so the adapter's
process can report under its own name. Both are validated at runtime by
Pydantic — dropping either Literal makes these tests fail with
``ValidationError``, which is the self-falsification receipt for STATUS.
"""

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
    """The ``process`` Literal off ``init_sentry``'s signature.

    ``get_type_hints`` cannot resolve the full signature — ``Integration``
    is a TYPE_CHECKING import — so only the parameter under test is
    evaluated.
    """
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
            "external_id": "66666666-7777-8888-9999-00000000000a",
            "account_id": uuid.uuid4(),
            "created_at": datetime.now(UTC),
        }
    )
    assert row.platform == "teams"


def test_teams_process_round_trips_through_init_sentry_annotation() -> None:
    adapter: TypeAdapter[str] = TypeAdapter(_process_annotation())
    assert adapter.validate_python("teams") == "teams"


def test_teams_process_round_trip_is_runtime_checked() -> None:
    """The falsification hook: a value NOT in the Literal raises ValidationError.

    If ``"teams"`` is reverted from the process Literal, the round-trip test
    above fails identically — same adapter, same code path.
    """
    adapter: TypeAdapter[str] = TypeAdapter(_process_annotation())
    with pytest.raises(ValidationError):
        adapter.validate_python("definitely-not-a-process")
