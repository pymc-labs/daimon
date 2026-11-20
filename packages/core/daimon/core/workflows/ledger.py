"""The run ledger's hash chain: `hash = sha256(prev_hash || canonical_json(event))`.

Seq and hashes are assigned under the run row lock (`stores.workflow_run_events`); a
finished run's head goes to the security audit log, so a rewritten ledger shows.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Protocol
from uuid import UUID

from pydantic import JsonValue

GENESIS: Final = "0" * 64


class Entry(Protocol):
    @property
    def seq(self) -> int: ...
    @property
    def at(self) -> datetime: ...
    @property
    def type(self) -> str: ...
    @property
    def step_key(self) -> str | None: ...
    @property
    def actor(self) -> Mapping[str, JsonValue] | None: ...
    @property
    def data(self) -> Mapping[str, JsonValue]: ...
    @property
    def prev_hash(self) -> str: ...
    @property
    def hash(self) -> str: ...


def canonical(value: JsonValue) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def event_hash(
    prev: str,
    *,
    run_id: UUID,
    seq: int,
    at: datetime,
    type: str,
    step: str | None,
    actor: Mapping[str, JsonValue] | None,
    data: Mapping[str, JsonValue],
) -> str:
    body: dict[str, JsonValue] = {
        "run": str(run_id),
        "seq": seq,
        "at": at.isoformat(),
        "type": type,
        "step": step,
        "actor": dict(actor) if actor is not None else None,
        "data": dict(data),
    }
    return hashlib.sha256(prev.encode() + canonical(body)).hexdigest()


@dataclass(frozen=True)
class Verification:
    ok: bool
    head: str
    #: The first event whose seq, link or hash does not hold.
    bad_seq: int | None = None


def verify(run_id: UUID, entries: Sequence[Entry]) -> Verification:
    prev = GENESIS
    for want, e in enumerate(entries, start=1):
        computed = event_hash(
            prev,
            run_id=run_id,
            seq=e.seq,
            at=e.at,
            type=e.type,
            step=e.step_key,
            actor=e.actor,
            data=e.data,
        )
        if e.seq != want or e.prev_hash != prev or e.hash != computed:
            return Verification(False, prev, e.seq)
        prev = e.hash
    return Verification(True, prev)
