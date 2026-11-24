"""Google Cloud Run control plane: one private service per session.

The service runs the same thin MCP image as a Fly machine, with one instance that
keeps its CPU between requests. Errors deliberately omit remote bodies and URLs.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import re
import uuid
from typing import Protocol, cast

import httpx
import structlog
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
from google import auth as google_auth
from google.auth import exceptions as google_exceptions
from google.auth.transport.requests import Request
from google.oauth2 import id_token as google_id_token
from pydantic import BaseModel, ConfigDict, Field, JsonValue

log = structlog.get_logger(__name__)

_API = "https://run.googleapis.com/v2/"
_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
_DEPLOYMENT_LABEL = "daimon_deployment"
_SESSION_LABEL = "daimon_session"
# How long one delete call waits on Google's side before it reports cleanup pending.
_DELETE_WAIT = "20s"


def deployment_label(deployment_id: str) -> str:
    """A label value for any deployment id: labels allow only lowercase, digits, _ and -."""
    return hashlib.sha256(deployment_id.encode()).hexdigest()[:32]


class GoogleTokens(Protocol):
    """The two Google tokens a control plane needs, minted without blocking the loop."""

    async def access_token(self) -> str: ...
    async def id_token(self, audience: str) -> str: ...


class _Credentials(Protocol):
    """The part of a google-auth credential used here; google-auth ships no full stubs."""

    token: str | None

    @property
    def valid(self) -> bool: ...
    def refresh(self, request: Request) -> None: ...


class ApplicationDefaultTokens:
    """Tokens from application default credentials: the metadata server on Google
    Cloud, or the service account file `GOOGLE_APPLICATION_CREDENTIALS` names.

    google-auth is synchronous, so each mint runs in a thread. Credentials are kept
    and refreshed only when google-auth reports them expired.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._access: _Credentials | None = None
        self._identity: dict[str, _Credentials] = {}

    def _access_token(self) -> str:
        if self._access is None:
            found = google_auth.default(scopes=[_SCOPE])  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]  # google-auth has no stubs
            self._access = cast(_Credentials, found[0])
        return self._fresh(self._access)

    def _id_token(self, audience: str) -> str:
        credentials = self._identity.get(audience)
        if credentials is None:
            if len(self._identity) >= 256:
                # One entry per session URL; ended sessions are never asked for again.
                self._identity.clear()
            credentials = cast(
                _Credentials,
                google_id_token.fetch_id_token_credentials(audience, request=Request()),  # pyright: ignore[reportUnknownMemberType]  # google-auth has no stubs
            )
            self._identity[audience] = credentials
        return self._fresh(credentials)

    @staticmethod
    def _fresh(credentials: _Credentials) -> str:
        if not credentials.valid:
            credentials.refresh(Request())
        if not credentials.token:
            raise google_exceptions.RefreshError
        return credentials.token

    async def _mint(self, audience: str | None) -> str:
        try:
            async with self._lock:
                if audience is None:
                    return await asyncio.to_thread(self._access_token)
                return await asyncio.to_thread(self._id_token, audience)
        except google_exceptions.GoogleAuthError:
            # Never chain: google-auth errors can quote credential files and responses.
            raise ComputeProviderError(
                "Google credentials for Cloud Run are not available. Ask an admin."
            ) from None

    async def access_token(self) -> str:
        return await self._mint(None)

    async def id_token(self, audience: str) -> str:
        return await self._mint(audience)


class _Condition(BaseModel):
    state: str = "STATE_UNSPECIFIED"


class _Service(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str
    uid: str = ""
    labels: dict[str, str] = Field(default_factory=dict)
    uri: str = ""
    delete_time: str | None = Field(default=None, alias="deleteTime")
    reconciling: bool = False
    terminal_condition: _Condition = Field(default_factory=_Condition, alias="terminalCondition")

    @property
    def machine_state(self) -> str:
        """Cloud Run's readiness in the words Fly uses for a machine."""
        if self.delete_time:
            return "destroying"
        if self.terminal_condition.state == "CONDITION_FAILED":
            return "failed"
        if self.terminal_condition.state == "CONDITION_SUCCEEDED" and not self.reconciling:
            return "started"
        return "starting"


class _Services(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    services: list[_Service] = Field(default_factory=lambda: list[_Service]())
    next_page_token: str = Field(default="", alias="nextPageToken")


class _Operation(BaseModel):
    name: str = ""
    done: bool = False


class CloudRunComputeProvider:
    def __init__(
        self,
        settings: ComputeSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        tokens: GoogleTokens | None = None,
    ):
        self.settings = settings
        self.transport = transport
        self.tokens: GoogleTokens = tokens or ApplicationDefaultTokens()
        # A service's URL never changes; remembering it saves one Admin API read per call.
        self._urls: dict[str, str] = {}

    def _regions(self) -> list[str]:
        """Every region a profile may launch in. Cloud Run cannot list all regions at once."""
        return sorted({r for profile in self.settings.profiles.values() for r in profile.regions})

    def _path(self, region: str, name: str | None = None) -> str:
        parent = f"projects/{self.settings.cloud_run_project}/locations/{region}/services"
        return parent if name is None else f"{parent}/{name}"

    async def _request(
        self,
        method: str,
        path: str,
        body: dict[str, JsonValue] | None = None,
        *,
        params: dict[str, str] | None = None,
        tolerate: tuple[int, ...] = (404,),
    ) -> httpx.Response:
        if (
            not self.settings.cloud_run_project
            or not self.settings.cloud_run_service_account
            or not self.settings.deployment_id
        ):
            raise ComputeProviderError("Cloud Run sessions are not configured. Ask an admin.")
        token = await self.tokens.access_token()
        response: httpx.Response | None = None
        with contextlib.suppress(httpx.HTTPError):
            async with httpx.AsyncClient(transport=self.transport, timeout=50) as client:
                response = await client.request(
                    method,
                    _API + path,
                    headers={"Authorization": "Bearer " + token},
                    params=params,
                    json=body,
                )
        if response is not None and (response.is_success or response.status_code in tolerate):
            return response
        if response is not None:
            # The status alone: bodies and URLs can carry tokens and project details.
            log.warning("compute.cloud_run_refused", method=method, status=response.status_code)
        raise ComputeProviderError("Cloud Run is unavailable. Cleanup will retry; try again later.")

    def _labels(self, resource: ComputeResource) -> dict[str, str]:
        return {
            _DEPLOYMENT_LABEL: deployment_label(resource.deployment_id),
            _SESSION_LABEL: resource.session_id.hex,
        }

    def _check(self, resource: ComputeResource, service: _Service | None = None) -> None:
        """Refuse another deployment's resource, and a service whose labels disagree."""
        if (
            resource.deployment_id != self.settings.deployment_id
            or resource.app_name != app_name(self.settings.deployment_id, resource.session_id)
            or (
                service is not None and not self._labels(resource).items() <= service.labels.items()
            )
        ):
            raise ComputeProviderError(
                "Cloud Run resource ownership does not match this deployment."
            )
        if resource.region is not None and region_provider(resource.region) != "cloud_run":
            # A missing service would read as deleted while the other provider's
            # machine kept running and billing.
            raise ComputeProviderError(OTHER_PROVIDER)

    async def _get(self, region: str, name: str) -> _Service | None:
        response = await self._request("GET", self._path(region, name))
        if response.status_code == 404:
            return None
        try:
            return _Service.model_validate_json(response.content)
        except ValueError:
            raise ComputeProviderError("Cloud Run returned an invalid service record.") from None

    def _resource(self, session: ComputeSession) -> ComputeResource:
        resource = ComputeResource(
            app_name=session.app_name,
            deployment_id=session.deployment_id,
            session_id=session.id,
            region=session.region,
        )
        self._check(resource)
        return resource

    def _service_body(
        self, session: ComputeSession, labels: dict[str, str], token: str
    ) -> dict[str, JsonValue]:
        return {
            "labels": {**labels},
            # Reachable from the control plane wherever it runs; IAM keeps it private.
            "ingress": "INGRESS_TRAFFIC_ALL",
            "invokerIamDisabled": False,
            # Exactly one instance, so every call reaches the same files and processes.
            "scaling": {"minInstanceCount": 1, "maxInstanceCount": 1},
            "template": {
                "serviceAccount": self.settings.cloud_run_service_account,
                "executionEnvironment": "EXECUTION_ENVIRONMENT_GEN2",
                "timeout": "3600s",
                "containers": [
                    {
                        "image": session.image,
                        "ports": [{"containerPort": 8080}],
                        "env": [{"name": "MCP_AUTH_TOKEN", "value": token}],
                        "resources": {
                            "limits": {
                                "cpu": str(session.cpus),
                                "memory": f"{session.memory_mb}Mi",
                            },
                            # Instance-based billing: background commands keep their
                            # CPU when no request is open.
                            "cpuIdle": False,
                            "startupCpuBoost": True,
                        },
                    }
                ],
            },
        }

    async def ensure(self, session: ComputeSession, token: str) -> ComputeResource:
        resource = self._resource(session)
        service = await self._get(session.region, session.app_name)
        if service is None:
            # The service id is the deterministic app name, so a retried create
            # answers 409 and the read below recovers the first one.
            await self._request(
                "POST",
                self._path(session.region),
                self._service_body(session, self._labels(resource), token),
                params={"serviceId": session.app_name},
                tolerate=(409,),
            )
            service = await self._get(session.region, session.app_name)
            if service is None:
                raise ComputeProviderError(
                    "Cloud Run has not created the machine yet. Try again later."
                )
        self._check(resource, service)
        # Creation continues on Google's side; status reports it until it is ready.
        return resource.model_copy(
            update={"machine_id": service.uid or None, "machine_state": service.machine_state}
        )

    async def inspect(self, session: ComputeSession) -> ComputeResource:
        resource = self._resource(session)
        service = await self._get(session.region, session.app_name)
        if service is None:
            return resource.model_copy(update={"machine_state": "no_machines"})
        self._check(resource, service)
        return resource.model_copy(
            update={"machine_id": service.uid or None, "machine_state": service.machine_state}
        )

    async def delete(self, resource: ComputeResource) -> None:
        self._check(resource)
        self._urls.pop(resource.app_name, None)
        regions = [resource.region] if resource.region is not None else self._regions()
        for region in regions:
            service = await self._get(region, resource.app_name)
            if service is None:
                continue
            self._check(resource, service)
            response = await self._request(
                "DELETE", self._path(region, resource.app_name), tolerate=(404, 409)
            )
            if response.status_code == 200:
                await self._wait(response)
            if await self._get(region, resource.app_name) is not None:
                raise ComputeProviderError("Cloud Run deletion is pending. Cleanup will retry.")

    async def _wait(self, response: httpx.Response) -> None:
        """Let Google finish a delete for a bounded time, so a stop usually confirms."""
        try:
            operation = _Operation.model_validate_json(response.content)
        except ValueError:
            return
        if operation.done or not re.fullmatch(
            r"projects/[^/]+/locations/[^/]+/operations/[^/]+", operation.name
        ):
            return
        # The read that follows decides; a failed wait is not a failed delete.
        with contextlib.suppress(ComputeProviderError):
            await self._request("POST", operation.name + ":wait", {"timeout": _DELETE_WAIT})

    async def inventory(self) -> list[ComputeResource]:
        prefix = app_prefix(self.settings.deployment_id)
        resources: list[ComputeResource] = []
        for region in self._regions():
            page = ""
            while True:
                response = await self._request(
                    "GET",
                    self._path(region),
                    params={"pageSize": "100"} | ({"pageToken": page} if page else {}),
                )
                try:
                    listed = _Services.model_validate_json(response.content)
                except ValueError:
                    raise ComputeProviderError(
                        "Cloud Run returned an invalid service list."
                    ) from None
                for service in listed.services:
                    name = service.name.rsplit("/", 1)[-1]
                    if not re.fullmatch(re.escape(prefix) + r"[0-9a-f]{32}", name):
                        continue
                    resource = ComputeResource(
                        app_name=name,
                        deployment_id=self.settings.deployment_id,
                        session_id=uuid.UUID(name[len(prefix) :]),
                        machine_id=service.uid or None,
                        region=region,
                    )
                    try:
                        self._check(resource, service)
                    except ComputeProviderError:
                        continue  # Never destroy a service with contradictory ownership labels.
                    resources.append(resource)
                page = listed.next_page_token
                if not page:
                    break
        return resources

    async def _url(self, session: ComputeSession) -> str:
        """The session's own `run.app` address, read from Cloud Run and never from a caller."""
        resource = self._resource(session)
        known = self._urls.get(session.app_name)
        if known is not None:
            return known
        service = await self._get(session.region, session.app_name)
        if service is not None:
            self._check(resource, service)
            with contextlib.suppress(httpx.InvalidURL):
                url = httpx.URL(service.uri)
                if url.scheme == "https" and url.host.endswith(".run.app"):
                    self._urls[session.app_name] = known = f"https://{url.host}"
                    return known
        raise ComputeProviderError(
            "The session did not answer. Check its status before retrying a command."
        )

    async def call(
        self, session: ComputeSession, token: str, tool: str, arguments: dict[str, JsonValue]
    ) -> JsonValue:
        url = await self._url(session)
        # Cloud Run checks this Google identity token and leaves `Authorization` for
        # the session's own token. Its audience is the service URL.
        identity = await self.tokens.id_token(url)
        return await call_thin_mcp(
            url + "/mcp/",
            token,
            tool,
            arguments,
            transport=self.transport,
            platform_headers={"X-Serverless-Authorization": "Bearer " + identity},
        )
