"""Provider boundary for disposable compute. No caller supplies hosts or provider IDs."""

from __future__ import annotations

import hashlib
import hmac
import uuid
from typing import Protocol

from daimon.core.compute_models import ComputeResource, ComputeSession
from daimon.core.config import ComputeSettings
from pydantic import JsonValue


class ComputeProviderError(Exception):
    """A sanitized provider failure, safe to relay without request or response bodies."""


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
