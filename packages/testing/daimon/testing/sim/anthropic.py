"""Managed Agents and Messages, simulated: the stateful MA fakes behind strict shapes."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, cast

import httpx
from anthropic.types.beta.agent_create_params import AgentCreateParams
from anthropic.types.beta.agent_update_params import AgentUpdateParams
from anthropic.types.beta.environment_create_params import EnvironmentCreateParams
from anthropic.types.beta.memory_store_create_params import MemoryStoreCreateParams
from anthropic.types.beta.session_create_params import SessionCreateParams
from anthropic.types.beta.sessions.event_send_params import EventSendParams
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from daimon.testing.ma_sessions import FakeSessionsState, make_fake_sessions_handler
from daimon.testing.sim.network import StrictFake
from daimon.testing.sim.script import Script, Turn
from mux.testing.drivers.anthropic.ma import (
    FakeMAState,
    FakeMemoryStoreState,
    NotHandled,
    list_response,
    ma_id,
    make_fake_ma_handler,
    make_fake_memory_store_handler,
    skill_response,
)

HOST = "api.anthropic.com"
_ID = r"([^/]+)"
# A JSON object as the API returns it.
Json = Any
_LATENCY_S = 0.05


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class AnthropicAPI(StrictFake):
    """The Anthropic API one tenant's turns reach: agents, environments, skills, sessions."""

    def __init__(self, script: Script) -> None:
        super().__init__(host=HOST)
        self.script = script
        self.ma = FakeMAState()
        self.sessions = FakeSessionsState(ma=self.ma)
        self.environments: dict[str, dict[str, Any]] = {}
        self.skills: dict[str, dict[str, Any]] = {}
        self.stores = FakeMemoryStoreState()
        self._stores = make_fake_memory_store_handler(self.stores)
        self._paused: dict[str, list[dict[str, Any]]] = {}
        self.reported: list[tuple[int, int]] = []
        self._tick: dict[str, asyncio.Event] = {}
        self._due: dict[str, list[dict[str, Any]]] = {}
        self._listening: dict[str, int] = {}
        self._agents = make_fake_ma_handler(self.ma)
        self._sessions = make_fake_sessions_handler(self.sessions)
        self.shape("POST", "/v1/agents", AgentCreateParams)
        self.shape("POST", f"/v1/agents/{_ID}", AgentUpdateParams)
        self.shape("POST", "/v1/environments", EnvironmentCreateParams)
        self.shape("POST", "/v1/sessions", SessionCreateParams)
        self.shape("POST", f"/v1/sessions/{_ID}/events", EventSendParams)
        self.shape("POST", "/v1/messages", MessageCreateParamsNonStreaming)
        self.route("GET", f"/v1/sessions/{_ID}/events/stream", self._stream)
        self.route("POST", f"/v1/sessions/{_ID}/events", self._send)
        self.route("POST", "/v1/messages", self._message)
        self.route("GET", "/v1/models", self._models)
        self.route("GET", "/v1/environments", self._list_environments)
        self.route("POST", "/v1/environments", self._create_environment)
        self.route("GET", f"/v1/environments/{_ID}", self._get_environment)
        self.route("GET", "/v1/skills", lambda r, m: list_response(list(self.skills.values())))
        self.route("POST", "/v1/skills", self._create_skill)
        self.route("GET", f"/v1/skills/{_ID}", self._get_skill)
        self.route("GET", f"/v1/skills/{_ID}/versions", self._skill_versions)
        self.route("POST", f"/v1/agents/{_ID}/archive", self._archive_agent)
        self.shape("POST", "/v1/memory_stores", MemoryStoreCreateParams)
        for method in ("GET", "POST", "DELETE"):
            self.route(method, r"/v1/memory_stores(/.*)?", self._store_api)
        for method in ("GET", "POST", "DELETE"):
            self.route(method, r"/v1/(sessions|files)(/.*)?", self._session_api)
            self.route(method, r"/v1/agents(/.*)?", self._agent_api)

    def refuse(self, status: int, message: str, param: str | None = None) -> httpx.Response:
        kind = "not_found_error" if status == 404 else "invalid_request_error"
        return httpx.Response(
            status, json={"type": "error", "error": {"type": kind, "message": message}}
        )

    def _store_api(self, request: httpx.Request, _: re.Match[str]) -> httpx.Response:
        try:
            return self._stores(request)
        except NotHandled:
            self.violations.append(f"unserved {request.method} {request.url.path}")
            return self.refuse(404, "not found")

    def _send(self, request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        """A person's message or answer starts the next scripted model turn."""
        response = self._session_api(request, match)
        kinds = {e["type"] for e in json.loads(request.content)["events"]}
        if response.status_code == 200 and kinds & {"user.message", "user.tool_confirmation"}:
            sid = match[1]
            events = self._paused.pop(sid, None) or self._render(sid, self.script.next())
            self._set_status(sid, "running")
            self._due[sid] = events
            if self._listening.get(sid):
                self._answer(sid)
        return response

    def _answer(self, sid: str) -> None:
        """The model's events land a moment later; deterministic, it waits for a listener."""
        if (events := self._due.pop(sid, None)) is not None:
            asyncio.get_running_loop().call_later(_LATENCY_S, self._emit, sid, events)

    def _emit(self, sid: str, events: list[dict[str, Any]]) -> None:
        self.sessions.events[sid].extend(events)
        self._set_status(sid, "idle")
        self._tick.pop(sid, asyncio.Event()).set()

    def _set_status(self, sid: str, status: str) -> None:
        session = self.sessions.sessions[sid]
        self.sessions.sessions[sid] = session.model_copy(update={"status": status})

    def _session_api(self, request: httpx.Request, _: re.Match[str]) -> httpx.Response:
        try:
            return self._sessions(request)
        except NotHandled:
            self.violations.append(f"unserved {request.method} {request.url.path}")
            return self.refuse(404, "not found")

    def _agent_api(self, request: httpx.Request, _: re.Match[str]) -> httpx.Response:
        response = self._agents(request)
        if response.status_code == 404 and "unhandled" in response.text:
            self.violations.append(f"unserved {request.method} {request.url.path}")
        if request.method == "POST" and response.status_code == 200:
            agent = self._echo(response.json())
            self.ma.agents[agent["id"]] = agent
            self.ma.agent_versions[agent["id"]][agent["version"]] = dict(agent)
            return httpx.Response(200, json=agent)
        return response

    def _echo(self, agent: Json) -> Json:
        """Fill the defaults the API adds to a stored agent: skill versions, tool policies."""
        for skill in cast("list[Json]", agent.get("skills") or []):
            if skill.get("version") in (None, "latest"):
                known = self.skills.get(skill["skill_id"], {})
                skill["version"] = known.get("latest_version", "1")
        for tool in cast("list[Json]", agent.get("tools") or []):
            if "default_config" not in tool and "configs" not in tool:
                continue
            default: Json = tool.setdefault("default_config", {})
            default.setdefault("enabled", True)
            default.setdefault("permission_policy", {"type": "always_allow"})
            for config in cast("list[Json]", tool.get("configs") or []):
                config.setdefault("enabled", default["enabled"])
                config.setdefault("permission_policy", default["permission_policy"])
        return agent

    def _create_skill(self, request: httpx.Request, _: re.Match[str]) -> httpx.Response:
        title = re.search(rb'name="display_title"\r\n\r\n([^\r]*)', request.content)
        skill = skill_response(ma_id("skill"), title[1].decode() if title else None)
        self.skills[skill["id"]] = skill
        return httpx.Response(200, json=skill)

    def _get_skill(self, _: httpx.Request, match: re.Match[str]) -> httpx.Response:
        skill = self.skills.get(match[1])
        return httpx.Response(200, json=skill) if skill else self.refuse(404, "no such skill")

    def _skill_versions(self, _: httpx.Request, match: re.Match[str]) -> httpx.Response:
        skill = self.skills.get(match[1])
        if skill is None:
            return self.refuse(404, "no such skill")
        row = {
            "id": ma_id("skillver"),
            "type": "skill_version",
            "skill_id": skill["id"],
            "version": skill["latest_version"],
            "name": skill["display_title"],
            "description": "",
            "directory": skill["display_title"],
            "created_at": skill["created_at"],
        }
        return list_response([row])

    def _archive_agent(self, _: httpx.Request, match: re.Match[str]) -> httpx.Response:
        agent = self.ma.agents.get(match[1])
        if agent is None:
            return self.refuse(404, "no such agent")
        agent["archived_at"] = _now()
        return httpx.Response(200, json=agent)

    def _stream(self, _: httpx.Request, match: re.Match[str]) -> httpx.Response:
        """Every event after the connect, as it lands, up to the next idle."""
        sid = match[1]
        log = self.sessions.events.setdefault(sid, [])
        cursor = len(log)

        async def frames() -> AsyncIterator[bytes]:
            nonlocal cursor
            self._listening[sid] = self._listening.get(sid, 0) + 1
            self._answer(sid)
            try:
                async for frame in _follow():
                    yield frame
            finally:
                self._listening[sid] -= 1

        async def _follow() -> AsyncIterator[bytes]:
            nonlocal cursor
            while True:
                while cursor == len(log):
                    await self._tick.setdefault(sid, asyncio.Event()).wait()
                event, cursor = log[cursor], cursor + 1
                yield f"data: {json.dumps(event)}\n\n".encode()
                if event["type"] == "session.status_idle":
                    return

        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=frames())

    def _render(self, sid: str, turn: Turn) -> list[dict[str, Any]]:
        """MA events for one model turn; an `always_ask` tool pauses for the person."""
        events = [self._event("session.status_running")]
        tail = [
            self._event("agent.message", content=[{"type": "text", "text": turn.text}]),
            self._event("span.model_request_end", **self._usage(turn)),
            self._event("session.status_idle", stop_reason={"type": "end_turn"}),
        ]
        if turn.tool is None:
            return events + tail
        tool, mcp = turn.tool, turn.tool.server is not None
        call = self._event(
            "agent.mcp_tool_use" if mcp else "agent.tool_use",
            name=tool.name,
            input=tool.input,
            evaluated_permission=self._permission(sid, tool.name, tool.server),
            **({"mcp_server_name": tool.server} if mcp else {}),
        )
        result = self._event(
            "agent.mcp_tool_result" if mcp else "agent.tool_result",
            **{"mcp_tool_use_id" if mcp else "tool_use_id": call["id"]},
            content=[{"type": "text", "text": tool.result}],
            is_error=False,
        )
        if call["evaluated_permission"] != "ask":
            return [*events, call, result, *tail]
        self._paused[sid] = [self._event("session.status_running"), result, *tail]
        stop = {"type": "requires_action", "event_ids": [call["id"]]}
        return [*events, call, self._event("session.status_idle", stop_reason=stop)]

    def _permission(self, sid: str, name: str, server: str | None) -> str:
        """What the session's agent policy says for this tool, as the API evaluates it."""
        agent: Json = self.sessions.sessions[sid].agent.model_dump(mode="json")
        for toolset in agent["tools"]:
            if toolset.get("mcp_server_name") != server or "default_config" not in toolset:
                continue
            configs = cast("list[Json]", toolset.get("configs") or [])
            config: Json = next((c for c in configs if c["name"] == name), None)
            policy: str = (config or toolset["default_config"])["permission_policy"]["type"]
            return "ask" if policy == "always_ask" else "allow"
        raise AssertionError(f"the session's agent has no tool {name!r} on {server!r}")

    def _usage(self, turn: Turn) -> dict[str, Any]:
        self.reported.append((turn.input_tokens, turn.output_tokens))
        return {
            "is_error": False,
            "model_request_start_id": ma_id("sevt"),
            "model_usage": {
                "input_tokens": turn.input_tokens,
                "output_tokens": turn.output_tokens,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        }

    @staticmethod
    def _event(kind: str, **fields: object) -> dict[str, Any]:
        return {"id": ma_id("sevt"), "type": kind, "processed_at": _now(), **fields}

    def _message(self, request: httpx.Request, _: re.Match[str]) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": ma_id("msg"),
                "type": "message",
                "role": "assistant",
                "model": body["model"],
                "content": [{"type": "text", "text": "Simulated chat"}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 40, "output_tokens": 4},
            },
        )

    def _models(self, _: httpx.Request, __: re.Match[str]) -> httpx.Response:
        ids = ("claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5")
        rows = [
            {"id": i, "type": "model", "display_name": i, "created_at": "2026-01-01T00:00:00Z"}
            for i in ids
        ]
        return httpx.Response(
            200, json={"data": rows, "has_more": False, "first_id": ids[0], "last_id": ids[-1]}
        )

    def _list_environments(self, _: httpx.Request, __: re.Match[str]) -> httpx.Response:
        return list_response(list(self.environments.values()))

    def _create_environment(self, request: httpx.Request, _: re.Match[str]) -> httpx.Response:
        body = json.loads(request.content)
        env = {
            "id": ma_id("env"),
            "type": "environment",
            "name": body["name"],
            "description": body.get("description") or "",
            "config": body.get("config")
            or {"type": "cloud", "networking": {"type": "unrestricted"}},
            "metadata": body.get("metadata") or {},
            "created_at": _now(),
            "updated_at": _now(),
            "archived_at": None,
        }
        self.environments[str(env["id"])] = env
        return httpx.Response(200, json=env)

    def _get_environment(self, _: httpx.Request, match: re.Match[str]) -> httpx.Response:
        env = self.environments.get(match[1])
        return httpx.Response(200, json=env) if env else self.refuse(404, "no such environment")
