"""Environment tools: list / get / create / update / archive.

Environments mirror the agent tool group minus fork and model field.
"""

from __future__ import annotations

import datetime
from typing import Any

from anthropic.types.beta import BetaEnvironment
from anthropic.types.beta.beta_cloud_config_params import BetaCloudConfigParams
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import (
    _auth,  # pyright: ignore[reportPrivateUsage]
    _require_admin,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.defaults.ma_index import (
    find_environment_by_daimon_tag,
    find_environments_by_daimon_tag,
    list_environments_by_tenant,
)
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED, build_metadata
from daimon.core.specs import EnvironmentSpec
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel


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
    rows = await list_environments_by_tenant(runtime.client, tenant_id=auth.tenant_id)
    return [EnvironmentInfo.from_ma(e) for e in rows]


async def _get_environment_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    name: str,
) -> EnvironmentInfo:
    env = await find_environment_by_daimon_tag(runtime.client, tenant_id=auth.tenant_id, name=name)
    if env is None:
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
    # environment is inert: nothing routes to it until an admin scopes an agent
    # onto a channel or the workspace via set_agent_default, which is gated. So
    # the gate here bought no isolation while blocking the ordinary onboarding
    # ask -- "make me an agent that can run pymc" -- for every non-admin.
    # Matches create_agent / fork_agent, which are ungated for the same reason.
    await _reject_environment_name_collision(runtime, auth, spec.name)
    payload = spec.model_dump(exclude_none=True)
    payload["metadata"] = build_metadata(tenant_id=auth.tenant_id, name=spec.name)
    ma_env = await runtime.client.beta.environments.create(**payload)
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
) -> EnvironmentInfo:
    _require_admin(auth)
    maybe_fields: dict[str, Any] = {"config": config, "description": description}
    patch: dict[str, Any] = {k: v for k, v in maybe_fields.items() if v is not None}
    if not patch:
        raise ToolError("update_environment: at least one field is required")
    env = await find_environment_by_daimon_tag(runtime.client, tenant_id=auth.tenant_id, name=name)
    if env is None:
        raise ToolError(f"environment '{name}' not found")
    _reject_managed_environment(
        env,
        refusal="chat tools cannot modify it. Use create_environment to make a new one instead.",
    )
    updated = await runtime.client.beta.environments.update(env.id, **patch)
    return EnvironmentInfo.from_ma(updated)


async def _archive_environment_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    name: str,
) -> None:
    _require_admin(auth)
    env = await find_environment_by_daimon_tag(runtime.client, tenant_id=auth.tenant_id, name=name)
    if env is None:
        raise ToolError(f"environment '{name}' not found")
    _reject_managed_environment(
        env,
        refusal="built-in agents run in it, so chat tools cannot archive it. Nothing changed.",
    )
    await runtime.client.beta.environments.archive(env.id)


def register_environment_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    # Reads are untagged and ungated — full visibility for every session,
    # matching the agents/skills read tools. Mutations carry tags={"admin"} plus
    # the _require_admin impl gate, with one deliberate exception:
    # create_environment is ungated, because a new environment is inert until an
    # admin scopes an agent onto it. See the comment on _create_environment_impl.
    @mcp.tool
    async def list_environments(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        page: str | None = None,
    ) -> list[EnvironmentInfo]:
        """List environments in the tenant pool. ``page`` is reserved for future pagination."""
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
        """Create a sandbox environment an agent can later be scoped onto.

        Packages are declared under ``spec.config.packages``, one list per
        ecosystem: ``apt``, ``cargo``, ``gem``, ``go``, ``npm``, ``pip``. For
        example, an environment with numpy, pandas, and pymc sets
        ``config.packages.pip = ["numpy", "pandas", "pymc"]``. Pin a version
        with the ecosystem's own syntax — for ``pip``, ``package==1.0.0``.

        The package lists you send are the environment's complete package
        set, not a delta: a later change to these packages replaces rather
        than merges, so omitting a previously-installed package removes it.
        Always send the full intended list for every ecosystem you care about.

        The new environment is inert until an admin scopes an agent onto it
        (an admin-only action) — creating one here does not change what any
        agent runs on yet.
        """
        return await _create_environment_impl(runtime, await _auth(ctx), spec)

    @mcp.tool(tags={"admin"})
    async def update_environment(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        name: str,
        *,
        config: BetaCloudConfigParams | None = None,
        description: str | None = None,
    ) -> EnvironmentInfo:
        """Patch-update an environment. Omitted (None) fields are preserved."""
        return await _update_environment_impl(
            runtime,
            await _auth(ctx),
            name,
            config=config,
            description=description,
        )

    @mcp.tool(tags={"admin"})
    async def archive_environment(ctx: Context, name: str) -> None:  # pyright: ignore[reportUnusedFunction]
        """Archive the MA environment and delete from the tenant pool."""
        await _archive_environment_impl(runtime, await _auth(ctx), name)
