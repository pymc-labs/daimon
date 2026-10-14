"""Explicit operator capability for chat Add-skill on the Gemini catalog.

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
from daimon.adapters.mcp.tools._native_resources import (
    MCP_AUTHORIZATION_ID,
    bind_native_host,
    check_skill_origin,
    check_skill_revision,
)
from daimon.core.channel_backend import channel_ref, current_backend
from daimon.core.stores.domain import TurnOriginRow
from fastmcp.exceptions import ToolError
from mux.profiles.gemini import INLINE_REUSE

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
            for value in (
                self.platform,
                self.channel_id,
                self.config_digest,
                self.account_scope_id,
            )
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
    check_skill_origin(auth, origin)
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
            session,
            channel_ref(auth.tenant_id, origin.platform, origin.parent_channel_id),
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
    if len(authorized) != 1:
        raise ToolError(
            "This channel's Gemini skill capability is not authorized for this configuration. "
            "Nothing was changed. Do not retry."
        )
    check_skill_revision(revision, INLINE_REUSE)
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
    """Select the native Gemini catalog for the shared scope binder."""
    from mux.drivers.gemini import GeminiCatalogAdapter

    return bind_native_host(
        host,
        auth=auth,
        workspace=workspace,
        profile=INLINE_REUSE,
        catalog_type=GeminiCatalogAdapter,
    )
