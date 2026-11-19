"""Assert effect sequences against a checked-in JSON recording.

``DAIMON_RECORD_EFFECTS=1`` rewrites the entries a run checks; commit the diff
when a behaviour change is deliberate.
"""

from __future__ import annotations

import dataclasses
import json
import os
from datetime import datetime
from pathlib import Path
from typing import cast
from uuid import UUID

from pydantic import BaseModel


def canon(value: object, ids: dict[UUID, str], epoch: datetime | None = None) -> object:
    """JSON-ready copy: random UUIDs become first-seen labels, sets sort, tuples are lists.

    With ``epoch``, datetimes become whole-second offsets from it.
    """
    if isinstance(value, UUID):
        return ids.setdefault(value, f"uuid{len(ids)}")
    if isinstance(value, datetime):
        return value.isoformat() if epoch is None else f"{(value - epoch).total_seconds():+g}s"
    if isinstance(value, BaseModel):
        return canon(value.model_dump(), ids, epoch)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return canon(dataclasses.asdict(value), ids, epoch)
    if isinstance(value, dict):
        items = cast("dict[object, object]", value).items()
        return {str(canon(k, ids, epoch)): canon(v, ids, epoch) for k, v in items}
    if isinstance(value, set | frozenset):
        members = cast("set[object]", value)
        return sorted((canon(v, ids, epoch) for v in members), key=repr)
    if isinstance(value, list | tuple):
        return [canon(v, ids, epoch) for v in cast("list[object]", value)]
    if value is None or isinstance(value, bool | int | float | str):
        return value
    return repr(value)


class Recording:
    """One JSON file mapping a case key to its recorded effects."""

    def __init__(self, path: Path, *, epoch: datetime | None = None) -> None:
        self.path, self.epoch = path, epoch
        self.record = bool(os.environ.get("DAIMON_RECORD_EFFECTS"))
        self.entries: dict[str, object] = json.loads(path.read_text()) if path.exists() else {}

    def check(self, key: str, effects: object) -> None:
        got = json.loads(json.dumps(canon(effects, {}, self.epoch)))
        if self.record:
            self.entries[key] = got
            lines = (f"{json.dumps(k)}: {json.dumps(v)}" for k, v in sorted(self.entries.items()))
            self.path.write_text("{\n" + ",\n".join(lines) + "\n}\n")
            return
        assert key in self.entries, (
            f"no recorded effects for {key}; record with DAIMON_RECORD_EFFECTS=1"
        )
        assert got == self.entries[key], f"effects for {key} differ from {self.path.name}"
