"""What a workflow keeps between runs (`workflow.state`), as pure rules.

A Finish merges its `state` into the stored object: top-level keys only, a null value
deletes the key. The object is capped, so a run cannot grow it without end.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Final

from pydantic import JsonValue

STATE_LIMIT: Final = 64 * 1024
#: The workflow facts a template reads that cost a query or a large copy; `workflow.id`
#: and `workflow.name` are always there.
_READS: Final = re.compile(r"""workflow\s*(?:\.|\[\s*["'])\s*(state|last_run)\b""")


def merged(state: Mapping[str, JsonValue], patch: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    out = dict(state)
    for key, value in patch.items():
        if value is None:
            out.pop(key, None)
        else:
            out[key] = value
    return out


def oversized(state: Mapping[str, JsonValue]) -> bool:
    return len(json.dumps(state, ensure_ascii=False).encode()) > STATE_LIMIT


def reads_of(spec_text: str) -> frozenset[str]:
    """Which of `state` and `last_run` a spec's text reads."""
    return frozenset(m.group(1) for m in _READS.finditer(spec_text))
