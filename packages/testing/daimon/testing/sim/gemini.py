"""Gemini (Interactions API), simulated: agents, environments and background interactions.

Bodies are checked against the google-genai SDK's own param types. Replies carry
what the API does on its own: the create reply stamps sub-second times while every
later read is whole seconds (#1614), and a read echoes the request's `mcp_server`
headers back (#1767), on every read, so a restarted worker must still clear it.
"""

from __future__ import annotations

import asyncio
import io
import itertools
import json
import re
import tarfile
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Required, TypedDict, cast

import httpx
from daimon.core.mcp_rpc import mcp_headers, mcp_request, open_mcp_session
from daimon.testing.sim.network import StrictFake
from daimon.testing.sim.script import Script, Turn
from google.genai._interactions.types.agent_create_params import AgentCreateParams
from google.genai._interactions.types.interaction_create_params import (
    BaseCreateAgentInteractionParams as _SDKAgentTurn,
)
from google.genai._interactions.types.interaction_create_params import Environment, Input
from google.genai._interactions.types.tool_param import ToolParam

Json = Any


class AgentCreate(AgentCreateParams, total=False):
    """The API reference's agent create; SDK 2.7.0's type predates `agent_config`."""

    agent_config: dict[str, Json]


class AgentInteractionCreate(TypedDict, total=False):
    """An interaction on a named agent, as the API reference has it; SDK 2.7.0's
    `agent_config` union predates the `antigravity` config Daimon sends."""

    agent: Required[str]
    input: Required[Input]
    agent_config: dict[str, Json]
    background: bool
    environment: Environment
    previous_interaction_id: str
    store: bool
    system_instruction: str
    tools: list[ToolParam]
    stream: bool


assert set(AgentInteractionCreate.__annotations__) - {"stream"} <= set(
    _SDKAgentTurn.__annotations__
), "every authored interaction key is one the SDK names"

_LATENCY_S = 0.05


@dataclass
class _Interaction:
    raw: dict[str, Json]
    log: list[dict[str, Json]] = field(default_factory=list[dict[str, Json]])
    done: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class GeminiAPI(StrictFake):
    """generativelanguage.googleapis.com v1beta: agents, environments, interactions."""

    script: Script = field(default_factory=Script)
    host: str = "generativelanguage.googleapis.com"
    reported: list[tuple[int, int]] = field(default_factory=list[tuple[int, int]])
    agents: dict[str, dict[str, Json]] = field(default_factory=dict[str, dict[str, Json]])
    environments: dict[str, dict[str, int]] = field(default_factory=dict[str, dict[str, int]])
    interactions: dict[str, _Interaction] = field(default_factory=dict[str, _Interaction])
    _ids: itertools.count[int] = field(default_factory=itertools.count)
    _calls: set[asyncio.Task[None]] = field(default_factory=set[asyncio.Task[None]])

    def __post_init__(self) -> None:
        one = "/v1beta/interactions/([^/]+)"
        env = "/v1beta/environments/([^/]+)"
        self.shape("POST", "/v1beta/agents", AgentCreate)
        self.shape("POST", "/v1beta/interactions", AgentInteractionCreate)
        self.route("POST", "/v1beta/agents", self._create_agent)
        self.route("GET", "/v1beta/agents/([^/]+)", self._agent)
        self.route("DELETE", "/v1beta/agents/([^/]+)", self._delete_agent)
        self.route("POST", "/v1beta/environments", self._create_environment)
        self.route("PUT", "/upload" + env + "/files/(.*)", self._upload)
        self.route("GET", env + "/files/(.*)", self._files)
        self.route("DELETE", env, lambda r, m: httpx.Response(200, json={}))
        self.route("GET", "/v1beta/files/environment-([^/:]+):download", self._download)
        self.route("POST", "/v1beta/interactions", self._create)
        self.route("GET", one, self._read)
        self.route("POST", one + ":cancel", self._cancel)
        self.route("POST", "/v1beta/models/([^/:]+):generateContent", self._generate)

    def refuse(self, status: int, message: str, param: str | None = None) -> httpx.Response:
        if message.endswith("Unknown parameter"):
            message = f'Invalid JSON payload received. Unknown name "{param}": Cannot find field.'
        code = {400: "INVALID_ARGUMENT", 404: "NOT_FOUND"}.get(status, "UNKNOWN")
        return httpx.Response(
            status, json={"error": {"code": status, "message": message, "status": code}}
        )

    def _generate(self, request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        """Daimon's own small calls (a thread's title): billed like any model call."""
        self.reported.append((40, 4))
        candidate = {
            "content": {"role": "model", "parts": [{"text": "Simulated chat"}]},
            "finishReason": "STOP",
            "index": 0,
        }
        usage = {"promptTokenCount": 40, "candidatesTokenCount": 4, "totalTokenCount": 44}
        return httpx.Response(
            200, json={"candidates": [candidate], "usageMetadata": usage, "modelVersion": match[1]}
        )

    def _id(self, kind: str) -> str:
        return f"{kind}-{next(self._ids):04d}"

    def _stamp(self) -> str:
        """Now, as the create reply stamps it: with microseconds (#1614)."""
        at = datetime.now(UTC).replace(microsecond=142775)
        return at.isoformat().replace("+00:00", "Z")

    # agents and environments

    def _create_agent(self, request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        body = json.loads(request.content)
        agent = {"id": body.get("id") or self._id("agent"), **body}
        self.agents[agent["id"]] = agent
        return httpx.Response(200, json=agent)

    def _agent(self, request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        agent = self.agents.get(match[1])
        if agent is None:
            return self.refuse(404, f"Agent {match[1]} not found.")
        return httpx.Response(200, json=agent)

    def _delete_agent(self, request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        self.agents.pop(match[1], None)
        return httpx.Response(200, json={})

    def _create_environment(self, request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        id_ = self._id("env")
        self.environments[id_] = {}
        return httpx.Response(200, json={"id": id_})

    def _upload(self, request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        files = self.environments.setdefault(match[1], {})
        files[match[2].rstrip("/") + "/archive.tar.gz"] = len(request.content)
        return httpx.Response(200, json={})

    def _files(self, request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        files = self.environments.get(match[1], {})
        listed = [
            {"path": path, "size_bytes": str(size), "type": "FILE"}
            for path, size in files.items()
            if path.startswith(match[2].strip("/"))
        ]
        return httpx.Response(200, json={"files": listed})

    # interactions

    def _create(self, request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        body = json.loads(request.content)
        id_ = self._id("interaction")
        raw: dict[str, Json] = {
            "id": id_,
            "status": "in_progress",
            "created": self._stamp(),
            "updated": self._stamp(),
            "steps": [],
            "environment_id": self._workspace(body.get("environment")),
            **{k: v for k, v in body.items() if k not in ("input", "background", "store")},
        }
        self.interactions[id_] = _Interaction(raw)
        turn = self.script.next()
        if turn.tool is not None and turn.tool.live:
            call = asyncio.get_running_loop().create_task(self._call(id_, turn, body))
            self._calls.add(call)
            call.add_done_callback(self._calls.discard)
        else:
            asyncio.get_running_loop().call_later(_LATENCY_S, self._finish, id_, turn)
        return httpx.Response(200, json=raw)

    async def _call(self, id_: str, turn: Turn, body: dict[str, Json]) -> None:
        """Call the turn's tool on the MCP server the request names (by its native
        name), as Gemini does, and answer with what it returned."""
        tool = turn.tool
        assert tool is not None
        tools: list[dict[str, Json]] = body.get("tools") or []
        servers = [t for t in tools if t.get("type") == "mcp_server"]
        server = next((t for t in servers if t.get("name") == tool.server), None)
        if server is None:
            self.violations.append(f"no MCP server {tool.server} in {servers}")
            text = "no such server"
        else:
            given: dict[str, str] = server.get("headers") or {}
            headers = {**mcp_headers(""), **given}
            call = {"name": tool.name, "arguments": tool.input}
            request = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": call}
            try:
                async with httpx.AsyncClient(follow_redirects=False) as http:
                    await open_mcp_session(http, server["url"], headers)
                    result = await mcp_request(http, server["url"], headers, request)
            except ValueError as error:
                text = f"The call failed: {error}"
            else:
                content: list[dict[str, Json]] = cast(Any, result.get("content")) or []
                text = "\n".join(str(c.get("text", "")) for c in content)
        self._finish(id_, replace(turn, tool=replace(tool, result=text)))

    def _workspace(self, environment: Json) -> str:
        """A named environment is reused; an inline or absent one gets a new workspace."""
        return environment if isinstance(environment, str) else self._id("workspace")

    def _download(self, request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz"):
            pass
        return httpx.Response(200, content=archive.getvalue())

    def _finish(self, id_: str, turn: Turn) -> None:
        interaction = self.interactions[id_]
        steps: list[Json] = []
        if turn.tool is not None:
            call = f"{id_}:tool"
            steps += [
                {
                    "id": call,
                    "type": "mcp_server_tool_call",
                    "name": turn.tool.name,
                    "server_name": turn.tool.server or "tools",
                    "arguments": turn.tool.input,
                },
                {
                    "id": call + ":result",
                    "type": "mcp_server_tool_result",
                    "call_id": call,
                    "name": turn.tool.name,
                    "server_name": turn.tool.server or "tools",
                    "result": turn.tool.result,
                },
            ]
        steps.append(
            {
                "id": f"{id_}:answer",
                "type": "model_output",
                "content": [{"type": "text", "text": turn.text}],
            }
        )
        self.reported.append((turn.input_tokens, turn.output_tokens))
        interaction.raw.update(
            status="completed",
            steps=steps,
            usage={
                "total_input_tokens": turn.input_tokens,
                "total_cached_tokens": 0,
                "total_output_tokens": turn.output_tokens,
                "total_thought_tokens": 0,
            },
        )
        interaction.log.append(
            {
                "event_type": "interaction.completed",
                "event_id": f"{id_}:end",
                "interaction": {"id": id_, "status": "completed"},
            }
        )
        interaction.done.set()

    def _snapshot(self, id_: str) -> dict[str, Json]:
        # Every read says whole seconds, so a turn that ends within the second it
        # started reads older than its create reply (#1614).
        raw = dict(self.interactions[id_].raw)
        for key in ("created", "updated"):
            raw[key] = raw[key].split(".")[0] + "Z"
        return raw

    def _read(self, request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        if match[1] not in self.interactions:
            return self.refuse(404, f"Interaction {match[1]} not found.")
        if request.url.params.get("stream") != "true":
            return httpx.Response(200, json=self._snapshot(match[1]))
        interaction = self.interactions[match[1]]

        async def follow() -> AsyncIterator[bytes]:
            await interaction.done.wait()
            for event in interaction.log:
                yield f"data: {json.dumps(event)}\n\n".encode()

        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=follow())

    def _cancel(self, request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        raw = self.interactions[match[1]].raw
        raw["status"] = "cancelled"
        return httpx.Response(200, json=raw)
