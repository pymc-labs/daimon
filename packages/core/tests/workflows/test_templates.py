"""The starter workflows in `defaults/workflows/` validate and tell someone when they finish."""

from __future__ import annotations

from pathlib import Path

from daimon.core.workflows.templates import IDS, list_templates, load_template
from daimon.core.workflows.validate import validate_document

DEFAULTS = Path(__file__).resolve().parents[4] / "defaults"


def test_every_starter_validates_and_delivers_its_result() -> None:
    assert [t.id for t in list_templates(DEFAULTS)] == list(IDS), "every starter is listed"
    for template_id in IDS:
        spec = load_template(DEFAULTS, template_id)
        assert spec is not None, f"{template_id} loads"
        problems = validate_document(spec).errors
        assert problems == [], f"{template_id} validates: {problems}"
        delivered = [n for n in spec["nodes"].values() if n["kind"] == "finish" and "deliver" in n]
        assert delivered, f"{template_id} posts its result when it finishes"
