"""Environment tools: list / get / create / update / archive.

Environments mirror the agent tool group minus fork and model field.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any

from anthropic.types.beta import BetaEnvironment
from anthropic.types.beta.beta_cloud_config_params import BetaCloudConfigParams
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import (
    _auth,  # pyright: ignore[reportPrivateUsage]
    _require_admin,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools._rule_view import load_caller_hidden_environments
from daimon.adapters.mcp.tools._scopes import require_scope, scope_tags
from daimon.core.channel_environments import (
    archive_needs_confirm,
    build_archive_environment_note,
    build_limited_channels_confirm,
    update_needs_confirm,
)
from daimon.core.defaults.ma_index import (
    find_environment_by_daimon_tag,
    find_environments_by_daimon_tag,
    list_environments_by_tenant,
)
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_MANAGED,
    MA_METADATA_KEY_NAME,
    build_metadata,
)
from daimon.core.mux_backend import resource_scope
from daimon.core.mux_compat import archive_environment, create_environment, update_environment
from daimon.core.specs import EnvironmentSpec
from daimon.core.stores.scoped_config_write import clear_environment_references
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel

CONFIRM_OPEN_NETWORK_ASK = (
    " Ask the caller whether to go ahead; only if they confirm, retry with "
    "confirm_open_network=true. Never confirm on their behalf."
)


class EnvironmentInfo(BaseModel):
    name: str
    id: str
    description: str | None
    created_at: datetime.datetime

    @classmethod
    def from_ma(cls, env: BetaEnvironment) -> EnvironmentInfo:
        return cls(
            name=env.name,
            id=env.id,
            description=env.description,
            created_at=datetime.datetime.fromisoformat(env.created_at),
        )


async def _list_environments_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    page: str | None,
) -> list[EnvironmentInfo]:
    del page
    require_scope(auth, "tenant:read")
    rows = await list_environments_by_tenant(runtime.client, tenant_id=auth.tenant_id)
    hidden = await load_caller_hidden_environments(runtime, auth)
    return [
        EnvironmentInfo.from_ma(e)
        for e in rows
        if e.metadata.get(MA_METADATA_KEY_NAME) not in hidden
    ]


async def _get_environment_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    name: str,
) -> EnvironmentInfo:
    env = await find_environment_by_daimon_tag(runtime.client, tenant_id=auth.tenant_id, name=name)
    if env is None or name in await load_caller_hidden_environments(runtime, auth):
        raise ToolError(f"environment '{name}' not found")
    return EnvironmentInfo.from_ma(env)


async def _reject_environment_name_collision(
    runtime: McpRuntime,
    auth: AuthIdentity,
    name: str,
) -> None:
    """Raise ToolError if any non-archived environment with this name exists in the tenant.

    Tenant-scoped name uniqueness matches the resolver's (daimon_tenant, daimon_name)
    identity model; ANY non-empty match blocks the create regardless of owner (
    matching the agents' _reject_guild_name_collision).
    """
    matches = await find_environments_by_daimon_tag(
        runtime.client, tenant_id=auth.tenant_id, name=name
    )
    if matches:
        raise ToolError(f"environment '{name}' already exists in this server — pick another name")


async def _create_environment_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    spec: EnvironmentSpec,
) -> EnvironmentInfo:
    # Deliberately ungated, unlike update/archive below. A freshly created
    # environment is inert: nothing runs in it until an admin picks it for a
    # channel or the workspace, or a channel's admin for their channel, via
    # set_channel_environment, which is gated. So
    # the gate here bought no isolation while blocking the ordinary onboarding
    # ask -- "make me an agent that can run pymc" -- for every non-admin.
    # Matches create_agent, which is ungated for the same reason (fork_agent is
    # admin-only: a fork would copy a live agent's prompt and connectors).
    await _reject_environment_name_collision(runtime, auth, spec.name)
    payload = spec.model_dump(exclude_none=True)
    payload["metadata"] = build_metadata(tenant_id=auth.tenant_id, name=spec.name)
    ma_env = await create_environment(
        runtime.client,
        payload,
        scope=resource_scope(tenant_id=str(auth.tenant_id), account_id=str(auth.account_id)),
    )
    return EnvironmentInfo.from_ma(ma_env)


def _reject_managed_environment(env: BetaEnvironment, *, refusal: str) -> None:
    """Refuse chat edits of a defaults-managed environment, admins included.

    A chat edit never restamps the reconciler's spec hash, so the drift would
    survive every later `defaults apply`; archiving one strands every seeded
    agent scoped onto it.
    """
    if env.metadata.get(MA_METADATA_KEY_MANAGED) == "true":
        raise ToolError(f"environment '{env.name}' is managed by defaults; {refusal}")


async def _update_environment_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    name: str,
    *,
    config: BetaCloudConfigParams | None,
    description: str | None,
    confirm_open_network: bool = False,
) -> EnvironmentInfo:
    _require_admin(auth)
    maybe_fields: dict[str, Any] = {"config": config, "description": description}
    patch: dict[str, Any] = {k: v for k, v in maybe_fields.items() if v is not None}
    if not patch:
        raise ToolError("update_environment: at least one field is required")
    env = await find_environment_by_daimon_tag(runtime.client, tenant_id=auth.tenant_id, name=name)
    if env is None or name in await load_caller_hidden_environments(runtime, auth):
        raise ToolError(f"environment '{name}' not found")
    _reject_managed_environment(
        env,
        refusal="chat tools cannot modify it. Use create_environment to make a new one instead.",
    )
    if not confirm_open_network:
        async with runtime.session_factory() as session:
            confirm = await update_needs_confirm(
                session,
                tenant_id=auth.tenant_id,
                environment=env,
                config=config,
                default=runtime.deployment_default,
            )
        if confirm:
            raise ToolError(
                build_limited_channels_confirm(environment_name=name) + CONFIRM_OPEN_NETWORK_ASK
            )
    updated = await update_environment(
        runtime.client,
        env.id,
        patch,
        scope=resource_scope(tenant_id=str(auth.tenant_id), account_id=str(auth.account_id)),
    )
    return EnvironmentInfo.from_ma(updated)


@dataclass(frozen=True)
class ArchiveEnvironmentResult:
    """Result returned from archive_environment."""

    name: str
    cleared_picks: int
    """Channel and workspace picks of this environment that were cleared."""
    note: str
    """What changed, to report back as is."""


async def _archive_environment_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    name: str,
    *,
    confirm_open_network: bool = False,
) -> ArchiveEnvironmentResult:
    """Archive the environment, then clear every pick of it so those channels fall through."""
    _require_admin(auth)
    env = await find_environment_by_daimon_tag(runtime.client, tenant_id=auth.tenant_id, name=name)
    if env is None or name in await load_caller_hidden_environments(runtime, auth):
        raise ToolError(f"environment '{name}' not found")
    _reject_managed_environment(
        env,
        refusal="built-in agents run in it, so chat tools cannot archive it. Nothing changed.",
    )
    if not confirm_open_network:
        async with runtime.session_factory() as session:
            confirm = await archive_needs_confirm(
                session,
                runtime.client,
                tenant_id=auth.tenant_id,
                environment_name=name,
                default=runtime.deployment_default,
            )
        if confirm:
            raise ToolError(
                build_limited_channels_confirm(environment_name=None) + CONFIRM_OPEN_NETWORK_ASK
            )
    await archive_environment(
        runtime.client,
        env.id,
        scope=resource_scope(tenant_id=str(auth.tenant_id), account_id=str(auth.account_id)),
    )
    async with runtime.session_factory.begin() as session:
        cleared = await clear_environment_references(
            session, tenant_id=auth.tenant_id, environment_name=name
        )
    return ArchiveEnvironmentResult(
        name=name,
        cleared_picks=cleared,
        note=build_archive_environment_note(environment_name=name, cleared=cleared),
    )


def register_environment_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    # Reads are ungated — full visibility for every session, matching the
    # agents/skills read tools. list_environments also opens to operator tokens
    # with tenant:read, so an integration can pick one for set_channel_environment.
    # Other callers don't see names only isolated channels across their line pick.
    # Mutations carry tags={"admin"} plus the _require_admin impl gate, with one
    # deliberate exception:
    # create_environment is ungated, because a new environment is inert until an
    # admin or a channel's admin picks it. See the comment on _create_environment_impl.
    @mcp.tool(tags=scope_tags("tenant:read"))
    async def list_environments(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        page: str | None = None,
    ) -> list[EnvironmentInfo]:
        """List environments in the tenant pool. ``page`` is reserved for future pagination.

        Operator tokens need the ``tenant:read`` scope.
        """
        return await _list_environments_impl(runtime, await _auth(ctx), page)

    @mcp.tool
    async def get_environment(ctx: Context, name: str) -> EnvironmentInfo:  # pyright: ignore[reportUnusedFunction]
        """Return one environment by name."""
        return await _get_environment_impl(runtime, await _auth(ctx), name)

    @mcp.tool
    async def create_environment(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        spec: EnvironmentSpec,
    ) -> EnvironmentInfo:
        """Create a sandbox environment a channel or the workspace can later run in.

        Packages are declared under ``spec.config.packages``, one list per
        ecosystem: ``apt``, ``cargo``, ``gem``, ``go``, ``npm``, ``pip``. For
        example, an environment with numpy, pandas, and pymc sets
        ``config.packages.pip = ["numpy", "pandas", "pymc"]``. Pin a version
        with the ecosystem's own syntax — for ``pip``, ``package==1.0.0``.

        The package lists you send are the environment's complete package
        set, not a delta: a later change to these packages replaces rather
        than merges, so omitting a previously-installed package removes it.
        Always send the full intended list for every ecosystem you care about.

        The new environment is inert until an admin picks it for a channel or
        the workspace, or a channel's admin picks it for their channel, with
        ``set_channel_environment``; creating one here changes nothing yet.
        """
        return await _create_environment_impl(runtime, await _auth(ctx), spec)

    @mcp.tool(tags={"admin"})
    async def update_environment(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        name: str,
        *,
        config: BetaCloudConfigParams | None = None,
        description: str | None = None,
        confirm_open_network: bool = False,
    ) -> EnvironmentInfo:
        """Patch-update an environment. Omitted (None) fields are preserved.

        Opening the network of an environment a sealed channel runs in needs the
        caller's confirmation: pass ``confirm_open_network=true`` only after they
        confirm it.
        """
        return await _update_environment_impl(
            runtime,
            await _auth(ctx),
            name,
            config=config,
            description=description,
            confirm_open_network=confirm_open_network,
        )

    @mcp.tool(tags={"admin"})
    async def archive_environment(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, name: str, confirm_open_network: bool = False
    ) -> ArchiveEnvironmentResult:
        """Archive the MA environment and delete from the tenant pool.

        Channels and the workspace default that picked it are cleared, so they
        fall through to the next tier; read ``note`` back to the user. When that
        leaves a sealed channel on an environment with unrestricted networking,
        pass ``confirm_open_network=true`` only after the caller confirms it.
        """
        return await _archive_environment_impl(
            runtime, await _auth(ctx), name, confirm_open_network=confirm_open_network
        )
