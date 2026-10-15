"""Operator-selected durable Gemini records and conservative attempt holds.

SQLite serializes complete driver transactions across local app workers. The
holds use a separate database so accounting can commit before a native request
inside a driver-record transaction. Accepted/refused/unknown receipts retain
holds until actual accounting reconciliation; a refusal never means a free call.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sqlite3
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import cast
from uuid import uuid4

from daimon.core.turn.errors import AdmissionDenied
from mux.contracts.ids import ModelRef
from mux.drivers.gemini.flash import FLASH_MODELS, FlashAttempt
from mux.drivers.gemini.storage import Records
from pydantic import BaseModel, JsonValue, TypeAdapter

_RECORDS = TypeAdapter(Records)
_JSON = TypeAdapter[JsonValue](JsonValue)


def _encode(value: object) -> JsonValue:
    if isinstance(value, BaseModel):
        return _encode(value.model_dump(mode="python"))
    if isinstance(value, Mapping):
        return {
            "map": [
                [_encode(k), _encode(v)] for k, v in cast(Mapping[object, object], value).items()
            ]
        }
    if isinstance(value, tuple):
        return {"tuple": [_encode(item) for item in cast(tuple[object, ...], value)]}
    if isinstance(value, list):
        return [_encode(item) for item in cast(list[object], value)]
    if isinstance(value, bytes):
        return {"bytes": base64.b64encode(value).decode("ascii")}
    if isinstance(value, datetime):
        return {"datetime": value.isoformat()}
    if isinstance(value, Decimal):
        return {"decimal": str(value)}
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise ValueError("unsupported durable Gemini value")


def _decode(value: JsonValue) -> object:
    if isinstance(value, list):
        return [_decode(item) for item in value]
    if not isinstance(value, dict):
        return value
    if len(value) != 1:
        raise ValueError("invalid durable Gemini value")
    if "map" in value:
        pairs = value["map"]
        if not isinstance(pairs, list):
            raise ValueError("invalid durable Gemini map")
        result: dict[object, object] = {}
        for pair in pairs:
            if not isinstance(pair, list) or len(pair) != 2:
                raise ValueError("invalid durable Gemini pair")
            result[_decode(pair[0])] = _decode(pair[1])
        return result
    if "tuple" in value and isinstance(value["tuple"], list):
        return tuple(_decode(item) for item in value["tuple"])
    if "bytes" in value and isinstance(value["bytes"], str):
        return base64.b64decode(value["bytes"], validate=True)
    if "datetime" in value and isinstance(value["datetime"], str):
        return datetime.fromisoformat(value["datetime"])
    if "decimal" in value and isinstance(value["decimal"], str):
        return Decimal(value["decimal"])
    raise ValueError("invalid durable Gemini tag")


async def _finish(operation: Callable[[], None]) -> None:
    task = asyncio.create_task(asyncio.to_thread(operation))
    try:
        await asyncio.shield(task)
    except BaseException:
        await task
        raise


class _Database:
    def __init__(self, path: Path, namespace: str) -> None:
        if not path.is_absolute():
            raise ValueError("durable Gemini database path must be absolute")
        self.path, self.namespace = path, namespace

    def _open(self) -> tuple[sqlite3.Connection, str | None]:
        # Parent directory is operator-provisioned; never create a fallback.
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(descriptor)
        connection = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        try:
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS payloads "
                "(namespace TEXT PRIMARY KEY, body TEXT NOT NULL)"
            )
            row = connection.execute(
                "SELECT body FROM payloads WHERE namespace=?", (self.namespace,)
            ).fetchone()
            return connection, cast(str, row[0]) if row is not None else None
        except BaseException:
            connection.close()
            raise

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[tuple[sqlite3.Connection, str | None]]:
        opening = asyncio.create_task(asyncio.to_thread(self._open))
        try:
            connection, body = await asyncio.shield(opening)
        except BaseException:
            # Cancellation must not abandon a thread that acquired a DB lock.
            connection, _ = await opening
            await asyncio.to_thread(connection.close)
            raise
        try:
            yield connection, body
        except BaseException:
            await asyncio.to_thread(connection.rollback)
            raise
        finally:
            await asyncio.to_thread(connection.close)

    def save(self, connection: sqlite3.Connection, body: str) -> None:
        connection.execute(
            "INSERT INTO payloads VALUES (?, ?) ON CONFLICT(namespace) "
            "DO UPDATE SET body=excluded.body",
            (self.namespace, body),
        )
        connection.commit()


class SQLiteGeminiStorage:
    """Full Records transactions scoped to the authorized provider account."""

    def __init__(self, path: Path, *, namespace: str) -> None:
        self._database = _Database(path, namespace)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Records]:
        async with self._database.transaction() as (connection, body):
            records = (
                Records()
                if body is None
                else _RECORDS.validate_python(_decode(_JSON.validate_json(body)))
            )
            yield records
            encoded = json.dumps(_encode(_RECORDS.dump_python(records)), allow_nan=False)
            await _finish(lambda: self._database.save(connection, encoded))


class SQLiteFlashAccounting:
    """Reserve explicit operator holds before dispatch; never guess actual cost."""

    def __init__(self, path: Path, *, namespace: str, hold_usd: Decimal, cap_usd: Decimal) -> None:
        if not hold_usd.is_finite() or not cap_usd.is_finite() or not 0 < hold_usd <= cap_usd:
            raise ValueError("positive bounded Gemini holds are required")
        self._database = _Database(path, namespace)
        self._hold, self._cap = hold_usd, cap_usd
        self._pending: ContextVar[str | None] = ContextVar("gemini_attempt_hold", default=None)

    async def _update(self, update: Callable[[dict[str, JsonValue]], None]) -> None:
        async with self._database.transaction() as (connection, body):
            rows = TypeAdapter(dict[str, JsonValue]).validate_json(body) if body is not None else {}
            update(rows)
            await asyncio.to_thread(
                self._database.save, connection, json.dumps(rows, allow_nan=False)
            )

    async def before_attempt(self, *, model: ModelRef, attempt: int) -> None:
        if (
            model.provider != "gemini"
            or model.id not in FLASH_MODELS
            or attempt < 1
            or self._pending.get() is not None
        ):
            raise AdmissionDenied(reason="backend_unsupported")
        id_ = str(uuid4())

        def reserve(rows: dict[str, JsonValue]) -> None:
            held = Decimal(0)
            for row in rows.values():
                if not isinstance(row, dict) or not isinstance(row.get("held_usd"), str):
                    raise ValueError("invalid Gemini hold ledger")
                held += Decimal(cast(str, row["held_usd"]))
            if held + self._hold > self._cap:
                raise AdmissionDenied(reason="channel_budget_exceeded")
            rows[id_] = {
                "model": model.id,
                "attempt": attempt,
                "held_usd": str(self._hold),
                "outcome": "unknown",
                "actual_usd": None,
            }

        await self._update(reserve)
        self._pending.set(id_)

    async def after_attempt(self, *, receipt: FlashAttempt) -> None:
        id_ = self._pending.get()
        if id_ is None:
            raise ValueError("Gemini receipt has no reserved hold")

        def publish(rows: dict[str, JsonValue]) -> None:
            row = rows.get(id_)
            if (
                not isinstance(row, dict)
                or row.get("model") != receipt.model.id
                or row.get("attempt") != receipt.attempt
            ):
                raise ValueError("Gemini receipt differs from reserved hold")
            row.update(
                outcome=receipt.outcome,
                http_status=receipt.http_status,
                interaction_id=receipt.interaction_id,
            )

        await self._update(publish)
        self._pending.set(None)
