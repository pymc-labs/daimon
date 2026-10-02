from __future__ import annotations

import uuid
from dataclasses import dataclass

from daimon.adapters.cli.sessions_bootstrap import SessionBootstrapError
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.tenants import get_tenant
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class TenantSelector:
    """Raw --tenant/--guild option values from a group callback, pre-resolution."""

    tenant_id: str | None = None
    guild_id: str | None = None


async def discover_tenant(
    session: AsyncSession,
    *,
    override: uuid.UUID | None = None,
    workspace_id: str = "local",
) -> uuid.UUID:
    if override is not None:
        if await get_tenant(session, override) is None:
            raise SessionBootstrapError(
                "tenant_not_found",
                f"tenant {override} not found.\n  run: daimon tenants list  "
                "(to see which tenants are registered)",
            )
        return override

    tenant_id = derive_tenant_uuid(platform="cli", workspace_id=workspace_id)
    row = await get_tenant(session, tenant_id)
    if row is None:
        raise SessionBootstrapError(
            "defaults_missing",
            "no tenant exists.\n  run: daimon defaults apply",
        )
    return tenant_id


async def resolve_tenant_override(
    session: AsyncSession,
    selector: TenantSelector | None,
) -> uuid.UUID | None:
    """Turn a --tenant/--guild selector into the uuid discover_tenant expects.

    Returns None when no override was requested — the caller falls through to
    the local CLI tenant, unchanged from today. Existence is deliberately NOT
    checked here; discover_tenant already does that and owns that error message.
    """
    if selector is None or (selector.tenant_id is None and selector.guild_id is None):
        return None
    if selector.tenant_id is not None and selector.guild_id is not None:
        raise SessionBootstrapError(
            "tenant_override_conflict",
            "--tenant and --guild cannot be combined; pass exactly one.",
        )
    if selector.tenant_id is not None:
        try:
            return uuid.UUID(selector.tenant_id)
        except ValueError as err:
            raise SessionBootstrapError(
                "tenant_override_invalid",
                f"{selector.tenant_id!r} is not a valid tenant uuid. "
                "Pass a Discord guild id via --guild instead.",
            ) from err
    assert selector.guild_id is not None  # narrowed by the branches above
    return derive_tenant_uuid(platform="discord", workspace_id=selector.guild_id)


async def resolve_tenant_display(session: AsyncSession, tenant_id: uuid.UUID) -> str:
    """Human-readable tenant label for confirmation prompts: platform + external id + uuid.

    Falls back to the bare uuid when no row exists — the existence check belongs
    to discover_tenant, not this helper.
    """
    row = await get_tenant(session, tenant_id)
    if row is None:
        return str(tenant_id)
    return f"{row.platform}:{row.external_id} ({tenant_id})"
