"""Operator-selected durable Gemini records and conservative attempt holds.

SQLite serializes local workers; PostgreSQL serializes application instances.
Records and holds use independent transactions so accounting commits before a
native request even inside an open driver-record transaction. A hold is released
three ways: a definite 4xx refusal (nothing to bill), a settlement to the measured
cost of its interaction, or, failing both, expiry after HOLD_TTL with an audit
log. Anything else keeps its hold; a refusal never means a free call.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Protocol, cast, runtime_checkable
from uuid import uuid4

import structlog
from daimon.core.stores.gemini_runtime_state import (
    Payload,
    PayloadDatabase,
    PostgresPayloadDatabase,
    complete,
)
from daimon.core.turn.errors import AdmissionDenied
from mux.contracts.ids import ModelRef, Scope
from mux.contracts.usage import UsageObservation
from mux.drivers.gemini import ATTEMPT_MODELS, FlashAttempt, Records
from pydantic import BaseModel, JsonValue, TypeAdapter
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_RECORDS = TypeAdapter(Records)
_JSON = TypeAdapter[JsonValue](JsonValue)
_ROWS = TypeAdapter(dict[str, JsonValue])
log = structlog.get_logger(__name__)

# An attempt whose interaction never reports a measured cost (a lost response,
# a crash before usage, an unpriced alias) stops counting against the cap after
# this long. Actual billing is unaffected: it follows usage, not holds.
HOLD_TTL = timedelta(hours=24)


def _held(row: Mapping[str, JsonValue]) -> Decimal:
    value = row.get("held_usd")
    if not isinstance(value, str):
        raise ValueError("invalid Gemini hold ledger")
    return Decimal(value)


def _stamp(row: dict[str, JsonValue], now: datetime) -> datetime:
    """When the hold was taken; a row from before stamping starts its clock now."""
    value = row.get("held_at")
    if not isinstance(value, str):
        row["held_at"] = now.isoformat()
        return now
    return datetime.fromisoformat(value)


def _expire(rows: dict[str, JsonValue], now: datetime) -> None:
    """Release holds older than HOLD_TTL; released rows stay as financial evidence."""
    for id_, row in rows.items():
        if not isinstance(row, dict):
            raise ValueError("invalid Gemini hold ledger")
        held, taken = _held(row), _stamp(row, now)
        if held > 0 and now - taken >= HOLD_TTL:
            row.update(held_usd="0", expired_usd=str(held), expired_at=now.isoformat())
            log.warning(
                "provider_hold_expired",
                hold_id=id_,
                model=row.get("model"),
                outcome=row.get("outcome"),
                interaction_id=row.get("interaction_id"),
                expired_usd=str(held),
                held_at=taken.isoformat(),
            )


def outstanding_holds(body: str | None) -> dict[str, JsonValue]:
    """Read-only summary of one hold ledger: what still counts against its cap."""
    rows = _ROWS.validate_json(body) if body is not None else {}
    held, open_rows, oldest = Decimal(0), 0, None
    outcomes: dict[str, int] = {}
    for row in rows.values():
        if not isinstance(row, dict):
            raise ValueError("invalid Gemini hold ledger")
        amount = _held(row)
        if amount > 0:
            held += amount
            open_rows += 1
            outcome = str(row.get("outcome"))
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
            taken = row.get("held_at")
            if isinstance(taken, str) and (oldest is None or taken < oldest):
                oldest = taken
    return {
        "held_usd": str(held),
        "open_holds": open_rows,
        "open_by_outcome": dict(sorted(outcomes.items())),
        "oldest_open_held_at": oldest,
        "rows": len(rows),
    }


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
    await complete(asyncio.to_thread(operation))


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
    async def transaction(self, *, after_dispatch: bool = False) -> AsyncIterator[Payload]:
        opening = asyncio.create_task(asyncio.to_thread(self._open))
        try:
            connection, body = await asyncio.shield(opening)
        except BaseException:
            # Cancellation must not abandon a thread that acquired a DB lock.
            connection, _ = await opening
            await asyncio.to_thread(connection.close)
            raise
        try:

            async def save(value: str) -> None:
                await _finish(lambda: self.save(connection, value))

            yield Payload(body, save)
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


class _GeminiStorage:
    """Full Records transactions scoped to the authorized provider account."""

    def __init__(self, database: PayloadDatabase) -> None:
        self._database = database

    @asynccontextmanager
    async def read(self) -> AsyncIterator[Records]:
        """The last committed Records for a pure read (mux `read_records`); never saved.

        A database with no committed-snapshot read (SQLite, one local process) serves
        it from its transaction.
        """
        reader = getattr(self._database, "read", None)
        if not callable(reader):
            async with self.transaction() as records:
                yield records
            return
        body = await cast(Callable[[], Awaitable[str | None]], reader)()
        yield (
            Records()
            if body is None
            else _RECORDS.validate_python(_decode(_JSON.validate_json(body)))
        )

    @asynccontextmanager
    async def transaction(self, *, after_dispatch: bool = False) -> AsyncIterator[Records]:
        async with self._database.transaction(after_dispatch=after_dispatch) as payload:
            body = payload.body
            records = (
                Records()
                if body is None
                else _RECORDS.validate_python(_decode(_JSON.validate_json(body)))
            )
            yield records
            encoded = json.dumps(_encode(_RECORDS.dump_python(records)), allow_nan=False)
            await payload.save(encoded)


@runtime_checkable
class HoldSettlement(Protocol):
    """A hold ledger that releases an attempt's hold once its cost is measured."""

    async def settle(self, *, interaction_id: str, model: str, actual_usd: Decimal) -> bool: ...


def interaction_of(observation: UsageObservation) -> str | None:
    """The interaction a Gemini usage observation measures (`gemini:<id>:usage`)."""
    prefix, suffix = "gemini:", ":usage"
    value = observation.id
    if not value.startswith(prefix) or not value.endswith(suffix):
        return None
    return value[len(prefix) : -len(suffix)] or None


class _FlashAccounting:
    """Reserve explicit operator holds before dispatch; never guess actual cost."""

    def __init__(
        self,
        database: PayloadDatabase,
        *,
        hold_usd: Decimal,
        cap_usd: Decimal,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not hold_usd.is_finite() or not cap_usd.is_finite() or not 0 < hold_usd <= cap_usd:
            raise ValueError("positive bounded Gemini holds are required")
        self._database = database
        self._hold, self._cap = hold_usd, cap_usd
        self._now = now
        self._pending: ContextVar[str | None] = ContextVar("gemini_attempt_hold", default=None)

    async def _update(self, update: Callable[[dict[str, JsonValue]], None]) -> None:
        async with self._database.transaction() as payload:
            body = payload.body
            rows = _ROWS.validate_json(body) if body is not None else {}
            update(rows)
            await payload.save(json.dumps(rows, allow_nan=False))

    async def before_attempt(self, *, model: ModelRef, attempt: int) -> None:
        if (
            model.provider != "gemini"
            or model.id not in ATTEMPT_MODELS
            or attempt < 1
            or self._pending.get() is not None
        ):
            raise AdmissionDenied(reason="backend_unsupported")
        id_ = str(uuid4())

        full = False

        def reserve(rows: dict[str, JsonValue]) -> None:
            nonlocal full
            # Read the clock under the ledger lock, so expiry and stamps are ordered.
            now = self._now()
            # Expiry and first-sight stamps commit even when this attempt is refused,
            # so a ledger already at its cap still ages out.
            _expire(rows, now)
            held = sum(
                (_held(cast(dict[str, JsonValue], row)) for row in rows.values()), Decimal(0)
            )
            if held + self._hold > self._cap:
                full = True
                return
            rows[id_] = {
                "model": model.id,
                "attempt": attempt,
                "held_usd": str(self._hold),
                "held_at": now.isoformat(),
                "outcome": "unknown",
                "actual_usd": None,
            }

        await self._update(reserve)
        if full:
            raise AdmissionDenied(reason="provider_hold_cap_reached")
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
            status = receipt.http_status
            # A 4xx rejected the request itself; a 503 UNAVAILABLE refused the work
            # (no interaction returned). Kept stranded, one 503 storm across the
            # Flash fallback chain would block the account's Gemini calls for the TTL.
            rejected = status is not None and (400 <= status < 500 or status == 503)
            if (
                receipt.outcome == "refused"
                and receipt.interaction_id is None
                and (rejected or not receipt.sent)
            ):
                # Google rejected or refused the request, or it never reached Google
                # (the connection failed): no interaction, nothing to bill. Release
                # the hold instead of stranding it as unknown.
                row.update(held_usd="0", actual_usd="0", released_usd=row.get("held_usd"))

        try:
            await self._update(publish)
        finally:
            # One receipt ends the attempt even if publishing failed: a stale marker
            # would refuse every later attempt in this context as backend_unsupported.
            self._pending.set(None)

    async def settle(self, *, interaction_id: str, model: str, actual_usd: Decimal) -> bool:
        """Settle the hold of the attempt that created `interaction_id` to its measured cost.

        Idempotent: the hold is released once; a later cumulative revision of the
        same interaction's usage only replaces `actual_usd`, so nothing is counted
        twice. False when no accepted attempt in this ledger created the interaction.
        """
        if not interaction_id or not actual_usd.is_finite() or actual_usd < 0:
            raise ValueError("Gemini settlement requires an interaction and a measured cost")
        settled = False

        def apply(rows: dict[str, JsonValue]) -> None:
            nonlocal settled
            for row in rows.values():
                if (
                    not isinstance(row, dict)
                    or row.get("interaction_id") != interaction_id
                    or row.get("outcome") != "accepted"
                ):
                    continue
                if row.get("model") != model:
                    raise ValueError("Gemini usage differs from the attempt's model")
                held = _held(row)
                if held > 0:
                    row.update(held_usd="0", settled_usd=str(held))
                row["actual_usd"] = str(actual_usd)
                settled = True
                return

        await self._update(apply)
        return settled

    async def release_unbilled(self, *, interaction_id: str) -> bool:
        """Release the hold of an attempt whose interaction ended with no usage at all.

        A cancelled interaction Gemini never started (or cancelled before it ran)
        reports no usage, so `settle` never runs and its hold would wait out
        HOLD_TTL. Only while no cost was ever measured: a settled attempt keeps its
        actual cost, and a later usage reading still settles normally. Idempotent.
        """
        released = False

        def apply(rows: dict[str, JsonValue]) -> None:
            nonlocal released
            for row in rows.values():
                if (
                    not isinstance(row, dict)
                    or row.get("interaction_id") != interaction_id
                    or row.get("outcome") != "accepted"
                    or row.get("actual_usd") is not None
                ):
                    continue
                held = _held(row)
                if held > 0:
                    row.update(held_usd="0", released_usd=str(held))
                    released = True
                return

        await self._update(apply)
        return released


class SQLiteGeminiStorage(_GeminiStorage):
    """Full Records transactions in the explicitly selected local SQLite file."""

    def __init__(self, path: Path, *, namespace: str) -> None:
        super().__init__(_Database(path, namespace))


class PostgresGeminiStorage(_GeminiStorage):
    """Full Records transactions on the existing application state database."""

    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], *, scope: Scope, project: str
    ) -> None:
        super().__init__(
            PostgresPayloadDatabase(sessions, scope=scope, project=project, kind="records")
        )


class SQLiteFlashAccounting(_FlashAccounting):
    """The existing conservative hold owner with local SQLite persistence."""

    def __init__(
        self,
        path: Path,
        *,
        namespace: str,
        hold_usd: Decimal,
        cap_usd: Decimal,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        super().__init__(_Database(path, namespace), hold_usd=hold_usd, cap_usd=cap_usd, now=now)


class PostgresFlashAccounting(_FlashAccounting):
    """The same conservative hold owner with shared PostgreSQL persistence."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        scope: Scope,
        project: str,
        hold_usd: Decimal,
        cap_usd: Decimal,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        super().__init__(
            PostgresPayloadDatabase(sessions, scope=scope, project=project, kind="holds"),
            hold_usd=hold_usd,
            cap_usd=cap_usd,
            now=now,
        )
