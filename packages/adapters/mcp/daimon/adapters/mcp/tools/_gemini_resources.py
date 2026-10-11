"""Explicit operator capability for chat Add-skill on the Gemini A1 catalog.

No SDK, manifest or credential discovery. The operator supplies the resource
factory and exact account/channel configuration grants. Missing capability
keeps the existing dispatch; an enabled Gemini route fails closed on mismatch.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpResourceHostFactory, McpRuntime
from daimon.adapters.mcp.tools._openai_resources import MCP_AUTHORIZATION_ID
from daimon.core.channel_backend import channel_ref, current_backend
from daimon.core.stores.domain import TurnOriginRow
from fastmcp.exceptions import ToolError

if TYPE_CHECKING:
    from daimon.core.neutral_resources import NeutralResourceHost

PROFILE = "gemini.inline_reuse"


@dataclass(frozen=True)
class GeminiSkillGrant:
    """One operator-authorized account and exact channel configuration."""

    tenant_id: uuid.UUID
    account_id: uuid.UUID
    platform: str
    channel_id: str
    config_digest: str
    account_scope_id: str

    def __post_init__(self) -> None:
        if any(
            not value.strip()
            for value in (self.platform, self.channel_id, self.config_digest, self.account_scope_id)
        ):
            raise ValueError("Gemini skill grants require explicit routing identities")


@dataclass(frozen=True)
class GeminiSkillCapability:
    """An operator-owned resource factory; supplying it alone does not enable it."""

    factory: McpResourceHostFactory
    grants: tuple[GeminiSkillGrant, ...]
    enabled: bool = False

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ValueError("Gemini skill capability must be an explicit bool")


async def gemini_skill_host(
    runtime: McpRuntime,
    auth: AuthIdentity,
    origin: TurnOriginRow | None,
    *,
    capability: GeminiSkillCapability | None,
    operation: str,
) -> NeutralResourceHost | None:
    """Select only an explicitly enabled, freshly authorized Gemini channel."""
    if capability is None or not capability.enabled or origin is None:
        return None
    # An origin must be the verified caller's own chat identity before routing.
    if (
        origin.tenant_id != auth.tenant_id
        or origin.account_id != auth.account_id
        or origin.platform != auth.platform
    ):
        raise ToolError("This conversation's identity changed. Nothing was changed. Do not retry.")
    # Unselected callers/channels pay no new configuration read. The operator's
    # capability for another channel must not change this channel's path.
    selected = tuple(
        grant
        for grant in capability.grants
        if grant.tenant_id == auth.tenant_id
        and grant.account_id == auth.account_id
        and grant.platform == origin.platform
        and grant.channel_id == origin.parent_channel_id
    )
    if not selected:
        return None
    async with runtime.session_factory() as session:
        revision = await current_backend(
            session, channel_ref(auth.tenant_id, origin.platform, origin.parent_channel_id)
        )
    if revision is None or revision.profile != PROFILE:
        return None
    factory = capability.factory
    authorized = [
        grant
        for grant in selected
        if grant.config_digest == revision.digest
        and grant.account_scope_id == factory.account_scope_id
    ]
    if (
        len(authorized) != 1
        or revision.backend != "gemini"
        or revision.model != "gemini-3.8-flash"
        or revision.thread_mode != "per_caller"
    ):
        raise ToolError(
            "This channel's Gemini skill capability is not authorized for this configuration. "
            "Nothing was changed. Do not retry."
        )
    return bind_gemini_host(
        factory(
            tenant_id=auth.tenant_id,
            account_id=auth.account_id,
            principal_id=f"mcp:{auth.account_id}",
            authorization_id=MCP_AUTHORIZATION_ID,
            operation_key=f"mcp-{operation}:{uuid.uuid4()}",
        ),
        auth=auth,
        workspace=authorized[0].account_scope_id,
    )


def bind_gemini_host(host: object, *, auth: AuthIdentity, workspace: str) -> NeutralResourceHost:
    """Validate the returned host's entire resource scope before any catalog I/O."""
    from daimon.core.neutral_resources import NeutralResourceHost
    from mux.drivers.gemini import GeminiCatalogAdapter

    if not isinstance(host, NeutralResourceHost):
        raise ToolError("This deployment's resource host is misconfigured. Nothing was changed.")
    scope = host.scope
    capabilities = host.backend.capabilities()
    adapter = host.backend.agents
    if (
        scope.tenant_id != str(auth.tenant_id)
        or scope.account_id != str(auth.account_id)
        or scope.principal_id != f"mcp:{auth.account_id}"
        or scope.authorization_id != MCP_AUTHORIZATION_ID
        or scope.is_platform
        or scope.is_legacy_host_authorized
        or host.workspace != workspace
        or capabilities.provider != "gemini"
        or capabilities.profile_id != PROFILE
        or not isinstance(adapter, GeminiCatalogAdapter)
        or not adapter.enabled
        or adapter.account_scope_id != workspace
    ):
        raise ToolError(
            "This deployment's resource host is for a different tenant, account or workspace. "
            "Nothing was changed. Do not retry."
        )
    return host
