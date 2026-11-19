"""OpenAI Agents (sessions API), simulated: stateful resources and scripted turns.

The SDK has no types for this API, so the request shapes below are written from the
API reference. They are closed: an undeclared key is a 400 `Unknown parameter`, as
the API answers (#1394). Replies carry what the API adds on its own: web search and
MCP tool defaults echoed onto agents and sessions (#1148), and a turn resource
that reports the usage its terminal event carried only a moment later (#1769).
"""

from __future__ import annotations

import asyncio
import itertools
import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal, NotRequired, Required, TypedDict

import httpx
from daimon.testing.sim.network import StrictFake
from daimon.testing.sim.script import Script, Turn

Json = Any
_LATENCY_S = 0.05
#: When a turn resource fills in usage its terminal event already carried (#1769).
_LATE_USAGE_S = 2.0


class _Mcp(TypedDict, total=False):
    type: Required[Literal["mcp"]]
    server_label: Required[str]
    server_url: str
    server_description: str
    allowed_tools: list[str] | None
    require_approval: Json
    headers: dict[str, str] | None
    authorization: str
    credential_id: str | None
    connection_origin: Literal["service", "user"]
    request_metadata: dict[str, str]
    transport: Json


class _Function(TypedDict, total=False):
    type: Required[Literal["function"]]
    name: Required[str]
    description: str
    parameters: dict[str, Json]
    strict: bool


class _Builtin(TypedDict, total=False):
    type: Required[Literal["web_search", "shell", "apply_patch", "code_interpreter"]]
    mode: Literal["live", "cached"]
    context_size: Literal["low", "medium", "high"]
    allowed_domains: list[str] | None
    location: Json


type _Tool = _Mcp | _Function | _Builtin


class AgentCreate(TypedDict, total=False):
    name: Required[str]
    model: Required[str]
    instructions: str
    tools: list[_Tool]
    metadata: dict[str, str]
    reasoning: dict[str, str]
    multi_agent: dict[str, bool]


class _AgentOverride(TypedDict, total=False):
    model: str
    tools: list[_Tool]
    reasoning: dict[str, str]
    multi_agent: dict[str, bool]


class _Environment(TypedDict, total=False):
    type: Required[Literal["openai_hosted", "none"]]
    container_size: str
    environment_template_id: str
    files: list[Json]
    skills: list[Json]


class _Input(TypedDict):
    role: Literal["user"]
    content: list[Json]


class SessionCreate(TypedDict, total=False):
    agent_id: Required[str]
    environment: Required[_Environment]
    metadata: dict[str, str]
    agent: _AgentOverride
    vault_ids: list[str]
    input: list[_Input]


class TemplateCreate(TypedDict, total=False):
    name: Required[str]
    network: dict[str, Json]
    packages: Json
    files: list[Json]
    sources: list[Json]


class _InputEvent(TypedDict, total=False):
    type: Required[
        Literal[
            "agent.session.input.message",
            "agent.session.input.tool_result",
            "agent.session.input.cancel",
            "agent.session.input.computer_use_approval_request_result",
        ]
    ]
    input: list[_Input]
    call_id: str
    turn_id: str
    success: bool
    output: Json
    error: str
    request_id: str
    response: Json


class EventsSend(TypedDict):
    events: NotRequired[list[_InputEvent]]


# What the API fills into a tool it echoes back (#1148).
_WEB_SEARCH = {"mode": "live", "context_size": "medium", "allowed_domains": None, "location": None}
_MCP: dict[str, Json] = {
    "credential_id": None,
    "request_metadata": {},
    "connection_origin": "service",
}


def _echo_tools(tools: list[Json]) -> list[Json]:
    out: list[Json] = []
    for tool in tools:
        if tool.get("type") == "web_search":
            out.append({**_WEB_SEARCH, **tool})
        elif tool.get("type") == "mcp":
            given: dict[str, Json] = tool.get("transport") or {}
            transport: dict[str, Json] = {"headers": {}, **given}
            out.append({**_MCP, **tool, "transport": transport})
        else:
            out.append(tool)
    return out


@dataclass
class _Session:
    raw: dict[str, Json]
    log: list[dict[str, Json]] = field(default_factory=list[dict[str, Json]])
    turns: list[dict[str, Json]] = field(default_factory=list[dict[str, Json]])
    items: list[dict[str, Json]] = field(default_factory=list[dict[str, Json]])
    tick: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class OpenAIAPI(StrictFake):
    """api.openai.com: agents, sessions, skills, vaults and templates, plus turns."""

    script: Script = field(default_factory=Script)
    host: str = "api.openai.com"
    # Turn reads say usage null for a moment after the event that carried it (#1769).
    late_usage: bool = False
    reported: list[tuple[int, int]] = field(default_factory=list[tuple[int, int]])
    resources: dict[str, dict[str, Json]] = field(default_factory=dict[str, dict[str, Json]])
    sessions: dict[str, _Session] = field(default_factory=dict[str, _Session])
    _ids: itertools.count[int] = field(default_factory=itertools.count)

    def __post_init__(self) -> None:
        base = "/v1/agents/sessions/([^/]+)"
        self.shape("POST", "/v1/agents", AgentCreate)
        self.shape("POST", "/v1/agents/sessions", SessionCreate)
        self.shape("POST", "/v1/agents/environments/templates", TemplateCreate)
        self.shape("POST", base + "/events", EventsSend)
        self.route("GET", base + "/events", self._stream)
        self.route("POST", base + "/events", self._send)
        self.route(
            "GET",
            base + "/turns",
            lambda r, m: self._missing(m[1]) or self._page(self.sessions[m[1]].turns, r),
        )
        self.route("GET", base + "/turns/([^/]+)", self._turn)
        self.route("GET", base + "/turns/([^/]+)/items", self._turn_items)
        self.route(
            "GET",
            base + "/items",
            lambda r, m: self._missing(m[1]) or self._page(self.sessions[m[1]].items, r),
        )
        self.route("POST", "/v1/agents/sessions", self._create_session)
        self.route(
            "GET", base, lambda r, m: self._missing(m[1]) or self._json(self.sessions[m[1]].raw)
        )
        self.route("DELETE", base, self._delete_session)
        self.route("GET", "/v1/agents/sessions", lambda r, m: self._page(self._raws(), r))
        for kind, path in (
            ("tmpl", "/v1/agents/environments/templates"),
            ("agent", "/v1/agents"),
            ("vault", "/v1/vaults"),
        ):
            self.route("POST", path, self._creator(kind, path))
            self.route("GET", path, self._lister(path))
            self.route("GET", path + "/([^/]+)", self._getter)
            self.route("DELETE", path + "/([^/]+)", self._deleter)
        self.route("POST", "/v1/vaults/([^/]+)/credentials", self._credential)
        self.route("GET", "/v1/vaults/([^/]+)/credentials", self._lister_under)
        self.route("GET", "/v1/vaults/[^/]+/credentials/([^/]+)", self._getter)
        self.route("DELETE", "/v1/vaults/[^/]+/credentials/([^/]+)", self._deleter)
        self.route("POST", "/v1/skills", self._create_skill)
        self.route("GET", "/v1/skills", self._lister("/v1/skills"))
        self.route("GET", "/v1/skills/([^/]+)", self._getter)
        self.route("DELETE", "/v1/skills/([^/]+)", self._deleter)
        self.route("POST", "/v1/skills/([^/]+)/versions", self._skill_version)
        self.route("GET", "/v1/skills/([^/]+)/versions/([^/]+)", self._get_version)

    def refuse(self, status: int, message: str, param: str | None = None) -> httpx.Response:
        if message.endswith("Unknown parameter"):
            message = f"Unknown parameter: '{message.split(':')[0].removeprefix('$.')}'."
        error = {"message": message, "type": "invalid_request_error", "param": param, "code": None}
        return httpx.Response(status, json={"error": error})

    # resources

    def _missing(self, session_id: str) -> httpx.Response | None:
        """The API's 404 for a session that is gone, as a deleted one answers."""
        if session_id in self.sessions:
            return None
        return self.refuse(404, f"No such session: '{session_id}'")

    def _listed(self, session_id: str, kind: str, request: httpx.Request) -> httpx.Response:
        found = self._missing(session_id)
        return found or self._page(getattr(self.sessions[session_id], kind), request)

    def _id(self, kind: str) -> str:
        return f"{kind}_{next(self._ids):04d}"

    def _now(self) -> int:
        return 1_760_000_000 + len(self.seen)

    def _json(self, value: Json, status: int = 200) -> httpx.Response:
        return httpx.Response(status, json=value)

    def _page(self, values: list[dict[str, Json]], request: httpx.Request) -> httpx.Response:
        rows = values[::-1] if request.url.params.get("order") == "desc" else list(values)
        if after := request.url.params.get("after"):
            ids = [row["id"] for row in rows]
            rows = rows[ids.index(after) + 1 :] if after in ids else []
        last = rows[-1]["id"] if rows else None
        return self._json({"object": "list", "data": rows, "has_more": False, "last_id": last})

    def _store(self, path: str, raw: dict[str, Json]) -> httpx.Response:
        raw["_path"] = path
        self.resources[raw["id"]] = raw
        return self._json(self._public(raw))

    def _public(self, raw: dict[str, Json]) -> dict[str, Json]:
        return {k: v for k, v in raw.items() if not k.startswith("_")}

    def _creator(self, kind: str, path: str):
        def create(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
            body = json.loads(request.content)
            raw: dict[str, Json] = {
                "id": self._id(kind),
                "object": kind,
                "created_at": self._now(),
                **body,
            }
            if "tools" in raw:
                raw["tools"] = _echo_tools(raw["tools"])
            if kind == "tmpl":
                raw.setdefault("files", [])
            return self._store(path, raw)

        return create

    def _lister(self, path: str):
        def listing(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
            rows = [self._public(r) for r in self.resources.values() if r["_path"] == path]
            return self._page(rows, request)

        return listing

    def _lister_under(self, request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        rows = [self._public(r) for r in self.resources.values() if r.get("vault_id") == match[1]]
        return self._page(rows, request)

    def _getter(self, request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        raw = self.resources.get(match[1])
        if raw is None:
            return self.refuse(404, f"No such object: '{match[1]}'")
        return self._json(self._public(raw))

    def _deleter(self, request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        self.resources.pop(match[1], None)
        return self._json({"id": match[1], "deleted": True})

    def _credential(self, request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        body = json.loads(request.content)
        raw = {"id": self._id("cred"), "vault_id": match[1], "created_at": self._now(), **body}
        return self._store(f"/v1/vaults/{match[1]}/credentials", raw)

    def _fields(self, request: httpx.Request) -> dict[str, str]:
        found = re.findall(rb'name="([^"]+)"\r\n\r\n([^\r]*)\r\n', request.content)
        return {k.decode(): v.decode() for k, v in found}

    def _create_skill(self, request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        fields = self._fields(request)
        id_ = self._id("skill")
        raw: dict[str, Json] = {
            "id": id_,
            "object": "skill",
            "name": fields.get("name") or id_,
            "description": fields.get("description"),
            "latest_version": "1",
            "default_version": "1",
            "created_at": self._now(),
        }
        self.resources[f"{id_}@1"] = {**raw, "skill_id": id_, "version": "1", "_path": "version"}
        return self._store("/v1/skills", raw)

    def _skill_version(self, request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        skill = self.resources[match[1]]
        version = str(int(skill["latest_version"]) + 1)
        skill["latest_version"] = version
        raw = {**skill, "skill_id": match[1], "version": version, "_path": "version"}
        self.resources[f"{match[1]}@{version}"] = raw
        return self._json(self._public(raw))

    def _get_version(self, request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        raw = self.resources.get(f"{match[1]}@{match[2]}")
        if raw is None:
            return self.refuse(404, "No such skill version")
        return self._json(self._public(raw))

    # sessions and turns

    def _raws(self) -> list[dict[str, Json]]:
        return [s.raw for s in self.sessions.values()]

    def _create_session(self, request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        body = json.loads(request.content)
        agent = self.resources.get(body["agent_id"])
        if agent is None:
            return self.refuse(404, f"No such agent: '{body['agent_id']}'")
        override = dict(body.get("agent") or {})
        if "tools" in override:
            override["tools"] = _echo_tools(override["tools"])
        environment = {**body["environment"]}
        if environment["type"] == "openai_hosted":
            environment["id"] = self._id("env")
        id_ = self._id("sess")
        raw: dict[str, Json] = {
            "id": id_,
            "object": "agent.session",
            "created_at": self._now(),
            "agent": {**self._public(agent), **override},
            "environment": environment,
            "metadata": body.get("metadata") or {},
            "vault_ids": body.get("vault_ids") or [],
            "status": "idle",
            "required_actions": [],
        }
        self.sessions[id_] = _Session(raw)
        if body.get("input"):
            self._start(id_, body["input"])
        return self._json(raw)

    def _delete_session(self, request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        self.sessions.pop(match[1], None)
        return self._json({"id": match[1], "deleted": True})

    def _send(self, request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        for event in json.loads(request.content).get("events", []):
            if event["type"] == "agent.session.input.message":
                self._start(match[1], event["input"])
        return self._json({}, 202)

    def _turn(self, request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        turn = next((t for t in self.sessions[match[1]].turns if t["id"] == match[2]), None)
        if turn is None:
            return self.refuse(404, "No such turn")
        return self._json(turn)

    def _turn_items(self, request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        items = [i for i in self.sessions[match[1]].items if i["turn_id"] == match[2]]
        return self._page(items, request)

    def _start(self, sid: str, given: list[Json]) -> None:
        session = self.sessions[sid]
        turn_id = self._id("turn")
        user = self._item(turn_id, "message", role="user", content=given[0]["content"])
        session.items.append(user)
        turn = {
            "id": turn_id,
            "object": "agent.session.turn",
            "session_id": sid,
            "agent_id": session.raw["agent"]["id"],
            "subagent_id": None,
            "status": "in_progress",
            "created_at": self._now(),
            "usage": None,
        }
        session.turns.append(turn)
        session.raw["status"] = "in_progress"
        asyncio.get_running_loop().call_later(_LATENCY_S, self._emit, sid, turn, self.script.next())

    def _item(self, turn_id: str, kind: str, **fields: Json) -> dict[str, Json]:
        return {
            "id": self._id("item"),
            "turn_id": turn_id,
            "type": kind,
            "status": "completed",
        } | fields

    def _event(self, sid: str, kind: str, **fields: Json) -> dict[str, Json]:
        return {
            "type": "agent.session." + kind,
            "event_id": self._id("evt"),
            "session_id": sid,
        } | fields

    def _emit(self, sid: str, turn: dict[str, Json], script: Turn) -> None:
        session = self.sessions.get(sid)
        if session is None:
            return
        tid = turn["id"]
        events = [self._event(sid, "turn.in_progress", turn=dict(turn))]
        if script.tool is not None:
            call = self._item(
                tid,
                "mcp_call",
                name=script.tool.name,
                server_label=script.tool.server or "tools",
                arguments=json.dumps(script.tool.input),
                output=script.tool.result,
                error=None,
            )
            session.items.append(call)
            events.append(self._event(sid, "turn.item.done", turn_id=tid, item=call))
        answer = self._item(
            tid,
            "message",
            role="assistant",
            phase="final_answer",
            content=[{"type": "output_text", "text": script.text, "annotations": []}],
        )
        session.items.append(answer)
        usage = {
            "input_tokens": script.input_tokens,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": script.output_tokens,
            "output_tokens_details": {"reasoning_tokens": 0},
        }
        self.reported.append((script.input_tokens, script.output_tokens))
        turn.update(status="completed", usage=None if self.late_usage else usage)
        if self.late_usage:
            asyncio.get_running_loop().call_later(_LATE_USAGE_S, turn.update, {"usage": usage})
        ended = {**turn, "usage": usage}
        events += [
            self._event(sid, "turn.item.done", turn_id=tid, item=answer),
            self._event(sid, "turn.completed", turn=ended),
        ]
        session.raw["status"] = "idle"
        session.log.extend(events)
        session.tick.set()
        session.tick = asyncio.Event()

    async def _stream(self, request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        session = self.sessions[match[1]]
        cursor = len(session.log)

        async def follow() -> AsyncIterator[bytes]:
            nonlocal cursor
            while True:
                tick = session.tick
                while cursor < len(session.log):
                    event = session.log[cursor]
                    cursor += 1
                    yield f"data: {json.dumps(event)}\n\n".encode()
                await tick.wait()

        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=follow())
