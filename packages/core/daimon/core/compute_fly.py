"""Fly Machines control plane. Errors deliberately omit remote bodies and URLs."""

from __future__ import annotations

import re
import uuid

import httpx
from daimon.core.compute_models import ComputeResource, ComputeSession
from daimon.core.compute_provider import (
    OTHER_PROVIDER,
    ComputeProviderError,
    app_name,
    app_prefix,
    region_provider,
)
from daimon.core.compute_thin_mcp import call_thin_mcp
from daimon.core.config import ComputeSettings
from pydantic import BaseModel, Field, JsonValue, TypeAdapter


class _Config(BaseModel):
    metadata: dict[str, str] = Field(default_factory=dict)


class _Machine(BaseModel):
    id: str
    state: str = "unknown"
    config: _Config


class _IP(BaseModel):
    ip: str
    shared: bool = False


class _IPs(BaseModel):
    ips: list[_IP] = Field(default_factory=lambda: list[_IP]())


class _App(BaseModel):
    name: str


class _Apps(BaseModel):
    apps: list[_App] = Field(default_factory=lambda: list[_App]())


class FlyComputeProvider:
    def __init__(
        self, settings: ComputeSettings, *, transport: httpx.AsyncBaseTransport | None = None
    ):
        self.settings = settings
        self.transport = transport

    async def _request(
        self, method: str, path: str, body: dict[str, JsonValue] | None = None
    ) -> httpx.Response:
        token = self.settings.api_token
        if (
            token is None
            or not token.get_secret_value()
            or not self.settings.org_slug
            or not self.settings.deployment_id
        ):
            raise ComputeProviderError("Fly sessions are not configured. Ask an admin.")
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=50) as client:
                response = await client.request(
                    method,
                    "https://api.machines.dev/v1" + path,
                    headers={"Authorization": "Bearer " + token.get_secret_value()},
                    json=body,
                )
            if response.status_code != 404:
                response.raise_for_status()
            return response
        except httpx.HTTPError:
            raise ComputeProviderError(
                "Fly is unavailable. Cleanup will retry; try again later."
            ) from None

    def _check(self, resource: ComputeResource) -> None:
        if resource.deployment_id != self.settings.deployment_id or resource.app_name != app_name(
            self.settings.deployment_id, resource.session_id
        ):
            raise ComputeProviderError("Fly resource ownership does not match this deployment.")
        if resource.region is not None and region_provider(resource.region) != "fly":
            # A missing Fly app would read as deleted while the other provider's
            # resource kept running and billing.
            raise ComputeProviderError(OTHER_PROVIDER)

    async def _machines(self, name: str) -> list[_Machine]:
        response = await self._request("GET", f"/apps/{name}/machines")
        if response.status_code == 404:
            return []
        try:
            return TypeAdapter(list[_Machine]).validate_json(response.content)
        except ValueError:
            raise ComputeProviderError("Fly returned an invalid machine record.") from None

    def _owned(self, machines: list[_Machine], resource: ComputeResource) -> None:
        for machine in machines:
            if machine.config.metadata.get(
                "daimon_deployment"
            ) != resource.deployment_id or machine.config.metadata.get("daimon_session") != str(
                resource.session_id
            ):
                raise ComputeProviderError("Fly resource ownership does not match this deployment.")

    async def ensure(self, session: ComputeSession, token: str) -> ComputeResource:
        resource = ComputeResource(
            app_name=session.app_name,
            deployment_id=session.deployment_id,
            session_id=session.id,
            region=session.region,
        )
        self._check(resource)
        response = await self._request("GET", f"/apps/{session.app_name}")
        if response.status_code == 404:
            await self._request(
                "POST",
                "/apps",
                {
                    "app_name": session.app_name,
                    "org_slug": self.settings.org_slug,
                    "network": session.app_name,
                },
            )
        machines = await self._machines(session.app_name)
        self._owned(machines, resource)
        if len(machines) > 1:
            raise ComputeProviderError(
                "Unexpected extra machines. Stop this session and try again."
            )
        if not machines:
            # Discover allocations before retrying a partial create. Repeating
            # dedicated IPv6 allocation could leak addresses and provider cost.
            response = await self._request("GET", f"/apps/{session.app_name}/ip_assignments")
            try:
                ips = _IPs.model_validate_json(response.content).ips
            except ValueError:
                raise ComputeProviderError("Fly returned an invalid IP list.") from None
            if not any(ip.shared for ip in ips):
                await self._request(
                    "POST", f"/apps/{session.app_name}/ip_assignments", {"type": "shared_v4"}
                )
            if not any(":" in ip.ip for ip in ips):
                await self._request(
                    "POST", f"/apps/{session.app_name}/ip_assignments", {"type": "v6"}
                )
            response = await self._request(
                "POST",
                f"/apps/{session.app_name}/machines",
                {
                    "name": "session",
                    "region": session.region,
                    "config": {
                        "image": session.image,
                        "env": {"MCP_AUTH_TOKEN": token},
                        "metadata": {
                            "daimon_deployment": session.deployment_id,
                            "daimon_session": str(session.id),
                        },
                        "guest": {
                            "cpu_kind": session.cpu_kind,
                            "cpus": session.cpus,
                            "memory_mb": session.memory_mb,
                        },
                        "restart": {"policy": "always"},
                        "services": [
                            {
                                "protocol": "tcp",
                                "internal_port": 8080,
                                "ports": [{"port": 443, "handlers": ["tls", "http"]}],
                            }
                        ],
                    },
                },
            )
            try:
                machine = _Machine.model_validate_json(response.content)
            except ValueError:
                raise ComputeProviderError("Fly returned an invalid machine record.") from None
            machines = [machine]
        self._owned(machines, resource)
        return resource.model_copy(
            update={"machine_id": machines[0].id, "machine_state": machines[0].state}
        )

    async def inspect(self, session: ComputeSession) -> ComputeResource:
        resource = ComputeResource(
            app_name=session.app_name,
            deployment_id=session.deployment_id,
            session_id=session.id,
            region=session.region,
        )
        self._check(resource)
        machines = await self._machines(resource.app_name)
        self._owned(machines, resource)
        return resource.model_copy(
            update={
                "machine_id": machines[0].id if machines else None,
                "machine_state": machines[0].state if machines else "no_machines",
            }
        )

    async def delete(self, resource: ComputeResource) -> None:
        self._check(resource)
        self._owned(await self._machines(resource.app_name), resource)
        await self._request("DELETE", f"/apps/{resource.app_name}?force=true")
        response = await self._request("GET", f"/apps/{resource.app_name}")
        if response.status_code != 404:
            raise ComputeProviderError("Fly deletion is pending. Cleanup will retry.")

    async def inventory(self) -> list[ComputeResource]:
        response = await self._request("GET", "/apps?org_slug=" + self.settings.org_slug)
        try:
            apps = _Apps.model_validate_json(response.content).apps
        except ValueError:
            raise ComputeProviderError("Fly returned an invalid app list.") from None
        prefix = app_prefix(self.settings.deployment_id)
        resources: list[ComputeResource] = []
        for app in apps:
            if not re.fullmatch(re.escape(prefix) + r"[0-9a-f]{32}", app.name):
                continue
            resource = ComputeResource(
                app_name=app.name,
                deployment_id=self.settings.deployment_id,
                session_id=uuid.UUID(app.name[len(prefix) :]),
            )
            machines = await self._machines(app.name)
            try:
                self._owned(machines, resource)
            except ComputeProviderError:
                continue  # Never destroy a machine with contradictory ownership tags.
            resources.append(
                resource.model_copy(update={"machine_id": machines[0].id if machines else None})
            )
        return resources

    async def call(
        self, session: ComputeSession, token: str, tool: str, arguments: dict[str, JsonValue]
    ) -> JsonValue:
        self._check(
            ComputeResource(
                app_name=session.app_name,
                deployment_id=session.deployment_id,
                session_id=session.id,
                region=session.region,
            )
        )
        return await call_thin_mcp(
            f"https://{session.app_name}.fly.dev/mcp/",
            token,
            tool,
            arguments,
            transport=self.transport,
        )
