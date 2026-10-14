"""Shared native resource admission and host binding before any catalog access."""

from __future__ import annotations

from typing import TYPE_CHECKING

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.core.channel_backend import BackendUnsupported, check_backend
from daimon.core.stores.domain import TurnOriginRow
from fastmcp.exceptions import ToolError
from mux.contracts.config import ConfigRevision
from mux.contracts.profile import Profile

if TYPE_CHECKING:
    from daimon.core.neutral_resources import NeutralResourceHost

MCP_AUTHORIZATION_ID = "mcp-deployment-operator"


def check_skill_origin(auth: AuthIdentity, origin: TurnOriginRow) -> None:
    if (
        origin.tenant_id != auth.tenant_id
        or origin.account_id != auth.account_id
        or origin.platform != auth.platform
    ):
        raise ToolError("This conversation's identity changed. Nothing was changed. Do not retry.")


def check_skill_revision(revision: ConfigRevision, profile: Profile) -> None:
    try:
        check_backend(revision)
    except BackendUnsupported:
        raise ToolError(
            "This channel's skill capability is not authorized for this configuration. "
            "Nothing was changed. Do not retry."
        ) from None
    if (
        revision.profile != profile.profile_id
        or revision.backend != profile.provider
        or revision.thread_mode != "per_caller"
    ):
        raise ToolError(
            "This channel's skill capability is not authorized for this configuration. "
            "Nothing was changed. Do not retry."
        )


def bind_native_host(
    host: object,
    *,
    auth: AuthIdentity,
    workspace: str,
    profile: Profile,
    catalog_type: type[object],
) -> NeutralResourceHost:
    """Verify the returned host's complete authority before any catalog access."""
    from daimon.core.neutral_resources import NeutralResourceHost

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
        or capabilities.provider != profile.provider
        or capabilities.profile_id != profile.profile_id
        or not isinstance(adapter, catalog_type)
        or getattr(adapter, "provider", None) != profile.provider
        or getattr(adapter, "profile_id", None) != profile.profile_id
        or getattr(adapter, "enabled", None) is not True
        or getattr(adapter, "account_scope_id", None) != workspace
    ):
        raise ToolError(
            "This deployment's resource host is for a different tenant, account or workspace. "
            "Nothing was changed. Do not retry."
        )
    return host
