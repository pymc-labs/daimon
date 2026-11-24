"""Provider boundary for disposable compute. No caller supplies hosts or provider IDs."""

from __future__ import annotations

import hashlib
import hmac
import uuid
from typing import Literal, Protocol

from daimon.core.compute_models import ComputeResource, ComputeSession
from daimon.core.config import ComputeSettings
from pydantic import JsonValue


class ComputeProviderError(Exception):
    """A sanitized provider failure, safe to relay without request or response bodies."""


OTHER_PROVIDER = "This machine was started on another cloud provider. Ask an admin."


def region_provider(region: str) -> Literal["fly", "cloud_run"]:
    """The provider a stored region names: Google regions carry a hyphen, Fly's never do.

    A session row does not record its provider. Each provider refuses the other's
    regions, so changing `DAIMON_COMPUTE__PROVIDER` while sessions remain leaves their
    cleanup pending instead of marking a machine deleted that still runs.
    """
    return "cloud_run" if "-" in region else "fly"


def app_prefix(deployment_id: str) -> str:
    return "dmn-" + hashlib.sha256(deployment_id.encode()).hexdigest()[:12] + "-"


def app_name(deployment_id: str, session_id: uuid.UUID) -> str:
    return app_prefix(deployment_id) + session_id.hex


def session_token(settings: ComputeSettings, session_id: uuid.UUID) -> str:
    key = settings.session_auth_key
    if key is None or not key.get_secret_value() or not settings.deployment_id:
        raise ComputeProviderError("Compute authentication is not configured. Ask an admin.")
    return hmac.new(
        key.get_secret_value().encode(),
        f"{settings.deployment_id}:{session_id}".encode(),
        hashlib.sha256,
    ).hexdigest()


class ComputeProvider(Protocol):
    async def ensure(self, session: ComputeSession, token: str) -> ComputeResource: ...
    async def delete(self, resource: ComputeResource) -> None: ...
    async def inspect(self, session: ComputeSession) -> ComputeResource: ...
    async def inventory(self) -> list[ComputeResource]: ...
    async def call(
        self, session: ComputeSession, token: str, tool: str, arguments: dict[str, JsonValue]
    ) -> JsonValue: ...


def compute_provider_for(settings: ComputeSettings) -> ComputeProvider:
    """The provider `DAIMON_COMPUTE__PROVIDER` selects: the only place one is chosen.

    Every process that launches, uses or cleans up sessions builds its provider here,
    so they cannot disagree about where a session runs.
    """
    # Imported here because each provider module imports this boundary.
    from daimon.core.compute_cloud_run import CloudRunComputeProvider
    from daimon.core.compute_fly import FlyComputeProvider

    match settings.provider:
        case "fly":
            return FlyComputeProvider(settings)
        case "cloud_run":
            return CloudRunComputeProvider(settings)
