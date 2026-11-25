"""Gold sets: questions, who asks and which records answer them. JSON or YAML."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from pydantic import BaseModel, Field


class Caller(BaseModel):
    """Who asks. `account_id` reads as that person (their scopes and own lists, resolved
    from the store); `scopes` and `readers` add to or stand in for it."""

    label: str
    tenant_id: uuid.UUID
    account_id: uuid.UUID | None = None
    scopes: list[str] = Field(default_factory=list[str])
    readers: list[str] = Field(default_factory=list[str])


class Expected(BaseModel):
    """A record that answers the question: `source` is its source's name or id. Grade 2 is
    essential, 1 supporting."""

    source: str
    external_id: str
    grade: int = Field(default=2, ge=1, le=3)


class Question(BaseModel):
    id: str
    q: str
    caller: Caller
    expected: list[Expected]


class GoldSet(BaseModel):
    questions: list[Question]


def load(path: Path) -> GoldSet:
    """A gold file: `{"questions": [...]}` or a bare list, as JSON or YAML."""
    text = path.read_text("utf-8")
    if path.suffix in (".yaml", ".yml"):
        import yaml

        data: object = yaml.safe_load(text)  # pyright: ignore[reportUnknownMemberType]
    else:
        data = json.loads(text)
    return GoldSet.model_validate({"questions": data} if isinstance(data, list) else data)
