"""The `data_server` node: an identity server that keeps its own agents and their access.

Connecting links the server's agent for a Daimon agent (its external id is the id the
agent has at MCP servers; the exchanged token names the platform) and attaches the
server's MCP relay to the Daimon agent. Every other route passes one typed call through,
as the signed-in person, so the server's own permission checks decide.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Literal, cast

import httpx
import structlog
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.adapters.web.agent_access import (
    Editor,
    Viewer,
    actor_of,
    change_deps,
    edit_refusal,
    viewer_of,
)
from daimon.adapters.web.caller import WebCaller
from daimon.adapters.web.nodes.base import NodeConfigError
from daimon.adapters.web.nodes.exchange import (
    NODE_TIMEOUT_S,
    NodeAccessError,
    forget_node_token,
    node_http,
    node_token,
    server_base,
    serves,
)
from daimon.adapters.web.runtime import WebRuntime
from daimon.core import agent_changes, agent_editing
from daimon.core.agent_editing import AgentEditError
from daimon.core.agent_view import SPEND_WINDOW, AgentConfigDoc, McpServer, load_agent_view
from daimon.core.config import DataServerNodeSettings, Settings, SignedIdentityServer
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_identity import relay_url
from daimon.core.mcp_server_url import canonical_mcp_url
from daimon.core.token_exchange import ExchangeClient
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, JsonValue, SecretStr

log = structlog.get_logger(__name__)

_ACCESS: dict[str, tuple[int, str]] = {
    "no_account": (403, "Sign in with your chat account to use {label}."),
    "not_served": (403, "{label} doesn't serve this workspace."),
    "refused": (403, "{label} refused access. Ask your {label} admin."),
    "unconfigured": (503, "{label} isn't set up here."),
    "unreachable": (502, "{label} didn't answer. Try again soon."),
}
# Server answers passed on as they are, with the server's own reason.
_PASSED_ON = frozenset({400, 409, 422})
_MAX_DETAIL = 200


class DataServerStatus(BaseModel):
    attached: bool
    reachable: bool
    problem: str | None = None
    # The server's agent with its tokens and activity when the caller manages it.
    agent: JsonValue = None
    # Whether the server counts the caller as an admin, who may change an agent's role.
    admin: bool = False
    # The agent on the server's own pages.
    open_url: str | None = None


class DataServerAgentChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=200)
    scopes: list[str] | None = None
    acts_for: Literal["requester", "self"] | None = None
    status: Literal["active", "suspended"] | None = None
    redact_clients: bool | None = None
    can_write: bool | None = None
    personal: bool | None = None
    # Admin-only on the server, which refuses anyone else.
    role: Literal["admin", "member"] | None = None


class DataServerToolChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection_id: str
    tool_name: str | None = None
    allow: bool | None


@dataclass(frozen=True)
class DataServerNode:
    label: str
    scope: str
    client_id: str
    client_secret: SecretStr
    server: SignedIdentityServer
    # Where agents reach the server's MCP endpoint; None without a public MCP URL.
    relay: str | None

    @classmethod
    def from_settings(cls, config: DataServerNodeSettings, settings: Settings) -> DataServerNode:
        """The node, or a `NodeConfigError` naming what the web service lacks."""
        env = "DAIMON_WEB__NODES__DATA_SERVER"
        if (
            config.identity_server is None
            or config.client_id is None
            or config.client_secret is None
        ):
            missing = [
                f"{env}__{name.upper()}"
                for name in ("identity_server", "client_id", "client_secret")
                if getattr(config, name) is None
            ]
            raise NodeConfigError(f"{env} needs {', '.join(missing)}")
        server = next(
            (s for s in settings.mcp_identity.servers if s.name == config.identity_server), None
        )
        if server is None:
            raise NodeConfigError(
                f"{env}__IDENTITY_SERVER names {config.identity_server!r}, "
                "which DAIMON_MCP_IDENTITY__SERVERS lacks"
            )
        if settings.mcp_identity.signing_key is None:
            raise NodeConfigError(f"{env} needs DAIMON_MCP_IDENTITY__SIGNING_KEY")
        public = settings.mcp.public_url
        relay = None if public is None else relay_url(str(public), server.name)
        return cls(
            label=config.label,
            scope=config.scope,
            client_id=config.client_id,
            client_secret=config.client_secret,
            server=server,
            relay=relay,
        )

    @property
    def kind(self) -> str:
        return "data_server"

    def claims(self, server_url: str) -> bool:
        ours = {canonical_mcp_url(self.server.url)}
        if self.relay is not None:
            ours.add(canonical_mcp_url(self.relay))
        return canonical_mcp_url(server_url) in ours

    def offered(self, workspace: str | None) -> bool:
        return self.relay is not None and serves(self.server, workspace)

    def _client(self) -> ExchangeClient:
        return ExchangeClient(
            token_url=f"{server_base(self.server)}/oauth/token",
            client_id=self.client_id,
            client_secret=self.client_secret.get_secret_value(),
            scope=self.scope,
        )

    def _access_refusal(self, err: NodeAccessError) -> HTTPException:
        status, detail = _ACCESS[err.problem]
        return HTTPException(status_code=status, detail=detail.format(label=self.label))

    async def _call(
        self,
        runtime: WebRuntime,
        caller: WebCaller,
        method: Literal["GET", "POST", "PATCH", "PUT", "DELETE"],
        path: str,
        *,
        body: JsonValue = None,
        params: dict[str, str] | None = None,
    ) -> httpx.Response:
        """One API call as the caller; the server's refusals mapped to the web app's."""
        async with node_http(runtime, self.server) as http:
            try:
                token = await node_token(
                    runtime, caller, http, kind=self.kind, server=self.server, client=self._client()
                )
            except NodeAccessError as err:
                raise self._access_refusal(err) from err
            try:
                response = await http.request(
                    method,
                    f"{server_base(self.server)}/api{path}",
                    headers={"Authorization": f"Bearer {token}"},
                    json=body,
                    params=params,
                    timeout=NODE_TIMEOUT_S,
                )
            except httpx.HTTPError as err:
                log.warning("web.node.request_failed", kind=self.kind, err_type=type(err).__name__)
                raise HTTPException(
                    status_code=502, detail=f"{self.label} didn't answer. Try again soon."
                ) from err
        status = response.status_code
        if status == 401:
            # Never a web 401, which signs the person out of the web app.
            await forget_node_token(runtime, caller, self.server)
            raise HTTPException(status_code=403, detail=f"{self.label} didn't accept your sign-in.")
        if status == 403:
            raise HTTPException(status_code=403, detail=f"{self.label} doesn't allow this.")
        if status == 404:
            raise HTTPException(status_code=404, detail=f"Not found on {self.label}.")
        if status in _PASSED_ON:
            raise HTTPException(status_code=status, detail=_detail(response, self.label))
        if response.is_error:
            log.warning("web.node.request_refused", kind=self.kind, status=status)
            raise HTTPException(
                status_code=502, detail=f"{self.label} had a problem. Try again soon."
            )
        return response

    async def _json(
        self,
        runtime: WebRuntime,
        caller: WebCaller,
        method: Literal["GET", "POST", "PATCH", "PUT", "DELETE"],
        path: str,
        *,
        body: JsonValue = None,
        params: dict[str, str] | None = None,
    ) -> JsonValue:
        response = await self._call(runtime, caller, method, path, body=body, params=params)
        if not response.content:
            return None
        try:
            return cast("JsonValue", response.json())
        except ValueError as err:
            raise HTTPException(
                status_code=502, detail=f"{self.label} had a problem. Try again soon."
            ) from err

    def _external_id(self, caller: WebCaller, agent_id: str) -> str:
        """The id the agent has at MCP servers, which the server knows it by."""
        return str(derive_agent_uuid(tenant_id=caller.person.tenant_id, ma_agent_id=agent_id))

    async def _server_agent_id(
        self, runtime: WebRuntime, caller: WebCaller, agent_id: str
    ) -> str | None:
        """The server's id for the agent; None when it has none."""
        try:
            found = await self._json(
                runtime,
                caller,
                "GET",
                f"/agents/external/{self._external_id(caller, agent_id)}",
            )
        except HTTPException as exc:
            if exc.status_code == 404:
                return None
            raise
        server_id = found.get("id") if isinstance(found, dict) else None
        if not isinstance(server_id, str):
            raise HTTPException(
                status_code=502, detail=f"{self.label} had a problem. Try again soon."
            )
        return server_id

    async def _require_server_agent(
        self, runtime: WebRuntime, caller: WebCaller, agent_id: str
    ) -> str:
        server_id = await self._server_agent_id(runtime, caller, agent_id)
        if server_id is None:
            raise HTTPException(
                status_code=404, detail=f"Connect this agent to {self.label} first."
            )
        return server_id

    async def _editable(
        self, runtime: WebRuntime, caller: WebCaller, agent_id: str
    ) -> BetaManagedAgentsAgent:
        """The Daimon agent, refused unless the caller may change it."""
        try:
            return await agent_editing.load_editable_agent(
                change_deps(runtime).edit, actor=actor_of(caller), ma_agent_id=agent_id
            )
        except AgentEditError as exc:
            raise edit_refusal(exc) from exc

    async def _attached(self, runtime: WebRuntime, caller: WebCaller, agent_id: str) -> bool:
        """Whether the relay is attached, refused to viewers who can't see the agent's servers."""
        view = await load_agent_view(
            change_deps(runtime).view,
            tenant_id=caller.person.tenant_id,
            ma_agent_id=agent_id,
            viewer=viewer_of(caller),
            spend_since=runtime.now() - SPEND_WINDOW,
        )
        if view is None:
            raise HTTPException(status_code=404, detail="No such agent.")
        if not view.can_view_details:
            # The view hides attached servers from this viewer; so does this route.
            raise HTTPException(status_code=403, detail="You can't see this agent's connections.")
        return any(self.claims(s.url) for s in view.mcp_servers)

    async def status(
        self, runtime: WebRuntime, caller: WebCaller, agent_id: str
    ) -> DataServerStatus:
        attached = await self._attached(runtime, caller, agent_id)
        try:
            server_id = await self._server_agent_id(runtime, caller, agent_id)
        except HTTPException as exc:
            if exc.status_code not in (403, 502, 503):
                raise
            return DataServerStatus(attached=attached, reachable=False, problem=str(exc.detail))
        if server_id is None:
            return DataServerStatus(attached=attached, reachable=True)
        found, me = await asyncio.gather(
            self._json(runtime, caller, "GET", f"/agents/{server_id}"),
            self._json(runtime, caller, "GET", "/me"),
        )
        admin = isinstance(me, dict) and me.get("is_admin") is True
        return DataServerStatus(
            attached=attached,
            reachable=True,
            agent=found,
            admin=admin,
            open_url=f"{server_base(self.server)}/agents?agent={server_id}",
        )

    async def _can_manage(self, runtime: WebRuntime, caller: WebCaller, server_id: str) -> bool:
        found = await self._json(runtime, caller, "GET", f"/agents/{server_id}")
        return isinstance(found, dict) and found.get("can_manage") is True

    async def _managed(self, runtime: WebRuntime, caller: WebCaller, server_id: str) -> bool:
        """Whether the server's agent has a manager the caller acts as or may override.

        `can_manage` alone is also true for server admins on an agent nobody manages.
        """
        found = await self._json(runtime, caller, "GET", f"/agents/{server_id}")
        return (
            isinstance(found, dict)
            and found.get("can_manage") is True
            and found.get("managed_by") is not None
        )

    async def connect(
        self, runtime: WebRuntime, caller: WebCaller, agent_id: str
    ) -> DataServerStatus:
        """Link the server's agent to the caller, then attach the relay.

        Linking creates the agent, or takes over one the platform registered with no
        manager; the server answers 409 when another person manages it.
        """
        relay = self.relay
        if relay is None:
            raise HTTPException(status_code=503, detail=f"{self.label} isn't set up here.")
        deps, actor = change_deps(runtime), actor_of(caller)
        agent = await self._editable(runtime, caller, agent_id)
        await self._attached(runtime, caller, agent_id)
        server_id = await self._server_agent_id(runtime, caller, agent_id)
        if server_id is None or not await self._managed(runtime, caller, server_id):
            try:
                await self._json(
                    runtime,
                    caller,
                    "POST",
                    "/agents",
                    body={"name": agent.name, "external_id": self._external_id(caller, agent_id)},
                )
            except HTTPException as exc:
                # Someone else manages it: fine for an admin, who may manage it anyway.
                if exc.status_code != 409 or not (
                    server_id is not None and await self._can_manage(runtime, caller, server_id)
                ):
                    raise
            log.info("web.node_agent.linked", kind=self.kind)
        if not any(self.claims(s.url) for s in agent.mcp_servers):
            try:
                await agent_changes.apply_change(
                    deps,
                    actor=actor,
                    ma_agent_id=agent_id,
                    change=lambda config: self._with_relay(config, relay),
                    note=f"Connected {self.label}",
                )
            except AgentEditError as exc:
                # Nothing to apply: a concurrent connect attached it first.
                if not await self._attached(runtime, caller, agent_id):
                    raise edit_refusal(exc) from exc
            runtime.agents.forget(caller.person.tenant_id)
            log.info("web.node.attached", kind=self.kind)
        return await self.status(runtime, caller, agent_id)

    async def disconnect(
        self, runtime: WebRuntime, caller: WebCaller, agent_id: str
    ) -> DataServerStatus:
        """Detach the server from the agent; the server's agent and its access stay."""
        deps, actor = change_deps(runtime), actor_of(caller)
        agent = await self._editable(runtime, caller, agent_id)
        await self._attached(runtime, caller, agent_id)
        if any(self.claims(s.url) for s in agent.mcp_servers):
            try:
                await agent_changes.apply_change(
                    deps,
                    actor=actor,
                    ma_agent_id=agent_id,
                    change=self._without_servers,
                    note=f"Disconnected {self.label}",
                )
            except AgentEditError as exc:
                # Nothing to apply: a concurrent disconnect detached it first.
                if await self._attached(runtime, caller, agent_id):
                    raise edit_refusal(exc) from exc
            runtime.agents.forget(caller.person.tenant_id)
            log.info("web.node.detached", kind=self.kind)
        return await self.status(runtime, caller, agent_id)

    def _with_relay(self, config: AgentConfigDoc, relay: str) -> AgentConfigDoc:
        """`config` with the relay attached; unchanged when a concurrent connect did it. Pure."""
        if any(self.claims(s.url) for s in config.mcp_servers):
            return config
        return _with_server(config, self.server.name, relay)

    def _without_servers(self, config: AgentConfigDoc) -> AgentConfigDoc:
        kept = [s for s in config.mcp_servers if not self.claims(s.url)]
        return config.model_copy(update={"mcp_servers": kept})

    def router(self, runtime: WebRuntime) -> APIRouter:
        router = APIRouter()

        @router.post("/connect")
        async def connect(caller: Editor, agent_id: str) -> DataServerStatus:  # pyright: ignore[reportUnusedFunction]
            return await self.connect(runtime, caller, agent_id)

        @router.post("/disconnect")
        async def disconnect(caller: Editor, agent_id: str) -> DataServerStatus:  # pyright: ignore[reportUnusedFunction]
            return await self.disconnect(runtime, caller, agent_id)

        @router.get("/agent")
        async def get_agent(caller: Viewer, agent_id: str) -> JsonValue:  # pyright: ignore[reportUnusedFunction]
            server_id = await self._require_server_agent(runtime, caller, agent_id)
            return await self._json(runtime, caller, "GET", f"/agents/{server_id}")

        @router.patch("/agent")
        async def change_agent(  # pyright: ignore[reportUnusedFunction]
            caller: Editor, agent_id: str, body: DataServerAgentChange
        ) -> JsonValue:
            await self._editable(runtime, caller, agent_id)
            server_id = await self._require_server_agent(runtime, caller, agent_id)
            change = cast("JsonValue", body.model_dump(exclude_unset=True))
            return await self._json(runtime, caller, "PATCH", f"/agents/{server_id}", body=change)

        @router.get("/tools")
        async def tools(caller: Viewer, agent_id: str) -> JsonValue:  # pyright: ignore[reportUnusedFunction]
            server_id = await self._require_server_agent(runtime, caller, agent_id)
            return await self._json(runtime, caller, "GET", f"/agents/{server_id}/tools")

        @router.put("/tools")
        async def set_tool(  # pyright: ignore[reportUnusedFunction]
            caller: Editor, agent_id: str, body: DataServerToolChange
        ) -> JsonValue:
            await self._editable(runtime, caller, agent_id)
            server_id = await self._require_server_agent(runtime, caller, agent_id)
            change = cast("JsonValue", body.model_dump())
            return await self._json(
                runtime, caller, "PUT", f"/agents/{server_id}/tools", body=change
            )

        @router.delete("/tokens/{token_id}")
        async def revoke_token(  # pyright: ignore[reportUnusedFunction]
            caller: Editor, agent_id: str, token_id: uuid.UUID
        ) -> None:
            await self._editable(runtime, caller, agent_id)
            server_id = await self._require_server_agent(runtime, caller, agent_id)
            found = await self._json(runtime, caller, "GET", f"/agents/{server_id}")
            if str(token_id) not in _token_ids(found):
                raise HTTPException(status_code=404, detail=f"Not found on {self.label}.")
            await self._call(runtime, caller, "DELETE", f"/tokens/{token_id}")

        @router.get("/scopes")
        async def scopes(caller: Viewer, agent_id: str) -> JsonValue:  # pyright: ignore[reportUnusedFunction]
            # The caller's own scopes: the only ones they may give.
            return await self._json(runtime, caller, "GET", "/scopes/visible")

        return router


def _with_server(config: AgentConfigDoc, name: str, url: str) -> AgentConfigDoc:
    """`config` with an MCP server at `url`, keyed `name` or the first free `name-<n>`. Pure."""
    taken = {s.key for s in config.mcp_servers}
    key = next(k for k in (name, *(f"{name}-{n}" for n in range(2, 100))) if k not in taken)
    server = McpServer(key=key, name=key, url=url, auth="none", credential=None, tools=[])
    return config.model_copy(update={"mcp_servers": [*config.mcp_servers, server]})


def _detail(response: httpx.Response, label: str) -> str:
    """The server's own short reason, or a plain one."""
    try:
        payload: object = response.json()
    except ValueError:
        payload = None
    detail = cast("dict[str, object]", payload).get("detail") if isinstance(payload, dict) else None
    if isinstance(detail, str) and detail.strip():
        return detail.strip()[:_MAX_DETAIL]
    return f"{label} couldn't do this."


def _token_ids(agent: JsonValue) -> set[str]:
    """Ids of the tokens a server agent lists. Pure."""
    tokens = agent.get("tokens") if isinstance(agent, dict) else None
    if not isinstance(tokens, list):
        return set()
    return {str(t.get("id")) for t in tokens if isinstance(t, dict)}
