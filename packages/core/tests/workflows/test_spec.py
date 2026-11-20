from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from daimon.core.workflows.spec import (
    dump_spec,
    parse_duration,
    parse_spec,
    spec_hash,
    spec_json_schema,
)
from pydantic import ValidationError

FIXTURES = Path(__file__).parents[4] / "web/src/modules/workflows/fixtures/validation.json"
SEEDS: list[dict[str, Any]] = [
    c["spec"] for c in json.loads(FIXTURES.read_text())["cases"] if "seed" in c
]


def test_every_seed_spec_dumps_back_to_what_its_author_wrote() -> None:
    assert SEEDS, "the fixtures carry the mock seed specs"
    for document in SEEDS:
        assert dump_spec(parse_spec(document)) == document, f"{document['name']} round trip"


def test_spec_hash_ignores_key_order_but_not_content() -> None:
    document = SEEDS[0]
    reordered = dict(reversed(list(document.items())))
    changed = copy.deepcopy(document) | {"name": "Other"}

    assert spec_hash(parse_spec(reordered)) == spec_hash(parse_spec(document)), "key order"
    assert spec_hash(parse_spec(changed)) != spec_hash(parse_spec(document)), "content"


def test_unknown_fields_and_two_wait_modes_are_refused() -> None:
    document = copy.deepcopy(SEEDS[0]) | {"surprise": True}
    with pytest.raises(ValidationError):
        parse_spec(document)
    wait = {"kind": "wait", "for": "1h", "until": "${{ inputs.at }}"}
    two_modes = copy.deepcopy(SEEDS[0]) | {"nodes": {"pause": wait}, "start": "pause"}
    with pytest.raises(ValidationError, match="exactly one"):
        parse_spec(two_modes)


def test_json_schema_uses_the_authored_names() -> None:
    schema = json.dumps(spec_json_schema())

    for alias in ('"schema"', '"else"', '"for"', '"as"', '"with"', '"maxLength"'):
        assert alias in schema, f"{alias} in the JSON Schema"
    assert "schema_" not in schema and "else_" not in schema, "no Python field names leak"


def test_parse_duration_adds_its_parts() -> None:
    assert parse_duration("1h30m").total_seconds() == 5400, "1h30m"
    assert parse_duration("2d").days == 2, "2d"
