"""Anthropic agent port, with native tool settings validated by closed schemas."""

import hashlib
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import cast

from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent
from anthropic.types.beta.agent_create_params import AgentCreateParams
from anthropic.types.beta.agent_update_params import AgentUpdateParams
from pydantic import JsonValue

from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import Page, PageRequest, ResourceRef, Revision, Scope
from mux.contracts.receipts import DeletionReceipt, Operation
from mux.contracts.resources import Agent, AgentFilter, AgentPatch, AgentSpec
from mux.drivers.anthropic.resources._errors import provider_call, provider_iter
from mux.drivers.anthropic.schemas import AgentModelConfig, agent_tools, multiagent
from mux.errors import ScopeViolation, UnsupportedCapability


def check_ref(ref: ResourceRef, account_scope_id: str, kind: str) -> None:
    if ref.provider != "anthropic" or ref.account_scope_id != account_scope_id or ref.kind != kind:
        raise ScopeViolation(
            ref.id, "reference does not belong to this provider workspace and kind"
        )


def agent_payload(spec: AgentSpec | AgentPatch) -> dict[str, object]:
    """Preserve fields-set; a patch null clears while an omitted value preserves."""
    fields = spec.model_fields_set
    payload: dict[str, object] = {}
    for name in ("name", "description", "system", "metadata"):
        value = getattr(spec, name)
        if name in fields and (value is not None or isinstance(spec, AgentPatch)):
            payload[name] = dict(value) if name == "metadata" and value is not None else value
    if spec.model is not None and "model" in fields:
        if spec.model.provider != "anthropic":
            raise ScopeViolation(spec.model.id, "model belongs to another provider")
        payload["model"] = spec.model.id
        if spec.model.options:
            payload["model"] = {"id": spec.model.id, **dict(spec.model.options)}
    if "mcp_servers" in fields:
        payload["mcp_servers"] = (
            [{"type": "url", "name": s.name, "url": s.url} for s in spec.mcp_servers]
            if spec.mcp_servers is not None
            else None
        )
    if "skills" in fields:
        payload["skills"] = (
            [
                {
                    "type": skill.source,
                    "skill_id": skill.id,
                    **({"version": skill.version} if "version" in skill.model_fields_set else {}),
                }
                for skill in spec.skills
            ]
            if spec.skills is not None
            else None
        )
    if "tools" in fields:
        payload["tools"] = (
            [tool_payload(tool) for tool in spec.tools] if spec.tools is not None else None
        )
    if isinstance(spec, AgentPatch):
        extensions = tuple((spec.extensions or {}).values())
    else:
        extensions = spec.extensions or ()
    for extension in extensions:
        if extension.namespace == "anthropic.agent_tools":
            payload["tools"] = agent_tools(extension).model_dump(mode="json", exclude_unset=True)[
                "tools"
            ]
        elif extension.namespace == "anthropic.model_config":
            if extension.version != 1:
                raise ValueError("expected anthropic.model_config@1")
            payload.update(
                AgentModelConfig.model_validate(dict(extension.value)).model_dump(
                    mode="json", exclude_unset=True
                )
            )
        elif extension.namespace == "anthropic.multiagent":
            payload["multiagent"] = multiagent(extension).model_dump(
                mode="json", exclude_unset=True
            )
        else:
            raise ValueError(f"unsupported agent extension {extension.namespace!r}")
    return payload


def tool_payload(tool: object) -> dict[str, object]:
    # Neutral tools are mapped separately from Anthropic-only toolset schemas.
    from mux.contracts.resources import ToolSpec

    if not isinstance(tool, ToolSpec):
        raise TypeError("expected ToolSpec")
    if tool.kind == "custom":
        return {
            "type": "custom",
            "name": tool.name,
            "description": tool.description or "",
            "input_schema": dict(tool.input_schema or {}),
        }
    if tool.kind == "mcp_toolset":
        return {"type": "mcp_toolset", "mcp_server_name": tool.name}
    return {"type": "agent_toolset_20260401", "configs": [{"name": tool.name}]}


def agent_record(item: BetaManagedAgentsAgent, account_scope_id: str) -> Agent:
    extensions: dict[str, ExtensionConfig] = {
        "anthropic.agent_tools": ExtensionConfig(
            namespace="anthropic.agent_tools",
            version=1,
            value={
                "tools": cast(
                    JsonValue, [t.model_dump(mode="json", exclude_unset=True) for t in item.tools]
                )
            },
        )
    }
    if item.multiagent is not None:
        extensions["anthropic.multiagent"] = ExtensionConfig(
            namespace="anthropic.multiagent",
            version=1,
            value=cast(
                dict[str, JsonValue], item.multiagent.model_dump(mode="json", exclude_unset=True)
            ),
        )
    model_options = item.model.model_dump(mode="json", exclude_unset=True, exclude={"id"})
    return Agent.model_validate(
        {
            "ref": {
                "id": item.id,
                "kind": "agent",
                "provider": "anthropic",
                "account_scope_id": account_scope_id,
            },
            "revision": {"local": item.version, "native": str(item.version)},
            "native": item.model_dump(mode="json", exclude_unset=True),
            "created_at": item.created_at,
            "updated_at": item.updated_at,
            "archived_at": item.archived_at,
            "lifecycle": "archived" if item.archived_at is not None else "active",
            "spec": {
                "name": item.name,
                "description": item.description,
                "model": {"provider": "anthropic", "id": item.model.id, "options": model_options},
                "system": item.system,
                "metadata": item.metadata,
                "extensions": tuple(extensions.values()),
                "mcp_servers": [{"name": s.name, "url": s.url} for s in item.mcp_servers],
                "skills": [
                    {"id": s.skill_id, "source": s.type, "version": s.version} for s in item.skills
                ],
            },
        }
    )


class AnthropicAgents:
    def __init__(
        self,
        client: AsyncAnthropic,
        account_scope_id: str,
    ) -> None:
        self._client = client
        self._account_scope_id = account_scope_id

    async def create(self, scope: Scope, spec: AgentSpec, *, key: str) -> Agent:
        item = await provider_call(
            self._client.beta.agents.create(**cast(AgentCreateParams, agent_payload(spec)))
        )
        return agent_record(item, self._account_scope_id)

    async def retrieve(self, scope: Scope, ref: ResourceRef) -> Agent:
        check_ref(ref, self._account_scope_id, "agent")
        return agent_record(
            await provider_call(self._client.beta.agents.retrieve(ref.id)), self._account_scope_id
        )

    async def list(self, scope: Scope, *, filters: AgentFilter, page: PageRequest) -> Page[Agent]:
        kwargs: dict[str, object] = {"include_archived": filters.include_archived}
        if page.cursor is not None:
            kwargs["page"] = page.cursor
        if page.limit is not None:
            kwargs["limit"] = page.limit
        if page.order is not None:
            raise UnsupportedCapability(("agent_list_order",), "anthropic.managed_agents")
        from anthropic.types.beta.agent_list_params import AgentListParams

        result = await provider_call(self._client.beta.agents.list(**cast(AgentListParams, kwargs)))
        return Page(
            data=tuple(
                agent_record(item, self._account_scope_id)
                for item in (result.data or ())
                if filters.name is None or item.name == filters.name
            ),
            next_cursor=result.next_page,
            has_more=bool(result.next_page),
        )

    async def update(
        self, scope: Scope, ref: ResourceRef, patch: AgentPatch, *, expected: Revision, key: str
    ) -> Agent:
        check_ref(ref, self._account_scope_id, "agent")
        kwargs = agent_payload(patch)
        kwargs["version"] = int(expected.native) if expected.native is not None else expected.local
        item = await provider_call(
            self._client.beta.agents.update(ref.id, **cast(AgentUpdateParams, kwargs))
        )
        return agent_record(item, self._account_scope_id)

    async def archive(self, scope: Scope, ref: ResourceRef, *, key: str) -> Operation:
        check_ref(ref, self._account_scope_id, "agent")
        await provider_call(self._client.beta.agents.archive(ref.id))
        now = datetime.now(UTC)
        return Operation(
            id=key,
            key=key,
            request_digest=hashlib.sha256(
                json.dumps({"archive_agent": ref.id}, sort_keys=True).encode()
            ).hexdigest(),
            status="processed",
            resource=ref,
            created_at=now,
            updated_at=now,
        )

    async def delete(self, scope: Scope, ref: ResourceRef, *, key: str) -> DeletionReceipt:
        check_ref(ref, self._account_scope_id, "agent")
        raise UnsupportedCapability(("agent_hard_delete",), "anthropic.managed_agents")

    async def walk(self, scope: Scope, *, filters: AgentFilter) -> AsyncIterator[Agent]:
        """Preserve the SDK's full async iterator for legacy host walks."""
        async for item in provider_iter(
            self._client.beta.agents.list(include_archived=filters.include_archived)
        ):
            if filters.name is None or item.name == filters.name:
                yield agent_record(item, self._account_scope_id)
