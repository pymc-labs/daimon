"""Offline adapter for N9's paired legacy/mux replay benchmark.

Run from the repository root:
PYTHONPATH=tests:packages/core/tests uv run python tests/baselines/replay.py \
  --adapter turn.replay_adapter:build --output /tmp/n4-turn-replay.json
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import os
import time
from datetime import UTC, datetime
from typing import Any, Literal
from unittest.mock import patch

from anthropic.types.beta.sessions import BetaManagedAgentsSpanModelRequestEndEvent
from baselines.replay import Evidence
from daimon.core.config import load_turn_settings
from daimon.core.turn.driver import run_turn
from daimon.core.turn.posture import BillingExempt
from daimon.testing.ma import send_events_response
from daimon.testing.ma_models import ma_model_usage
from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport
from daimon.testing.turn_fakes import RecordingLifecycle
from mux.contracts.ids import ResourceRef, Scope
from mux.drivers.anthropic.normalize import EventNormalizer, object_json

from .conftest import make_agent_message, make_end_turn, make_status_idle

_SCOPE = Scope(
    tenant_id="offline-tenant",
    account_id="offline-account",
    principal_id="benchmark-host",
    authorization_id="offline-transport-replay",
)
_REF = ResourceRef(
    id="session",
    kind="session",
    provider="anthropic",
    account_scope_id="offline-workspace",
    tenant_id=_SCOPE.tenant_id,
    account_id=_SCOPE.account_id,
)
_NOW = datetime(2026, 1, 1, tzinfo=UTC)


class _Lifecycle(RecordingLifecycle):
    def __init__(self, start: int) -> None:
        super().__init__()
        self.first_event_ms: float | None = None
        self._start = start
        self.normalized: list[dict[str, object]] = []
        self._normalizer = EventNormalizer(_REF)

    async def on_sse_event(self, event: Any) -> None:
        normalized = self._normalizer.normalize(
            object_json(event.model_dump(mode="json")), observed_at=_NOW
        )
        if self.first_event_ms is None:
            self.first_event_ms = (time.perf_counter_ns() - self._start) / 1_000_000
        self.normalized.append(
            {
                "type": normalized.type,
                "authority": normalized.authority,
                "payload": dict(normalized.payload),
            }
        )
        await super().on_sse_event(event)


class TurnReplayAdapter:
    offline = True

    async def replay(self, path: Literal["legacy", "mux"]) -> Evidence:
        # Every invocation owns its fixture/client/stream/state. The transport
        # fails closed on unexpected calls and offers no network fallback.
        start = time.perf_counter_ns()
        transport = ScriptedTransport()
        lifecycle = _Lifecycle(start)
        transport.queue(
            ScriptedReply.stream(
                "/v1/sessions/session/events/stream",
                [
                    make_agent_message(event_id="message", text="offline reply").model_dump(
                        mode="json"
                    ),
                    BetaManagedAgentsSpanModelRequestEndEvent(
                        id="usage",
                        type="span.model_request_end",
                        model_request_start_id="start",
                        model_usage=ma_model_usage(
                            input_tokens=100,
                            output_tokens=50,
                            cache_creation_input_tokens=7,
                            cache_read_input_tokens=3,
                        ),
                        processed_at=_NOW,
                    ).model_dump(mode="json"),
                    make_status_idle(event_id="ended", stop_reason=make_end_turn()).model_dump(
                        mode="json"
                    ),
                ],
            ),
            ScriptedReply("POST", "/v1/sessions/session/events", send_events_response()),
        )
        with patch.dict(os.environ, {"DAIMON_TURN__PATH": path}):
            selected = load_turn_settings(_env_file=None).path
            async with transport.client() as client:
                state = await run_turn(
                    anthropic=client,
                    session_id="session",
                    user_message="offline question",
                    lifecycle=lifecycle,
                    cancel=asyncio.Event(),
                    billing=BillingExempt(reason="headless-unrecorded"),
                    scope=_SCOPE,
                    now=lambda: _NOW,
                )
        transport.assert_consumed()
        assert lifecycle.first_event_ms is not None
        effects = {
            "http": [request.to_dict() for request in transport.requests],
            "events": lifecycle.normalized,
            "content": [dataclasses.asdict(block) for block in state.content],
            "usage": dataclasses.asdict(state.usage_totals),
            "termination": state.termination,
            "success": len(lifecycle.terminal_success),
            "failures": len(lifecycle.terminal_failures),
        }
        # GET requests have an empty bytes body in RecordedRequest. Canonical
        # empty strings preserve the wire fact and make the digest JSON-safe.
        encoded = json.dumps(effects, sort_keys=True, default=lambda value: value.decode()).encode()
        return Evidence(
            selected_path=selected,
            effect_digest=hashlib.sha256(encoded).hexdigest(),
            transport_calls=len(transport.requests),
            first_event_ms=lifecycle.first_event_ms,
            external_calls=0,
        )


def build() -> TurnReplayAdapter:
    return TurnReplayAdapter()
