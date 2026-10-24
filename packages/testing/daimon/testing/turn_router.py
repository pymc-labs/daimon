"""One `MARouter` for a whole faked turn: responder resolution plus the SSE
event stream `run_turn` consumes.

Shared by the adapter parity drivers, the integration turn tests, and the
MCP hub `ask` tests, so every tree fakes a turn the same way.
"""

from __future__ import annotations

import itertools
import re
from datetime import UTC, datetime
from typing import Any

import httpx
from anthropic.types.beta.sessions.beta_managed_agents_agent_message_event import (
    BetaManagedAgentsAgentMessageEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_session_end_turn import (
    BetaManagedAgentsSessionEndTurn,
)
from anthropic.types.beta.sessions.beta_managed_agents_session_status_idle_event import (
    BetaManagedAgentsSessionStatusIdleEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_span_model_request_end_event import (
    BetaManagedAgentsSpanModelRequestEndEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_text_block import (
    BetaManagedAgentsTextBlock,
)
from daimon.testing.ma_models import ma_agent, ma_environment
from mux.testing.drivers.anthropic.ma import (
    MARouter,
    NotHandled,
    json_body,
    list_response,
    send_events_response,
    sse_response,
)
from mux.testing.drivers.anthropic.ma_models import DEFAULT_MODEL_ID, ma_model_usage, ma_session

AGENT_TEXT = "Hello from the agent!"
AGENT_ID = "ag_parity_test"
ENV_ID = "env_parity_test"
MODEL_ID = DEFAULT_MODEL_ID


def turn_events(
    *,
    agent_text: str = AGENT_TEXT,
    usage_event_id: str | None = "evt_parity_usage",
    input_tokens: int = 100,
    output_tokens: int = 50,
    event_id_suffix: str = "",
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """The SSE script of one finished turn, as JSON-ready event dicts.

    `agent.message` carrying `agent_text`, then (unless `usage_event_id` is
    None) the `span.model_request_end` event `usage_record` bills on, then a
    terminal `session.status_idle`. `event_id_suffix` is appended to every
    event id so repeated turns on one session do not collapse under the
    `(managed_session_id, event_id)` idempotency key.
    """
    processed_at = now if now is not None else datetime.now(UTC)
    events: list[dict[str, Any]] = [
        BetaManagedAgentsAgentMessageEvent(
            id=f"evt_parity_msg{event_id_suffix}",
            type="agent.message",
            processed_at=processed_at,
            content=[BetaManagedAgentsTextBlock(type="text", text=agent_text)],
        ).model_dump(mode="json")
    ]
    if usage_event_id is not None:
        events.append(
            BetaManagedAgentsSpanModelRequestEndEvent(
                id=f"{usage_event_id}{event_id_suffix}",
                is_error=False,
                model_request_start_id=f"start_parity{event_id_suffix}",
                model_usage=ma_model_usage(input_tokens=input_tokens, output_tokens=output_tokens),
                processed_at=processed_at,
                type="span.model_request_end",
            ).model_dump(mode="json")
        )
    events.append(
        BetaManagedAgentsSessionStatusIdleEvent(
            id=f"evt_parity_idle{event_id_suffix}",
            type="session.status_idle",
            processed_at=processed_at,
            stop_reason=BetaManagedAgentsSessionEndTurn(type="end_turn"),
        ).model_dump(mode="json")
    )
    return events


def build_turn_router(
    tenant_id_str: str,
    *,
    agent_id: str = AGENT_ID,
    env_id: str = ENV_ID,
    model_id: str = MODEL_ID,
    agent_text: str = AGENT_TEXT,
    usage_event_id: str | None = "evt_parity_usage",
    input_tokens: int = 100,
    output_tokens: int = 50,
    session_id: str | None = None,
    replacement_session_ids: tuple[str, ...] = (),
    fresh_event_ids: bool = False,
    send_events_data: list[dict[str, Any]] | None = None,
    sent_event_bodies: list[dict[str, Any]] | None = None,
    stream_hits: list[str] | None = None,
    router: MARouter | None = None,
) -> MARouter:
    """Build a MARouter handling agent/environment resolution + a turn SSE stream.

    Routes registered:
      GET  /v1/agents               -- list, for resolver tag lookup
      GET  /v1/agents/{id}          -- retrieve, for re-fetch after resolve (any id)
      GET  /v1/environments         -- list, for resolver tag lookup
      GET  /v1/environments/{id}    -- retrieve, for re-fetch after resolve (any id)
      POST /v1/sessions/{id}/events -- send-initial event (run_turn)
      GET  /v1/sessions/{id}/events/stream -- SSE turn stream (run_turn)
      GET  /v1/sessions/{id}/events -- exact post-turn usage catch-up query only

    The agent and environment carry `tenant_id_str` in their metadata so the
    resolver's tag lookup finds them. The stream is `turn_events(...)`; see
    there for the script and for `usage_event_id=None`.

    Knobs:
      `session_id`        -- scope the event routes to that session id
                             only, so a turn run anywhere else has no route
                             and fails loudly (proves which session ran).
      `replacement_session_ids` -- idle sessions with retrieve and archive routes for replacements.
      `fresh_event_ids`   -- each stream open mints new event ids, for
                             scenarios running several turns on one session.
      `send_events_data`  -- the `data` echoed by `POST .../events`.
      `sent_event_bodies` -- every `POST .../events` body is appended here.
      `stream_hits`       -- every session id a stream was opened on is
                             appended here.
      `router`            -- add the routes to an existing router.

    The SSE script ends in `session.status_idle`, a real terminal event.
    `run_turn` asks MA for the session's status (`GET /v1/sessions/{id}`)
    whenever a stream ends or stalls WITHOUT one; a scenario whose script
    does not terminate needs that route registered too
    (`MARouter.add_session` or `session_response`), or the status check
    404s and the turn finalizes as an unexpected upstream failure.
    """
    agent_item = ma_agent(id=agent_id, model=model_id, tenant_id=tenant_id_str).model_dump(
        mode="json"
    )
    env_item = ma_environment(id=env_id, tenant_id=tenant_id_str).model_dump(mode="json")
    session_re = re.escape(session_id) if session_id is not None else r"[^/]+"
    counter = itertools.count()
    history: dict[str, list[dict[str, Any]]] = {}

    def _send_events(request: httpx.Request, _match: re.Match[str]) -> httpx.Response:
        if sent_event_bodies is not None:
            sent_event_bodies.append(json_body(request))
        return send_events_response(data=send_events_data)

    def _stream(_request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        if stream_hits is not None:
            stream_hits.append(match["session_id"])
        suffix = f"_{next(counter)}" if fresh_event_ids else ""
        events = turn_events(
            agent_text=agent_text,
            usage_event_id=usage_event_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            event_id_suffix=suffix,
        )
        recorded = history.setdefault(match["session_id"], [])
        seen = {event["id"] for event in recorded}
        recorded.extend(event for event in events if event["id"] not in seen)
        return sse_response(events)

    def _history(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        # Post-turn billing reads completed spans, with timestamp bounds for catch-up:
        # the turn-end settle reads newest first, the usage walk oldest first.
        # Replay the exact stream objects, never mint fresh events on a read.
        params = request.url.params
        if (
            params.get_list("beta") != ["true"]
            or params.get_list("order") not in (["asc"], ["desc"])
            or params.get_list("limit") != ["100"]
            or params.get_list("types[]") != ["span.model_request_end"]
        ):
            # Transcript/idle reads belong to the caller's existing routes.
            raise NotHandled
        allowed = {"beta", "types[]", "order", "limit", "created_at[gte]", "created_at[lt]"}
        assert set(params) <= allowed, f"Unscripted event-history query: {params}"
        assert params.get("beta", "true") == "true", "Unscripted event-history beta flag"
        events = list(history.get(match["session_id"], []))
        types = params.get_list("types[]")
        if types:
            events = [event for event in events if event["type"] in types]
        events.sort(key=lambda event: event["processed_at"], reverse=params["order"] == "desc")
        for key in ("created_at[gte]", "created_at[lt]"):
            if (bound := params.get(key)) is not None:
                at = datetime.fromisoformat(bound)
                events = [
                    event
                    for event in events
                    if (datetime.fromisoformat(event["processed_at"]) >= at)
                    == (key == "created_at[gte]")
                ]
        if (limit := params.get("limit")) is not None:
            assert int(limit) > 0 and len(events) <= int(limit), (
                "Event-history fixture needs a scripted continuation page"
            )
        return list_response(events)

    target = router if router is not None else MARouter()
    target.add("GET", r"/v1/agents", lambda _r, _m: list_response([agent_item]))
    target.add("GET", r"/v1/agents/[^/]+", lambda _r, _m: httpx.Response(200, json=agent_item))
    target.add("GET", r"/v1/environments", lambda _r, _m: list_response([env_item]))
    target.add("GET", r"/v1/environments/[^/]+", lambda _r, _m: httpx.Response(200, json=env_item))
    for retrieve_id in replacement_session_ids:
        target.add_session(
            ma_session(id=retrieve_id, agent_id=agent_id, environment_id=env_id), with_archive=True
        )
    target.add("POST", rf"/v1/sessions/(?P<session_id>{session_re})/events", _send_events)
    target.add("GET", rf"/v1/sessions/(?P<session_id>{session_re})/events/stream", _stream)
    target.add("GET", rf"/v1/sessions/(?P<session_id>{session_re})/events", _history)
    return target
