"""The starter workflows: `defaults/workflows/<id>.yaml`, read from the defaults root."""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Any

import yaml
from daimon.core.workflows.views import obj, seq
from pydantic import BaseModel

#: The order the picker shows them in.
IDS = (
    "daily_digest",
    "webhook_triage",
    "approve_send",
    "research_each",
    "review_loop",
    "onboarding",
)


class TemplateInfo(BaseModel):
    id: str
    name: str
    trigger: str
    kinds: list[str]


@cache
def _read(root: Path) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for template_id in IDS:
        path = root / "workflows" / f"{template_id}.yaml"
        if path.is_file():
            found[template_id] = obj(yaml.safe_load(path.read_text(encoding="utf-8")))
    return found


def _kinds(spec: dict[str, Any]) -> list[str]:
    kinds: list[str] = []
    for node in obj(spec.get("nodes")).values():
        for kind in (obj(node).get("kind"), obj(obj(node).get("do")).get("kind")):
            if isinstance(kind, str) and kind not in kinds:
                kinds.append(kind)
    return kinds


def list_templates(root: Path) -> list[TemplateInfo]:
    return [
        TemplateInfo(
            id=template_id,
            name=str(spec["name"]),
            trigger=str(obj(seq(spec.get("triggers"))[0]).get("kind", "manual")),
            kinds=_kinds(spec),
        )
        for template_id, spec in _read(root).items()
    ]


def load_template(root: Path, template_id: str) -> dict[str, Any] | None:
    """A copy of the template's spec, or None for an id that is not one."""
    spec = _read(root).get(template_id)
    return None if spec is None else yaml.safe_load(yaml.safe_dump(spec))
