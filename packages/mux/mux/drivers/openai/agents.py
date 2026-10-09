"""Saved agent resources; unsupported conditional updates fail before I/O."""

from __future__ import annotations

from pydantic import JsonValue

from mux.contracts.ids import ModelRef, Page, PageRequest, ResourceRef, Revision, Scope
from mux.contracts.receipts import DeletionReceipt, Operation
from mux.contracts.resources import (
    Agent,
    AgentFilter,
    AgentPatch,
    AgentSpec,
    MCPConnection,
    ToolSpec,
)
from mux.drivers.openai._common import (
    Context,
    objects,
    owned,
    page_of,
    query,
    revision,
    text,
    timestamp,
)
from mux.drivers.openai.skill_bindings import KEY, decode, encode, verify_pins
from mux.drivers.openai.transport import Object, object_json, segment
from mux.errors import ScopeViolation


def agent_body(spec: AgentSpec, context: Context) -> Object:
    if spec.model.provider != "openai":
        raise context.unsupported("model_provider")
    if spec.description is not None or spec.model.options or spec.extensions:
        raise context.unsupported("agent_configuration")
    body: Object = {"name": spec.name, "model": spec.model.id}
    if spec.system is not None:
        body["instructions"] = spec.system
    if spec.metadata is not None:
        body["metadata"] = dict(spec.metadata)
    if spec.metadata is not None and KEY in spec.metadata:
        raise context.unsupported("reserved_skill_metadata")
    if spec.skills is not None:
        body["metadata"] = {
            **object_json(body.get("metadata") or {}),
            KEY: encode(spec.skills, context),
        }
    tools: list[JsonValue] = []
    for tool in spec.tools or ():
        if tool.permission != "auto":
            raise context.unsupported("tool_confirmation")
        if tool.kind == "custom":
            tools.append(
                {
                    "type": "function",
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": dict(tool.input_schema or {}),
                }
            )
        elif tool.kind == "builtin" and tool.name in (
            "web_search",
            "tool_search",
            "programmatic_tool_calling",
        ):
            tools.append({"type": tool.name})
        else:
            raise context.unsupported("agent_tool")
    for mcp in spec.mcp_servers or ():
        if mcp.credential_ref is not None or mcp.tool_policy or mcp.transport != "streamable_http":
            raise context.unsupported("mcp_credentials")
        tools.append(
            {
                "type": "mcp",
                "server_label": mcp.name,
                "transport": {"type": "http", "server_url": mcp.url},
            }
        )
    if spec.tools is not None or spec.mcp_servers is not None:
        body["tools"] = tools
    return body


def agent_spec(raw: Object) -> AgentSpec:
    tools: list[ToolSpec] = []
    servers: list[MCPConnection] = []
    for tool in objects(raw.get("tools", [])):
        kind = text(tool["type"])
        if kind == "mcp":
            transport = object_json(tool["transport"])
            servers.append(
                MCPConnection(name=text(tool["server_label"]), url=text(transport["server_url"]))
            )
        elif kind == "function":
            tools.append(
                ToolSpec(
                    name=text(tool["name"]),
                    kind="custom",
                    description=text(tool["description"]) if tool.get("description") else None,
                    input_schema=object_json(tool["parameters"]),
                )
            )
        else:
            tools.append(ToolSpec(name=kind, kind="builtin"))
    return AgentSpec.model_validate(
        {
            "name": raw.get("name") or "",
            "model": ModelRef(provider="openai", id=text(raw["model"])),
            "system": raw.get("instructions"),
            "tools": tuple(tools) if "tools" in raw else None,
            "mcp_servers": tuple(servers) if "tools" in raw else None,
            "metadata": raw.get("metadata"),
            "skills": decode(raw.get("metadata")),
        }
    )


class OpenAIAgents:
    def __init__(self, context: Context) -> None:
        self._c = context

    def _decode(self, scope: Scope, raw: Object) -> Agent:
        self._c.record(scope, raw)
        return Agent(
            ref=self._c.ref(scope, "agent", text(raw["id"])),
            revision=revision(raw),
            spec=agent_spec(raw),
            created_at=timestamp(raw["created_at"]),
        )

    @owned
    async def create(self, scope: Scope, spec: AgentSpec, *, key: str) -> Agent:
        self._c.authorize(scope, "agent")
        body = agent_body(spec, self._c)
        if spec.skills is not None:
            await verify_pins(spec.skills, scope, self._c)
        body["metadata"] = {
            **object_json(body.get("metadata") or {}),
            "mux_tenant": scope.tenant_id,
        }
        return self._decode(scope, await self._c.call("POST", "/agents", body=body))

    @owned
    async def retrieve(self, scope: Scope, ref: ResourceRef) -> Agent:
        self._c.check(scope, ref, "agent")
        raw = await self._c.call("GET", "/agents/" + segment(ref.id))
        if raw.get("id") != ref.id:
            raise ValueError("wrong agent identity")
        return self._decode(scope, raw)

    @owned
    async def list(self, scope: Scope, *, filters: AgentFilter, page: PageRequest) -> Page[Agent]:
        self._c.authorize(scope, "agent")
        if (
            filters.name is not None
            or filters.created_after is not None
            or filters.include_archived
        ):
            raise self._c.unsupported("agent_filters")
        raw = await self._c.call("GET", "/agents", params=query(page))
        parsed = page_of(raw, lambda item: self._decode(scope, item))
        return parsed.model_copy(
            update={
                "data": tuple(
                    a for a in parsed.data if scope.is_platform or self._visible(scope, a.ref.id)
                )
            }
        )

    def _visible(self, scope: Scope, id_: str) -> bool:
        try:
            self._c.authorize(scope, "agent", id_)
        except ScopeViolation:
            return False
        return True

    @owned
    async def update(
        self, scope: Scope, ref: ResourceRef, patch: AgentPatch, *, expected: Revision, key: str
    ) -> Agent:
        self._c.check(scope, ref, "agent")
        raise self._c.unsupported("agent_conditional_update")

    @owned
    async def archive(self, scope: Scope, ref: ResourceRef, *, key: str) -> Operation:
        self._c.check(scope, ref, "agent")
        raise self._c.unsupported("agent_archive")

    @owned
    async def delete(self, scope: Scope, ref: ResourceRef, *, key: str) -> DeletionReceipt:
        self._c.check(scope, ref, "agent")
        await self._c.call("DELETE", "/agents/" + segment(ref.id))
        return DeletionReceipt(operation_id=key, deleted=(ref,))
