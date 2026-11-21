"""The tool client Actions and approved proposals call through: the step agent's attached
MCP server, with the agent's own credential for that exact URL, as the watch poller does."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import anthropic
from daimon.core.config import Settings
from daimon.core.github_credentials import build_multifernet, decrypt_token
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_server_url import ascii_mcp_url, same_ascii_mcp_url
from daimon.core.mcp_tool_call import INTERACTIVE_LIMITS, McpCallError, mcp_tool_session
from daimon.core.mux_backend import resource_scope
from daimon.core.mux_compat import retrieve_agent
from daimon.core.stores import agent_mcp_credentials as credential_store
from daimon.core.tool_safety import DAIMON_SERVER_NAME
from daimon.core.workflows.action_step import ToolFailed
from daimon.core.workflows.code_worker import Targets
from daimon.core.workflows.runs import Sessions, StepWork
from pydantic import JsonValue


def _enabled(agent: Any, server: str, tool: str) -> bool:  # noqa: ANN401 - MA agent
    toolsets = [t for t in agent.tools if t.type == "mcp_toolset" and t.mcp_server_name == server]
    if len(toolsets) != 1:
        return False
    configs = [c for c in toolsets[0].configs if c.name == tool]
    config = configs[0] if len(configs) == 1 else toolsets[0].default_config
    return config is None or bool(config.enabled)


@dataclass(frozen=True)
class McpToolClient:
    client: anthropic.AsyncAnthropic
    sm: Sessions
    settings: Settings
    targets: Targets

    async def _agent(self, work: StepWork) -> tuple[str, Any]:
        agent_id, _ = await self.targets(work)
        scope = resource_scope(tenant_id=str(work.run.tenant_id))
        return agent_id, await retrieve_agent(self.client, agent_id, scope=scope, records=self.sm)

    async def server_url(self, work: StepWork, server: str) -> str | None:
        if server == DAIMON_SERVER_NAME:
            return None  # Daimon's own tools are not called from here
        _, agent = await self._agent(work)
        urls = [str(s.url) for s in agent.mcp_servers if s.name == server]
        return urls[0] if len(urls) == 1 else None

    async def call(
        self, work: StepWork, url: str, tool: str, args: dict[str, JsonValue]
    ) -> JsonValue:
        agent_id, agent = await self._agent(work)
        names = [str(s.name) for s in agent.mcp_servers if same_ascii_mcp_url(str(s.url), url)]
        if len(names) != 1 or names[0] == DAIMON_SERVER_NAME or not _enabled(agent, names[0], tool):
            raise ToolFailed("The agent no longer allows this tool")
        token = await self._token(work, agent_id, url)
        try:
            async with mcp_tool_session(ascii_mcp_url(url), token, limits=INTERACTIVE_LIMITS) as s:
                return await s.call_tool(tool, args)
        except McpCallError as err:
            raise ToolFailed(f"The tool call failed ({err.kind})") from err

    async def _token(self, work: StepWork, agent_id: str, url: str) -> str | None:
        keys = tuple(k.get_secret_value() for k in self.settings.crypto.keys)
        tenant_id = work.run.tenant_id
        async with self.sm() as s:
            rows = await credential_store.list_credentials(
                s,
                tenant_id=tenant_id,
                agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=agent_id),
            )
        matches = [r for r in rows if same_ascii_mcp_url(r.mcp_server_url, url)]
        if not matches or not keys:
            return None
        if len(matches) > 1:
            raise ToolFailed("The agent has more than one credential for this server")
        return decrypt_token(build_multifernet(keys), matches[0].encrypted_token)
