"""The workspace's sandbox environments as an admin manages them: list, create, change, archive."""

from __future__ import annotations

import re
from typing import Annotated, Literal

from anthropic.types.beta import BetaCloudConfig, BetaEnvironment, BetaLimitedNetwork
from anthropic.types.beta.beta_cloud_config_params import BetaCloudConfigParams
from daimon.core.agent_admin import require_admin
from daimon.core.agent_editing import AgentActor, AgentEditDeps, AgentEditError
from daimon.core.agent_target import ChangeRecorder
from daimon.core.channel_environments import (
    archive_needs_confirm,
    build_archive_environment_note,
    build_limited_channels_confirm,
    has_open_network,
    update_needs_confirm,
)
from daimon.core.defaults.ma_index import (
    find_environments_by_daimon_tag,
    list_environments_by_tenant,
)
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_MANAGED,
    MA_METADATA_KEY_NAME,
    build_metadata,
)
from daimon.core.errors import DaimonError
from daimon.core.mux_compat import archive_environment, create_environment, update_environment
from daimon.core.stores.scoped_config_write import clear_environment_references
from pydantic import AfterValidator, BaseModel, Field, StringConstraints

ECOSYSTEMS = ("apt", "cargo", "gem", "go", "npm", "pip")
_HOST = re.compile(r"[A-Za-z0-9*][A-Za-z0-9.*-]*")

Ecosystem = Literal["apt", "cargo", "gem", "go", "npm", "pip"]
Package = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


def _host(value: str) -> str:
    if not _HOST.fullmatch(value):
        raise ValueError("That is not a host name.")
    return value


Host = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=253),
    AfterValidator(_host),
]


class EnvironmentNeedsConfirm(DaimonError):
    """The change leaves a sealed channel on an open network; an admin confirms it first."""


class EnvironmentInput(BaseModel):
    """What an admin sets: the packages and the network."""

    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
    description: Annotated[str, StringConstraints(max_length=500)] = ""
    network: Literal["limited", "unrestricted"] = "limited"
    allowed_hosts: list[Host] = Field(default_factory=list, max_length=50)
    allow_package_managers: bool = True
    allow_mcp_servers: bool = True
    packages: dict[Ecosystem, list[Package]] = Field(default_factory=dict[Ecosystem, list[Package]])

    def config(self, *, replace: bool) -> BetaCloudConfigParams:
        """The cloud config. An update sends every ecosystem, so a removed package goes."""
        held: dict[str, list[str]] = {k: list(v) for k, v in self.packages.items()}
        packages = {eco: held.get(eco, []) for eco in ECOSYSTEMS if replace or eco in held}
        networking = (
            {"type": "unrestricted"}
            if self.network == "unrestricted"
            else {
                "type": "limited",
                "allowed_hosts": list(self.allowed_hosts),
                "allow_package_managers": self.allow_package_managers,
                "allow_mcp_servers": self.allow_mcp_servers,
            }
        )
        return {"type": "cloud", "networking": networking, "packages": packages}  # pyright: ignore[reportReturnType]


class EnvironmentDoc(BaseModel):
    id: str
    name: str
    description: str
    created_at: str
    updated_at: str
    managed: bool
    """A built-in agent runs in it, so it is read-only here."""
    cloud: bool
    network: Literal["limited", "unrestricted"]
    open_network: bool
    """Reaches beyond package managers and the agent's own servers."""
    allowed_hosts: list[str]
    allow_package_managers: bool
    allow_mcp_servers: bool
    packages: dict[str, list[str]]


def environment_doc(env: BetaEnvironment) -> EnvironmentDoc:
    doc = EnvironmentDoc(
        id=env.id,
        name=env.metadata.get(MA_METADATA_KEY_NAME) or env.name,
        description=env.description or "",
        created_at=env.created_at,
        updated_at=env.updated_at,
        managed=env.metadata.get(MA_METADATA_KEY_MANAGED) == "true",
        cloud=False,
        network="limited",
        open_network=has_open_network(env),
        allowed_hosts=[],
        allow_package_managers=False,
        allow_mcp_servers=False,
        packages={},
    )
    cfg = env.config
    if not isinstance(cfg, BetaCloudConfig):
        return doc
    net = cfg.networking
    held = {eco: list(getattr(cfg.packages, eco)) for eco in ECOSYSTEMS}
    doc = doc.model_copy(update={"cloud": True, "packages": {k: v for k, v in held.items() if v}})
    if isinstance(net, BetaLimitedNetwork):
        return doc.model_copy(
            update={
                "allowed_hosts": list(net.allowed_hosts),
                "allow_package_managers": net.allow_package_managers,
                "allow_mcp_servers": net.allow_mcp_servers,
            }
        )
    return doc.model_copy(update={"network": "unrestricted"})


async def _all(deps: AgentEditDeps, actor: AgentActor) -> list[BetaEnvironment]:
    return await list_environments_by_tenant(
        deps.client, tenant_id=actor.tenant_id, records=deps.session_factory
    )


async def _find(deps: AgentEditDeps, actor: AgentActor, environment_id: str) -> BetaEnvironment:
    for env in await _all(deps, actor):
        if env.id == environment_id:
            return env
    raise AgentEditError("No such environment.", kind="not_found")


def _editable(env: BetaEnvironment) -> None:
    if env.metadata.get(MA_METADATA_KEY_MANAGED) == "true":
        raise AgentEditError(
            f"{env.name} is managed by the server and can't be changed here.", kind="forbidden"
        )


async def list_environments(deps: AgentEditDeps, *, actor: AgentActor) -> list[EnvironmentDoc]:
    """The workspace's environments, for an admin."""
    require_admin(actor, "Environments")
    return sorted(
        (environment_doc(e) for e in await _all(deps, actor)), key=lambda e: e.name.casefold()
    )


async def create(
    deps: AgentEditDeps,
    *,
    actor: AgentActor,
    spec: EnvironmentInput,
    audited: ChangeRecorder | None = None,
) -> EnvironmentDoc:
    """A new environment. Nothing runs in it until a channel or the workspace picks it."""
    require_admin(actor, "Environments")
    if await find_environments_by_daimon_tag(
        deps.client, tenant_id=actor.tenant_id, name=spec.name, records=deps.session_factory
    ):
        raise AgentEditError(f"environment '{spec.name}' already exists", kind="taken")
    made = await create_environment(
        deps.client,
        {
            "name": spec.name,
            "description": spec.description,
            "config": spec.config(replace=False),
            "metadata": build_metadata(tenant_id=actor.tenant_id, name=spec.name),
        },
        scope=actor.scope(),
        records=deps.session_factory,
    )
    if audited is not None:
        async with deps.session_factory() as session, session.begin():
            await audited(session, spec.name)
    return environment_doc(made)


async def update(
    deps: AgentEditDeps,
    *,
    actor: AgentActor,
    environment_id: str,
    spec: EnvironmentInput,
    confirmed: bool = False,
    expected_updated_at: str | None = None,
    audited: ChangeRecorder | None = None,
) -> EnvironmentDoc:
    """Set an environment's packages and network; its name stays.

    A stale `expected_updated_at` (the version the caller loaded) is refused.

    Raises `EnvironmentNeedsConfirm` when this opens the network of one a sealed
    channel runs in, until `confirmed`.
    """
    require_admin(actor, "Environments")
    env = await _find(deps, actor, environment_id)
    _editable(env)
    if expected_updated_at is not None and env.updated_at != expected_updated_at:
        raise AgentEditError(
            "This environment changed since you opened it. Reload and try again.", kind="stale"
        )
    name = env.metadata.get(MA_METADATA_KEY_NAME) or env.name
    config = spec.config(replace=True)
    if not confirmed:
        async with deps.session_factory() as session:
            needed = await update_needs_confirm(
                session,
                tenant_id=actor.tenant_id,
                environment=env,
                config=config,
                default=deps.deployment_default,
            )
        if needed:
            raise EnvironmentNeedsConfirm(build_limited_channels_confirm(environment_name=name))
    updated = await update_environment(
        deps.client,
        env.id,
        {"config": config, "description": spec.description},
        scope=actor.scope(),
        records=deps.session_factory,
    )
    if audited is not None:
        async with deps.session_factory() as session, session.begin():
            await audited(session, name)
    return environment_doc(updated)


async def archive(
    deps: AgentEditDeps,
    *,
    actor: AgentActor,
    environment_id: str,
    confirmed: bool = False,
    audited: ChangeRecorder | None = None,
) -> str:
    """Archive an environment and clear every pick of it; what that did, in words."""
    require_admin(actor, "Environments")
    env = await _find(deps, actor, environment_id)
    _editable(env)
    name = env.metadata.get(MA_METADATA_KEY_NAME) or env.name
    if not confirmed:
        async with deps.session_factory() as session:
            needed = await archive_needs_confirm(
                session,
                deps.client,
                tenant_id=actor.tenant_id,
                environment_name=name,
                default=deps.deployment_default,
            )
        if needed:
            raise EnvironmentNeedsConfirm(build_limited_channels_confirm(environment_name=None))
    await archive_environment(
        deps.client, env.id, scope=actor.scope(), records=deps.session_factory
    )
    async with deps.session_factory() as session, session.begin():
        cleared = await clear_environment_references(
            session, tenant_id=actor.tenant_id, environment_name=name
        )
        if audited is not None:
            await audited(session, name)
    return build_archive_environment_note(environment_name=name, cleared=cleared)
