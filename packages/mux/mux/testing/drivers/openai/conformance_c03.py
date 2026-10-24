"""Explicit offline QA policy; never installed by a product driver or default runtime."""

from __future__ import annotations

from mux.drivers.openai.transport import Method, Object, object_json


class C03Policy:
    """Three requests: one required create input and two canonical event POSTs.

    Tools=[] does not disable a hosted sandbox's implicit shell. This policy
    requires conversation_only/none and independently checks native readbacks.
    Counts advance before dispatch; a failed/ambiguous POST cannot be retried.
    Resource ownership and cleanup remain the enclosing runner's responsibility.
    """

    def __init__(self) -> None:
        self.agent_creates = 0
        self.session_creates = 0
        self.event_posts = 0
        self.agent_checks = 0
        self.session_checks = 0

    def request(self, method: Method, path: str, body: Object | None) -> Object | None:
        if method != "POST":
            return body
        if path == "/agents":
            if self.agent_creates or body is None or body.get("model") != "gpt-6-luna":
                raise ValueError("C03 agent create refused")
            if body.get("tools") != []:
                raise ValueError("C03 explicit empty tools required")
            self.agent_creates += 1
            return {**body, "service_tier": "default", "multi_agent": {"enabled": False}}
        if path == "/agents/sessions":
            if self.session_creates or body is None or body.get("environment") != {"type": "none"}:
                raise ValueError("C03 environment must be none")
            initial = body.get("input")
            if not isinstance(initial, list) or len(initial) != 1 or "spend_control" in body:
                raise ValueError("C03 requires one initial input and no native cap")
            self.session_creates += 1
            return {
                **body,
                "agent": {
                    **object_json(body.get("agent") or {}),
                    "model": "gpt-6-luna",
                    "tools": [],
                    "service_tier": "default",
                    "multi_agent": {"enabled": False},
                },
            }
        if path.endswith("/events"):
            events = body.get("events") if body is not None else None
            if events == [{"type": "agent.session.input.cancel"}]:
                return body
            if (
                self.session_creates != 1
                or self.event_posts >= 2
                or not isinstance(events, list)
                or len(events) != 1
                or not isinstance(events[0], dict)
                or events[0].get("type") != "agent.session.input.message"
            ):
                raise ValueError("C03 model request allowance exhausted or non-user input")
            self.event_posts += 1
            return body
        raise ValueError("C03 unexpected mutation")

    def response(self, method: Method, path: str, value: Object) -> None:
        if method == "DELETE":
            return  # Cleanup must remain possible after policy refusal.
        if path.endswith("/items") or path.endswith("/turns"):
            rows = value.get("data")
            if not isinstance(rows, list):
                raise ValueError("C03 native history is incomplete")
            for row in rows:
                if not isinstance(row, dict) or (
                    path.endswith("/items")
                    and row.get("type") not in ("message", "reasoning")
                    or path.endswith("/turns")
                    and ("subagent_id" not in row or row["subagent_id"] is not None)
                ):
                    raise ValueError("C03 native tool or delegation loop observed")
            return
        parts = path.strip("/").split("/")
        if (
            path == "/agents"
            and method == "POST"
            or len(parts) == 2
            and parts[0] == "agents"
            and parts[1] != "sessions"
        ):
            agent = value
        elif (
            path == "/agents/sessions"
            and method == "POST"
            or (len(parts) == 3 and parts[:2] == ["agents", "sessions"])
        ):
            if value.get("environment") != {"type": "none"}:
                raise ValueError("C03 native environment is not none")
            agent = object_json(value.get("agent") or {})
        else:
            return
        if (
            agent.get("tools") != []
            or agent.get("model") != "gpt-6-luna"
            or agent.get("service_tier") != "default"
            or object_json(agent.get("multi_agent") or {}).get("enabled") is not False
        ):
            raise ValueError("C03 native tools/model/tier/delegation differs from bound")
        if path == "/agents/sessions" or len(parts) == 3:
            self.session_checks += 1
        else:
            self.agent_checks += 1
