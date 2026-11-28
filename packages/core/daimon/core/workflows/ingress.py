"""Webhook ingress: one delivery to a trigger's public URL becomes an accepted event.

Security code at the public edge. The trigger id is the only address; an unknown id, a
trigger that is not a webhook, a workspace with webhooks off and a test URL that is not
listening all answer the same 404, so nothing tells one workspace's ids from another's.
A failed signature is logged without its body or delivery id, so it cannot poison the
dedupe, and only up to the per-minute limit, so it cannot fill the log. Only signed
deliveries count toward the rate limits, so a stranger holding the URL cannot block the
sender. A delivery over a limit leaves one counted row per minute and one owner notice a day.
The body is verified raw, then parsed. Nothing here starts a run: the starts loop does,
through `runs.start_run`, so every trigger path meets the same preflight.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final, cast
from urllib.parse import parse_qsl

from cryptography.fernet import InvalidToken
from daimon.core.config import Settings
from daimon.core.github_credentials import build_multifernet, decrypt_token
from daimon.core.stores import workflow_triggers as store
from daimon.core.stores import workflows
from daimon.core.stores.workflow_triggers import TriggerRow
from daimon.core.workflows import engine, gate, runs
from daimon.core.workflows.expressions import ExpressionError, render
from daimon.core.workflows.runs import Sessions
from daimon.core.workflows.signatures import DELIVERY_HEADERS, SECRET_HEADERS, Scheme, verify
from daimon.core.workflows.spec import Spec, parse_spec
from daimon.core.workflows.triggers import MappingError, map_inputs, passes
from pydantic import JsonValue, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

_MAX_HEADERS: Final = 64
_MAX_HEADER_VALUE: Final = 1024
_MAX_DELIVERY_ID: Final = 200
_MINUTE: Final = timedelta(minutes=1)
_DAY: Final = timedelta(days=1)
#: Deeper bodies are refused: evaluating one would overflow the interpreter's stack.
MAX_DEPTH: Final = 64
#: GitHub signs no timestamp and its delivery header is unsigned, so a captured request
#: replays unless its body is deduped. Bearer is not: the token is the whole credential, so a
#: body hash adds no replay protection and would drop a legitimate repeated body.
_BODY_DEDUPED: Final = "github"


@dataclass(frozen=True)
class Reply:
    status: int
    body: dict[str, JsonValue] = field(default_factory=dict[str, JsonValue])


NOT_FOUND: Final = Reply(404, {"error": "not_found"})


def delivery_ref(event_id: uuid.UUID, test: bool) -> str:
    """The id a sender and the Triggers tab see for a delivery."""
    return f"dlv_{'test_' if test else ''}{event_id.hex}"


def kept_headers(headers: Mapping[str, str]) -> dict[str, JsonValue]:
    """What the log keeps: bounded, and never a header that carries a secret or session."""
    kept: dict[str, JsonValue] = {}
    for name, value in headers.items():
        if len(kept) >= _MAX_HEADERS:
            break
        if name not in SECRET_HEADERS:
            kept[name] = value[:_MAX_HEADER_VALUE]
    return kept


def parse(body: bytes, content_type: str) -> dict[str, JsonValue] | None:
    """The payload object, or None when the body is not one."""
    if not body.strip():
        return {}
    try:
        if content_type.startswith("application/x-www-form-urlencoded"):
            return dict(parse_qsl(body.decode(), keep_blank_values=True))
        value = json.loads(body)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    return cast("dict[str, JsonValue]", value) if isinstance(value, dict) else None


def too_deep(value: JsonValue) -> bool:
    """Nested more than `MAX_DEPTH` levels; walked without recursion."""
    stack: list[tuple[JsonValue, int]] = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        if depth > MAX_DEPTH:
            return True
        if isinstance(item, dict):
            stack.extend((v, depth + 1) for v in item.values())
        elif isinstance(item, list):
            stack.extend((v, depth + 1) for v in item)
    return False


def _delivery_id(
    scheme: Scheme | None,
    config: Mapping[str, Any],
    headers: Mapping[str, str],
    context: dict[str, JsonValue],
    now: datetime,
) -> str | None:
    """The sender's id for the delivery, so a retry is a duplicate."""
    found: JsonValue = headers.get(DELIVERY_HEADERS.get(scheme or "", ""))
    payload = context["payload"]
    if not found and isinstance(payload, dict):
        found = payload.get({"stripe": "id", "slack": "event_id"}.get(scheme or "", ""))
    source = config.get("delivery_id")
    if not found and isinstance(source, str):
        try:
            found = render(source if "${{" in source else f"${{{{ {source} }}}}", context, now=now)
        except ExpressionError:
            found = None
    return str(found)[:_MAX_DELIVERY_ID] if isinstance(found, str | int) and found else None


async def _target(
    session: AsyncSession, trigger: TriggerRow, test: bool, now: datetime
) -> tuple[Spec, dict[str, Any]] | None:
    """The version a delivery runs (the draft for a listening test URL) and its trigger."""
    workflow = await workflows.get_workflow(
        session, tenant_id=trigger.tenant_id, workflow_id=trigger.workflow_id
    )
    if workflow is None or workflow.status != "active":
        return None
    if test:
        if trigger.listening_until is None or trigger.listening_until <= now:
            return None
        document: JsonValue = workflow.draft_spec
    else:
        if trigger.live is None or workflow.live_version is None:
            return None
        live = await workflows.get_version(
            session,
            tenant_id=trigger.tenant_id,
            workflow_id=workflow.id,
            number=workflow.live_version,
        )
        if live is None:
            return None
        document = live.spec
    try:
        spec = parse_spec(document)
    except ValidationError:
        return None
    found = next((t for t in spec.triggers or [] if t.id == trigger.trigger_key), None)
    if found is None or found.kind != "webhook":
        return None
    return spec, found.model_dump(by_alias=True, exclude_none=True)


def _secrets(settings: Settings, encrypted: list[bytes]) -> list[str]:
    keys = tuple(k.get_secret_value() for k in settings.crypto.keys)
    if not keys or not encrypted:
        return []
    fernet = build_multifernet(keys)
    plain: list[str] = []
    for secret in encrypted:
        try:
            plain.append(decrypt_token(fernet, secret))
        except InvalidToken:
            continue  # under a key the deployment dropped: it verifies nothing
    return plain


async def _limited(
    session: AsyncSession, settings: Settings, trigger: TriggerRow, now: datetime
) -> bool:
    limits = settings.workflows
    since = now - _MINUTE
    if await store.count_events_since(session, since=since, trigger_id=trigger.id) >= (
        limits.webhook_per_minute
    ):
        return True
    return (
        await store.count_events_since(session, since=since, tenant_id=trigger.tenant_id)
        >= limits.webhook_tenant_per_minute
    )


async def _note_limited(
    session: AsyncSession, trigger: TriggerRow, now: datetime, test: bool
) -> None:
    await store.note_rate_limited(session, trigger=trigger, now=now, test=test)
    topic = f"rate_limited:{trigger.trigger_key}"
    if test or await store.notice_sent_since(
        session, workflow_id=trigger.workflow_id, topic=topic, since=now - _DAY
    ):
        return
    await store.add_owner_notice(
        session,
        tenant_id=trigger.tenant_id,
        workflow_id=trigger.workflow_id,
        topic=topic,
        text=(
            f"The {trigger.trigger_key} webhook is receiving more deliveries than it accepts "
            "a minute, and the extra ones are dropped. See the Deliveries tab."
        ),
    )


async def _offer(
    session: AsyncSession,
    trigger: TriggerRow,
    spec: Spec,
    config: dict[str, Any],
    context: dict[str, JsonValue],
    now: datetime,
) -> None:
    """A delivery that starts no run is an event for this workflow's waits on the trigger,
    keyed like a run its mapped inputs would start."""
    try:
        inputs, _ = engine.resolve_inputs(spec, map_inputs(config.get("inputs"), context, now=now))
        key = runs.concurrency_key(spec, inputs, now)
    except (MappingError, ExpressionError):
        inputs, key = None, None
    source = f"trigger:{trigger.workflow_id}:{trigger.trigger_key}"
    event = {**context, "inputs": inputs, "received_at": now.isoformat()}
    await runs.offer_event(session, trigger.tenant_id, source, key, event, now)


async def receive(
    sm: Sessions,
    settings: Settings,
    *,
    trigger_id: str,
    test: bool,
    headers: Mapping[str, str],
    body: bytes,
    now: datetime,
) -> Reply:
    """Verify, map and record one delivery. `headers` have lowercase names; `body` is
    already bounded by the caller and is checked against the cap again here."""
    try:
        tid = uuid.UUID(trigger_id)
    except ValueError:
        return NOT_FOUND
    async with sm() as session:
        trigger = await store.get_trigger(session, tid)
    if trigger is None or trigger.kind != "webhook" or not trigger.enabled:
        return NOT_FOUND
    features = await gate.features_at(sm, settings, trigger.tenant_id)
    if features is None or not features.enabled or not features.webhooks:
        return NOT_FOUND
    if len(body) > settings.workflows.webhook_max_body_bytes:
        return Reply(413, {"error": "too_large"})
    async with sm.begin() as session:
        target = await _target(session, trigger, test, now)
        if target is None:
            return NOT_FOUND
        spec, config = target
        scheme = cast("Scheme | None", config.get("verify"))
        secrets = await store.live_secrets(session, trigger_id=trigger.id, now=now)
        plain = _secrets(settings, [s.encrypted_secret for s in secrets])
        kept, signed = kept_headers(headers), scheme is not None

        async def reject(http: int, reason: str, payload: dict[str, JsonValue] | None) -> None:
            await store.record_event(
                session,
                trigger=trigger,
                status="rejected",
                received_at=now,
                test=test,
                verified=reason != "signature" and signed,
                http_status=http,
                reason=reason,
                headers=kept,
                payload=payload,
            )

        if not verify(scheme, plain, headers, body, now=now):
            bad = await store.count_events_since(
                session, since=now - _MINUTE, trigger_id=trigger.id, reason="signature"
            )
            if bad < settings.workflows.webhook_per_minute:
                await reject(401, "signature", None)
            return Reply(401, {"error": "invalid_signature"})
        if await _limited(session, settings, trigger, now):
            await _note_limited(session, trigger, now, test)
            return Reply(429, {"error": "rate_limited"})
        payload = parse(body, headers.get("content-type", ""))
        if payload is None:
            await reject(422, "not_an_object", None)
            return Reply(422, {"error": "invalid_body", "message": "Send a JSON object"})
        if too_deep(payload):
            await reject(422, "too_deep", None)
            message = f"Nest the JSON at most {MAX_DEPTH} levels deep"
            return Reply(422, {"error": "invalid_body", "message": message})
        if scheme == "slack" and payload.get("type") == "url_verification":
            return Reply(200, {"challenge": payload.get("challenge")})
        context: dict[str, JsonValue] = {"payload": payload, "headers": kept}
        delivery = _delivery_id(scheme, config, headers, context, now)
        # GitHub signs no timestamp, so a replayed body is caught by its hash.
        digest = (
            f"{'test:' if test else ''}{hashlib.sha256(body).hexdigest()}"
            if scheme == _BODY_DEDUPED
            else None
        )
        try:
            wanted = passes(config.get("start_when"), context, now=now)
            inputs = map_inputs(config.get("inputs"), context, now=now) if wanted else {}
        except MappingError as err:
            await reject(422, "mapping", payload)
            return Reply(422, {"error": "mapping", "message": str(err)})
        resolved, problems = engine.resolve_inputs(spec, inputs)
        if wanted and problems:
            await reject(422, "invalid_inputs", payload)
            return Reply(422, {"error": "invalid_inputs", "problems": list(problems)})
        event, first = await store.record_event(
            session,
            trigger=trigger,
            status="accepted" if wanted else "skipped",
            received_at=now,
            delivery_id=delivery,
            payload_hash=digest,
            test=test,
            verified=signed,
            http_status=202 if wanted else 200,
            reason=None if wanted else "start_when",
            headers=kept,
            payload=payload,
            inputs=resolved if wanted else None,
        )
        if first is not None:
            return Reply(200, {"delivery": delivery_ref(first.id, test), "duplicate": True})
        if test:
            await store.set_listening(session, trigger, until=None, listener=None)  # one capture
        if not wanted:
            if not test:
                await _offer(session, trigger, spec, config, context, now)
            return Reply(200, {"delivery": delivery_ref(event.id, test), "started": False})
        await session.execute(select(func.pg_notify(runs.START_CHANNEL, "webhook")))
    return Reply(202, {"delivery": delivery_ref(event.id, test)})
