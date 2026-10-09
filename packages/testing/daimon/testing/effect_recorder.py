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
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import cast
from urllib.parse import parse_qsl, unquote_plus, urlsplit
from uuid import UUID

from daimon.core._models import Base
from daimon.core.session_snapshot import (
    SessionSnapshot,
    fingerprint_identity,
    fingerprint_mutable,
)
from daimon.testing.ma_transport import Json, RecordedRequest
from pydantic import BaseModel, ValidationError
from sqlalchemy import MetaData, select
from sqlalchemy.ext.asyncio import AsyncSession

DB_TABLES = (
    "usage_events",
    "tenant_ledger",
    "turn_outcomes",
    "thread_sessions",
    "task_continuations",
)
# Pinned at integration 4d61c7391a5098f8ae1cffd7ca1a80fb077af326.
# Additive schema fields are captured separately; never regenerate this list.
LEGACY_DB_COLUMNS: dict[str, tuple[str, ...]] = {
    "usage_events": (
        "id",
        "tenant_id",
        "occurred_at",
        "platform_user_id",
        "managed_session_id",
        "model",
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "event_id",
        "channel_id",
    ),
    "tenant_ledger": (
        "id",
        "tenant_id",
        "delta_usd",
        "reason",
        "idempotency_key",
        "payment_event_id",
        "payment_intent",
        "occurred_at",
        "channel_id",
    ),
    "turn_outcomes": (
        "id",
        "tenant_id",
        "account_id",
        "platform",
        "channel_id",
        "thread_id",
        "agent_id",
        "session_id",
        "origin",
        "reason",
        "started_at",
        "ended_at",
        "duration_ms",
        "recovered",
        "error_class",
        "release",
        "usage_refs",
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "model_calls",
        "model_ids",
        "cost_usd",
        "unpriced_calls",
        "billing_posture",
    ),
    "thread_sessions": (
        "id",
        "tenant_id",
        "platform",
        "thread_id",
        "account_id",
        "ma_session_id",
        "ma_agent_id",
        "channel_id",
        "seal_ids",
        "watermark_message_id",
        "status",
        "effective_config",
        "identity_fingerprint",
        "mutable_fingerprint",
        "predecessor_id",
        "replaced_by_id",
        "transfer_file_id",
        "transfer_kind",
        "fresh_start_requested_at",
        "github_key_restart_notice",
        "pending_unsaved_work",
        "active_turn_message_id",
        "active_turn_started_at",
        "active_turn_channel_id",
        "created_at",
        "updated_at",
    ),
    "task_continuations": (
        "id",
        "tenant_id",
        "platform",
        "parent_channel_id",
        "thread_id",
        "requester_account_id",
        "requester_external_user_id",
        "target_ma_agent_id",
        "target_name",
        "requested_work",
        "reason",
        "status",
        "skip_reason",
        "idempotency_key",
        "created_at",
        "claimed_at",
        "delivered_at",
        "available_at",
        "lease_owner",
        "lease_expires_at",
        "started_at",
        "attempts",
    ),
}
DATABASE_EXTENSIONS = "non_legacy_columns"

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
        "tenant_id",
        "account_id",
        "principal_id",
        "authorization_id",
        "platform_user_id",
        "requester_account_id",
        "requester_external_user_id",
        "channel_id",
        "thread_id",
        "parent_channel_id",
        "model_id",
        "model_ids",
    }
)

# Provider handles, including the prefixes used by the offline SDK fixtures.
# Match only explicit ID fields and provider URL paths, never arbitrary text.
PROVIDER_ID = re.compile(
    r"(?<![\w-])(?:memstore|memver|session|agent|sesn|sess|skill|file|"
    r"vault|vlt|env|sevt|evt|outc|res|mem|ag|ses|toolu|tu|e|m|s)_[A-Za-z0-9_-]+(?![\w-])"
)
PROVIDER_ID_FIELDS = frozenset({"id", "first_id", "last_id", "agent"})
PROVIDER_COLLECTIONS = frozenset(
    {"agents", "sessions", "environments", "skills", "files", "vaults", "memory_stores"}
)
SSE_DATA = re.compile(r"(?m)^(data: ?)([^\r\n]+)")
TURN_CONTROLS = re.compile(r"(<turn_controls>\r?\n)([^\r\n]+)")
JSON_STRING = re.compile(r'"(?:\\.|[^"\\])*"')


def _provider_id_field(field: str) -> bool:
    return field in PROVIDER_ID_FIELDS or field in ID_FIELDS or field.endswith(("_id", "_ids"))


def database_metadata() -> MetaData:
    """Expose mapped table metadata for deterministic offline fixture setup.

    Return the live mapped metadata so fixture clocks and UUID defaults affect
    ORM inserts as well as recorder queries. Reflected tables would lose those
    Python defaults. Private model imports stay inside the testing package.
    """
    return Base.metadata


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
    def __init__(
        self,
        *,
        epoch: datetime | None = None,
        runtime_ids: Iterable[str] = (),
        provider_requests: Iterable[dict[str, Json]] = (),
    ) -> None:
        self.ids: dict[str, str] = {}
        self.epoch = epoch
        self.runtime_ids = set(runtime_ids)
        self.provider_ids: set[str] = set()
        self._normalizing = False
        # Only the actual transport envelope owns an API path/query. Nested
        # request bodies and configured URLs/paths remain literal even when
        # they contain /v1/ or the same handle as a provider response.
        self.provider_requests = tuple(provider_requests)
        self._provider_request_ids = {id(request) for request in self.provider_requests}

    @staticmethod
    def _route_id_segments(path: str) -> dict[int, str]:
        segments = path.split("/")
        if len(segments) < 4 or segments[:2] != ["", "v1"]:
            return {}
        if segments[2] not in PROVIDER_COLLECTIONS:
            return {}
        identifiers = {3: segments[3]}
        if len(segments) > 5 and (
            (segments[2] == "sessions" and segments[4] in {"events", "resources"})
            or (segments[2] == "memory_stores" and segments[4] == "versions")
        ):
            identifiers[5] = segments[5]
        return identifiers

    def _collect_route(self, value: str) -> None:
        parsed = urlsplit(value)
        self.provider_ids.update(
            segment
            for segment in self._route_id_segments(parsed.path).values()
            if PROVIDER_ID.fullmatch(segment)
        )
        for name, item in parse_qsl(parsed.query, keep_blank_values=True):
            self._collect_providers(item, field=name, identity=False)

    def _collect_providers(self, value: Json, *, field: str, identity: bool) -> None:
        identity = identity or field in IDENTITY_FIELDS
        if identity:
            return
        if isinstance(value, dict):
            for key, item in value.items():
                if id(value) in self._provider_request_ids and key in {"path", "url"}:
                    if isinstance(item, str):
                        self._collect_route(item)
                elif id(value) in self._provider_request_ids and key == "query":
                    if isinstance(item, list) and self._query_pairs(item):
                        for pair in cast(list[list[Json]], item):
                            self._collect_providers(
                                pair[1], field=cast(str, pair[0]), identity=identity
                            )
                else:
                    self._collect_providers(item, field=key, identity=identity)
        elif isinstance(value, list):
            for item in value:
                self._collect_providers(item, field=field, identity=identity)
        elif isinstance(value, str):
            if _provider_id_field(field) and PROVIDER_ID.fullmatch(value):
                self.provider_ids.add(value)
            for _, payload in self._json_fragments(value, field=field):
                self._collect_providers(payload, field="", identity=False)

    @staticmethod
    def _json_fragments(value: str, *, field: str) -> list[tuple[re.Match[str], Json]]:
        pattern = (
            SSE_DATA
            if field == "body" and value.startswith(("event:", "data:"))
            else TURN_CONTROLS
            if field == "text"
            else None
        )
        if pattern is None:
            return []
        result: list[tuple[re.Match[str], Json]] = []
        for match in pattern.finditer(value):
            try:
                payload = cast(Json, json.loads(match.group(2)))
            except json.JSONDecodeError:
                continue
            result.append((match, payload))
        return result

    def _embedded_ids(self, value: Json, *, field: str = "", identity: bool = False) -> Json:
        identity = identity or field in IDENTITY_FIELDS
        if isinstance(value, dict):
            normalized = {
                key: self._embedded_ids(item, field=key, identity=identity)
                for key, item in value.items()
            }
            self._error_message(value, normalized, field=field, identity=identity)
            return normalized
        if isinstance(value, list):
            return [self._embedded_ids(item, field=field, identity=identity) for item in value]
        if isinstance(value, str) and not identity:
            return self._runtime_string(value, field=field, table="")
        return value

    def _encoded_json_ids(self, raw: str, payload: Json) -> str:
        normalized = self._embedded_ids(payload)

        def strings(original: Json, updated: Json) -> list[tuple[str, str]]:
            if isinstance(original, dict):
                assert isinstance(updated, dict)
                return [
                    pair
                    for key, value in original.items()
                    for pair in [(key, key), *strings(value, updated[key])]
                ]
            if isinstance(original, list):
                assert isinstance(updated, list)
                return [
                    pair
                    for value, replacement in zip(original, updated, strict=True)
                    for pair in strings(value, replacement)
                ]
            if isinstance(original, str):
                assert isinstance(updated, str)
                return [(original, updated)]
            return []

        pieces: list[str] = []
        offset = 0
        for match, (original, updated) in zip(
            JSON_STRING.finditer(raw), strings(payload, normalized), strict=True
        ):
            assert json.loads(match.group()) == original
            pieces.append(raw[offset : match.start()])
            pieces.append(match.group() if original == updated else json.dumps(updated))
            offset = match.end()
        pieces.append(raw[offset:])
        return "".join(pieces)

    def _identifier(self, value: str) -> str:
        return self.ids.setdefault(value, f"<id:{len(self.ids) + 1}>")

    @staticmethod
    def _query_pairs(value: list[Json]) -> bool:
        return all(
            isinstance(pair, list) and len(pair) == 2 and isinstance(pair[0], str) for pair in value
        )

    def _provider_references(self, value: str) -> str:
        return PROVIDER_ID.sub(
            lambda match: (
                self._identifier(match.group())
                if match.group() in self.provider_ids
                else match.group()
            ),
            value,
        )

    def _route_references(self, value: str) -> str:
        path, separator, query = value.partition("?")
        parsed_path = urlsplit(path).path
        identifiers = self._route_id_segments(parsed_path)
        segments = parsed_path.split("/")
        for index, identifier in identifiers.items():
            segments[index] = self._provider_references(identifier)
        normalized_path = "/".join(segments)
        # Retain the exact authority/encoding; only known handle slots change.
        if parsed_path:
            path = path[: len(path) - len(parsed_path)] + normalized_path
        if not separator:
            return path
        pairs: list[str] = []
        for pair in query.split("&"):
            name, equals, encoded = pair.partition("=")
            field = unquote_plus(name).removesuffix("[]")
            original = unquote_plus(encoded)
            updated = (
                original
                if field in IDENTITY_FIELDS
                else self._runtime_string(original, field=field, table="")
            )
            pairs.append(name + equals + (encoded if original == updated else updated))
        return path + separator + "&".join(pairs)

    def _error_message(
        self, original: dict[str, Json], normalized: dict[str, Json], *, field: str, identity: bool
    ) -> None:
        message = original.get("message")
        if (
            not identity
            and (field == "error" or "error_kind" in original)
            and isinstance(message, str)
        ):
            normalized["message"] = self._provider_references(message)

    def _runtime_string(self, value: str, *, field: str, table: str) -> str:
        if (
            field in ID_FIELDS
            or (field == "id" and value in self.runtime_ids)
            or (_provider_id_field(field) and value in self.provider_ids)
            or (field == "idempotency_key" and table == "task_continuations")
        ):
            return self._identifier(value)
        if field == "idempotency_key":
            return self._provider_references(value)
        return value

    def normalize(
        self,
        value: Json,
        *,
        field: str = "",
        identity: bool = False,
        anchor: datetime | None = None,
        table: str = "",
    ) -> Json:
        if not self._normalizing:
            self._collect_providers(value, field=field, identity=identity)
            self._normalizing = True
            try:
                return self._normalize(
                    value, field=field, identity=identity, anchor=anchor, table=table
                )
            finally:
                self._normalizing = False
        return self._normalize(value, field=field, identity=identity, anchor=anchor, table=table)

    def _normalize(
        self,
        value: Json,
        *,
        field: str,
        identity: bool,
        anchor: datetime | None,
        table: str,
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
            normalized: dict[str, Json] = {}
            for key, item in value.items():
                if not identity and id(value) in self._provider_request_ids:
                    if key in {"path", "url"} and isinstance(item, str):
                        normalized[key] = self._route_references(item)
                        continue
                    if key == "query" and isinstance(item, list) and self._query_pairs(item):
                        normalized[key] = [
                            [
                                pair[0],
                                self.normalize(
                                    pair[1],
                                    field=cast(str, pair[0]),
                                    anchor=local_anchor,
                                    table=table,
                                ),
                            ]
                            for pair in cast(list[list[Json]], item)
                        ]
                        continue
                normalized[key] = self.normalize(
                    item,
                    field=key,
                    identity=identity,
                    anchor=local_anchor,
                    table=key if key in DB_TABLES else table,
                )
            self._error_message(value, normalized, field=field, identity=identity)
            self._fingerprints(value, normalized)
            return normalized
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
            fragments = self._json_fragments(value, field=field)
            if fragments:
                pieces: list[str] = []
                offset = 0
                for match, payload in fragments:
                    pieces.append(value[offset : match.start(2)])
                    # Only identifiers change inside encoded wire/prompt JSON;
                    # timestamps, text and all other payload values stay exact.
                    pieces.append(self._encoded_json_ids(match.group(2), payload))
                    offset = match.end(2)
                pieces.append(value[offset:])
                return "".join(pieces)
            return self._runtime_string(value, field=field, table=table)
        return value

    @staticmethod
    def _fingerprints(original: dict[str, Json], normalized: dict[str, Json]) -> None:
        config = original.get("effective_config")
        if not isinstance(config, dict):
            return
        try:
            snapshot = SessionSnapshot.model_validate(config)
        except ValidationError:
            # Partial/fake snapshots have no derivation we can verify.
            return
        normalized_snapshot = SessionSnapshot.model_validate(normalized["effective_config"])
        for name, fingerprint in (
            ("identity_fingerprint", fingerprint_identity),
            ("mutable_fingerprint", fingerprint_mutable),
        ):
            actual = original.get(name)
            if actual is None:
                continue
            if actual != fingerprint(snapshot):
                raise ValueError(f"Captured {name} does not match effective_config")
            # Preserve configuration/hash sensitivity while making the digest
            # use the same provider aliases as its recorded input snapshot.
            normalized[name] = fingerprint(normalized_snapshot)


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
        derived: frozenset[str] = frozenset()
        config = value.get("effective_config")
        if isinstance(config, dict):
            try:
                SessionSnapshot.model_validate(config)
            except ValidationError:
                pass
            else:
                # Its literal configuration remains in the key. The digest
                # additionally encodes opaque provider handles, so cannot sort.
                derived = frozenset({"identity_fingerprint", "mutable_fingerprint"})
        return {
            key: _semantic_key(item, runtime_fields=runtime_fields)
            for key, item in value.items()
            if key not in ID_FIELDS | TIME_FIELDS | runtime_fields | derived
            and not (
                key not in IDENTITY_FIELDS
                and _provider_id_field(key)
                and isinstance(item, str)
                and PROVIDER_ID.fullmatch(item)
            )
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
        """Capture pinned legacy rows and separately labelled additive columns.

        DB rowsets have no effect order. Runtime PKs cannot establish a stable
        order across runs. Keep causal references in each row, sorting by legacy
        non-runtime fields with created_at as a temporal tiebreak. Fixtures must
        pin caller/tenant identity and provider resource ids. Platform effects
        retain their original order and are never sorted. Semantically identical
        rows with equal created_at retain DB scan order; fixtures must pin
        their identities or give them a distinct semantic/sequence field.
        """
        snapshot: dict[str, Json] = {}
        extensions: dict[str, Json] = {}
        for name in DB_TABLES:
            table = database_metadata().tables[name]
            legacy_columns = LEGACY_DB_COLUMNS[name]
            missing = set(legacy_columns) - set(table.columns.keys())
            if missing:
                raise ValueError(f"Oracle legacy columns missing from {name}: {sorted(missing)}")
            added = tuple(
                column.name for column in table.columns if column.name not in legacy_columns
            )
            rows = (await session.execute(select(table))).mappings()
            serialized = [
                (
                    cast(dict[str, Json], json_value({key: row[key] for key in legacy_columns})),
                    cast(dict[str, Json], json_value({key: row[key] for key in added})),
                )
                for row in rows
            ]
            serialized.sort(
                key=lambda pair: (
                    json.dumps(
                        _semantic_key(
                            pair[0],
                            runtime_fields=frozenset({"id", "idempotency_key"})
                            if name == "task_continuations"
                            else frozenset({"id"}),
                        ),
                        sort_keys=True,
                    ),
                    str(pair[0].get("created_at", "")),
                )
            )
            snapshot[name] = [legacy for legacy, _ in serialized]
            if added:
                extensions[name] = {
                    "columns": list(added),
                    "rows": [
                        {"legacy_row_index": index, "values": extra}
                        for index, (_, extra) in enumerate(serialized)
                    ],
                }
        if extensions:
            snapshot[DATABASE_EXTENSIONS] = extensions
        return snapshot

    def transcript(
        self,
        *,
        database: object = None,
        requests: object = None,
        epoch: datetime | None = None,
        runtime_ids: Iterable[str] = (),
    ) -> str:
        serialized_database = json_value(database)
        extensions: Json = None
        if isinstance(serialized_database, dict):
            extensions = serialized_database.pop(DATABASE_EXTENSIONS, None)
        data = json_value(
            {"effects": self.effects, "database": serialized_database, "requests": requests}
        )
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
        provider_requests: list[dict[str, Json]] = []
        if isinstance(data, dict):
            effects = data["effects"]
            if isinstance(effects, list):
                for effect in effects:
                    if (
                        isinstance(effect, dict)
                        and effect.get("platform") in {"ma_http", "anthropic"}
                        and effect.get("operation") == "request"
                        and isinstance(payload := effect.get("payload"), dict)
                    ):
                        provider_requests.append(payload)
            requests = data["requests"]
            if isinstance(requests, list):
                provider_requests.extend(
                    request for request in requests if isinstance(request, dict)
                )
        normalizer = Normalizer(
            epoch=epoch, runtime_ids=registered, provider_requests=provider_requests
        )
        normalized = normalizer.normalize(data)
        if extensions is not None:
            # New fields must not consume legacy runtime-id numbers or alter dates.
            assert isinstance(normalized, dict)
            normalized["database_extensions"] = extensions
        return json.dumps(normalized, indent=2, sort_keys=True) + "\n"


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
