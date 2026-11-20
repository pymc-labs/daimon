"""The validator against the fixtures the web canvas also checks (`validation.test.ts`)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from daimon.core.workflows.taint import Catalog
from daimon.core.workflows.validate import validate_document

FIXTURES = Path(__file__).parents[4] / "web/src/modules/workflows/fixtures/validation.json"
CASES: list[dict[str, Any]] = json.loads(FIXTURES.read_text())["cases"]


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_validator_matches_shared_fixture(case: dict[str, Any]) -> None:
    catalog = Catalog.model_validate(case["catalog"]) if "catalog" in case else None
    result = validate_document(case["spec"], catalog)

    errors = sorted({f"{p.code}:{p.node or ''}" for p in result.errors})
    warnings = sorted({f"{p.code}:{p.node or ''}" for p in result.warnings})
    assert errors == case["errors"], f"errors differ: {[p.message for p in result.errors]}"
    assert warnings == case["warnings"], "warnings differ from the fixture"
    if "grants" in case:
        grants = sorted({f"{g.kind}:{g.node or ''}" for g in result.grants})
        assert grants == case["grants"], "grants differ from the fixture"
    if "classes" in case:
        assert result.classes == case["classes"], "Rule of Two classes differ from the fixture"
