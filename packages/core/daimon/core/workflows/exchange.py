"""Workflows as files: YAML (JSON reads as YAML too) in and out."""

from __future__ import annotations

import json
import re
from typing import Any, Literal

import yaml
from daimon.core.workflows.views import obj

Format = Literal["yaml", "json"]
#: The order a spec reads in; the database keeps JSON keys in its own order.
_TOP = (
    "schema", "name", "goal", "home", "visibility", "label", "inputs", "triggers",
    "concurrency", "defaults", "limits", "start", "nodes",
)  # fmt: skip


class NotAWorkflow(ValueError):
    """The text is not a workflow file; the message says where and why, for the person."""


def parse(text: str) -> dict[str, Any]:
    try:
        document = yaml.safe_load(text)
    except yaml.MarkedYAMLError as err:
        mark = err.problem_mark
        where = f"Line {mark.line + 1}: " if mark else ""
        raise NotAWorkflow(f"{where}{err.problem or 'This is not valid YAML.'}") from err
    spec = obj(document)
    if not isinstance(document, dict):
        raise NotAWorkflow("This file is not a workflow: it should be a YAML or JSON object.")
    for key in ("name", "start", "nodes"):
        if key not in spec:
            raise NotAWorkflow(f"This file is not a workflow: it has no `{key}`.")
    spec.setdefault("schema", "daimon.workflow/v1")
    return spec


def dump(spec: dict[str, Any], fmt: Format) -> str:
    ordered = {k: spec[k] for k in _TOP if k in spec} | {
        k: v for k, v in spec.items() if k not in _TOP
    }
    if fmt == "json":
        return json.dumps(ordered, indent=2, ensure_ascii=False) + "\n"
    return yaml.safe_dump(ordered, sort_keys=False, allow_unicode=True, width=100)


def filename(name: str, fmt: Format) -> str:
    return f"{re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-') or 'workflow'}.{fmt}"
