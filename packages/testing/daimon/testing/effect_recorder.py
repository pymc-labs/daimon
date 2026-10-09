"""Ordered platform effects and exact DB snapshots for the offline oracle.

Only explicitly identified runtime ids and timestamps are normalized.
Observed timestamps become opaque; scheduled times keep whole-second offsets
from a record anchor (or explicit scenario epoch), removing clock jitter. Caller,
tenant, model, prices, errors, continuity values and effect order stay literal.
"""

from __future__ import annotations

import base64
import dataclasses
import inspect
import json
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import cast
from uuid import UUID

from daimon.core._models import Base
from daimon.testing.ma_transport import Json, RecordedRequest
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

DB_TABLES = (
    "usage_events",
    "tenant_ledger",
    "turn_outcomes",
    "thread_sessions",
    "task_continuations",
)
ID_FIELDS = frozenset(
    {
        "event_id",
        "managed_session_id",
        "ma_session_id",
        "session_id",
        "ma_agent_id",
        "message_id",
        "active_turn_message_id",
        "watermark_message_id",
        "predecessor_id",
        "replaced_by_id",
        "thread_session_id",
        "transfer_file_id",
        "turn_id",
        "turn_token",
        "tool_use_id",
        "custom_tool_use_id",
        "model_request_start_id",
        "model_request_end_id",
        "file_id",
        "resource_id",
        "continuation_id",
        "outcome_id",
    }
)
OBSERVED_TIME_FIELDS = frozenset(
    {
        "created_at",
        "updated_at",
        "processed_at",
        "started_at",
        "ended_at",
        "finished_at",
        "claimed_at",
        "completed_at",
        "active_turn_started_at",
        "fresh_start_requested_at",
        "recorded_at",
        "last_seen_at",
        "occurred_at",
        "delivered_at",
        "waiting_since",
        "last_attempt_at",
    }
)
SCHEDULED_TIME_FIELDS = frozenset(
    {"due_at", "available_at", "lease_expires_at", "expires_at", "rate_limit_until"}
)
TIME_FIELDS = OBSERVED_TIME_FIELDS | SCHEDULED_TIME_FIELDS
ANCHOR_FIELDS = (
    "created_at",
    "claimed_at",
    "started_at",
    "processed_at",
    "occurred_at",
    "recorded_at",
)

IDENTITY_FIELDS = frozenset(
    {
        "caller",
        "user",
        "account",
        "tenant",
        "model",
        "principal",
        "author",
        "member",
        "requester",
        "owner",
        "mentions",
        "guild",
        "channel",
        "team",
    }
)


def json_value(value: object) -> Json:
    """Serialize values without rounding money or silently dropping unknown fields."""
    if value is None or isinstance(value, str | bool | int | float):
        return value
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bytes):
        return {"base64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, Enum):
        return json_value(value.value)
    if isinstance(value, Mapping):
        return {
            str(key): json_value(item) for key, item in cast(Mapping[object, object], value).items()
        }
    if isinstance(value, list | tuple):
        return [json_value(item) for item in cast(list[object] | tuple[object, ...], value)]
    if isinstance(value, set | frozenset):
        return sorted(
            [json_value(item) for item in cast(set[object] | frozenset[object], value)],
            key=lambda item: json.dumps(item, sort_keys=True),
        )
    if isinstance(value, Exception):
        fields: dict[str, Json] = {"error_kind": type(value).__name__, "message": str(value)}
        for name in ("kind", "status_code"):
            if hasattr(value, name):
                fields[name] = json_value(getattr(value, name))
        cause = getattr(value, "cause", None)
        if cause is not None:
            fields["cause_kind"] = type(cause).__name__
        return fields
    if isinstance(value, BaseModel):
        return json_value(value.model_dump(mode="json"))
    if isinstance(value, RecordedRequest):
        return json_value(value.to_dict())
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: json_value(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return json_value(to_dict())
    raise TypeError(f"Oracle cannot serialize {type(value).__name__}")


class Normalizer:
    def __init__(self, *, epoch: datetime | None = None, runtime_ids: Iterable[str] = ()) -> None:
        self.ids: dict[str, str] = {}
        self.epoch = epoch
        self.runtime_ids = set(runtime_ids)

    def normalize(
        self,
        value: Json,
        *,
        field: str = "",
        identity: bool = False,
        anchor: datetime | None = None,
        table: str = "",
    ) -> Json:
        identity = identity or field in IDENTITY_FIELDS
        if isinstance(value, dict):
            local_anchor = next(
                (
                    parsed
                    for name in ANCHOR_FIELDS
                    if (parsed := _timestamp(value.get(name))) is not None
                ),
                anchor,
            )
            return {
                key: self.normalize(
                    item,
                    field=key,
                    identity=identity,
                    anchor=local_anchor,
                    table=key if key in DB_TABLES else table,
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [
                self.normalize(item, field=field, identity=identity, anchor=anchor, table=table)
                for item in value
            ]
        if isinstance(value, str) and not identity:
            if field in TIME_FIELDS:
                timestamp = _timestamp(value)
                if timestamp is None:
                    return value
                if field not in SCHEDULED_TIME_FIELDS:
                    return "<time>"
                reference = anchor if anchor is not None else self.epoch
                if reference is None:
                    # No timeline can be inferred from an isolated deadline.
                    # Supply a scenario epoch to keep its duration meaningful.
                    return value
                delta = timestamp - reference
                seconds = (
                    Decimal(delta.days * 86400 + delta.seconds)
                    + Decimal(delta.microseconds) / 1000000
                ).quantize(Decimal(1), rounding=ROUND_HALF_UP)
                if not seconds:
                    seconds = Decimal(0)
                return f"<time:anchor{seconds:+f}s>"
            if (
                field in ID_FIELDS
                or (field == "id" and value in self.runtime_ids)
                or (field == "idempotency_key" and table == "task_continuations")
            ):
                return self.ids.setdefault(value, f"<id:{len(self.ids) + 1}>")
        return value


def _timestamp(value: Json) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return timestamp.replace(tzinfo=UTC) if timestamp.tzinfo is None else timestamp


def _semantic_key(value: Json, *, runtime_fields: frozenset[str] = frozenset({"id"})) -> Json:
    if isinstance(value, dict):
        return {
            key: _semantic_key(item, runtime_fields=runtime_fields)
            for key, item in value.items()
            if key not in ID_FIELDS | TIME_FIELDS | runtime_fields
        }
    if isinstance(value, list):
        return [_semantic_key(item, runtime_fields=runtime_fields) for item in value]
    return value


class FakeClock:
    def __init__(self, now: datetime = datetime(2026, 10, 9, tzinfo=UTC)) -> None:
        self.current = now
        self.elapsed = 0.0

    def now(self) -> datetime:
        return self.current

    def monotonic(self) -> float:
        return self.elapsed

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("Fake time cannot go backwards")
        self.current += timedelta(seconds=seconds)
        self.elapsed += seconds


class EffectRecorder:
    def __init__(self) -> None:
        self.effects: list[dict[str, Json]] = []

    def record(
        self, platform: str, operation: str, payload: object, *, result: object = None
    ) -> None:
        self.effects.append(
            {
                "platform": platform,
                "operation": operation,
                "payload": json_value(payload),
                "result": json_value(result),
            }
        )

    async def database(self, session: AsyncSession) -> dict[str, Json]:
        """Capture every column, ordered by semantic row identity before normalization.

        DB rowsets have no effect order. Runtime PKs cannot establish a stable
        order across runs. Keep causal references in each row, sorting by all
        non-runtime fields with created_at as a temporal tiebreak. Fixtures must
        pin caller/tenant identity and provider resource ids. Platform effects
        retain their original order and are never sorted. Semantically identical
        rows with equal created_at retain DB scan order; fixtures must pin
        their identities or give them a distinct semantic/sequence field.
        """
        snapshot: dict[str, Json] = {}
        for name in DB_TABLES:
            table = Base.metadata.tables[name]
            rows = (await session.execute(select(table))).mappings()
            serialized = [cast(dict[str, Json], json_value(dict(row))) for row in rows]
            serialized.sort(
                key=lambda row: (
                    json.dumps(
                        _semantic_key(
                            row,
                            runtime_fields=frozenset({"id", "idempotency_key"})
                            if name == "task_continuations"
                            else frozenset({"id"}),
                        ),
                        sort_keys=True,
                    ),
                    str(row.get("created_at", "")),
                )
            )
            snapshot[name] = cast(list[Json], serialized)
        return snapshot

    def transcript(
        self,
        *,
        database: object = None,
        requests: object = None,
        epoch: datetime | None = None,
        runtime_ids: Iterable[str] = (),
    ) -> str:
        data = json_value({"effects": self.effects, "database": database, "requests": requests})
        registered = set(runtime_ids)
        if isinstance(data, dict) and isinstance(data["database"], dict):
            for table in DB_TABLES:
                rows = data["database"].get(table)
                if isinstance(rows, list):
                    for row in rows:
                        if isinstance(row, dict):
                            identifier = row.get("id")
                            if isinstance(identifier, str):
                                registered.add(identifier)
        normalizer = Normalizer(epoch=epoch, runtime_ids=registered)
        return json.dumps(normalizer.normalize(data), indent=2, sort_keys=True) + "\n"


def platform_receipt(value: object) -> object:
    message_id = getattr(value, "id", None)
    if isinstance(message_id, str | int):
        return {"message_id": str(message_id)}
    data = getattr(value, "data", None)
    if isinstance(data, Mapping):
        return cast(Mapping[object, object], data)
    return json_value(value)


class RecordingPlatformClient:
    """Wrap existing fake clients: posts, edits, reactions and file operations.

    A single recorder can wrap Discord channel/message clients and Slack clients,
    preserving their interleaved order. With wrap_result=True returned Discord
    messages/threads are wrapped too, so later edits/reactions stay recorded.
    Plain attributes and synchronous methods pass through unchanged. Wrapped
    results are proxies, so concrete-type isinstance checks differ; wrap only
    at the fake boundary, not objects handed to type-checking adapter code.
    """

    def __init__(
        self,
        client: object,
        recorder: EffectRecorder,
        *,
        platform: str,
        result_serializer: Callable[[object], object] = platform_receipt,
        wrap_result: bool = False,
    ) -> None:
        self.client = client
        self.recorder = recorder
        self.platform = platform
        self.result_serializer = result_serializer
        self.wrap_result = wrap_result

    def __getattr__(self, name: str) -> object:
        attribute = getattr(self.client, name)
        if not inspect.iscoroutinefunction(attribute):
            return attribute
        target = cast(Callable[..., Awaitable[object]], attribute)

        async def call(*args: object, **kwargs: object) -> object:
            payload = {"args": args, "kwargs": kwargs}
            try:
                result = await target(*args, **kwargs)
            except Exception as error:
                self.recorder.record(
                    self.platform,
                    name,
                    payload,
                    result={"error_kind": type(error).__name__, "message": str(error)},
                )
                raise
            self.recorder.record(
                self.platform, name, payload, result=self.result_serializer(result)
            )
            if self.wrap_result and any(
                inspect.iscoroutinefunction(getattr(result, method, None))
                for method in ("edit", "send", "add_reaction")
            ):
                return RecordingPlatformClient(
                    result,
                    self.recorder,
                    platform=self.platform,
                    result_serializer=self.result_serializer,
                    wrap_result=True,
                )
            return result

        return call
