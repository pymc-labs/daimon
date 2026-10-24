"""Initialize tenant-owned self-hosted resources using only Postgres."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4

import typer
from daimon.core.db import build_engine
from daimon.core.stores.selfhosted_resources import (
    SelfHostedAgents,
    SelfHostedEnvironments,
)
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ModelRef, Scope
from mux.contracts.resources import Agent, AgentSpec, Environment, EnvironmentSpec
from mux.drivers.selfhosted import HarnessConfig
from mux.errors import ProviderError
from mux.profiles.selfhosted import LOOP
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

selfhosted_app = typer.Typer(help="Manage tenant-owned self-hosted agents and environments.")


async def initialize(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: UUID,
    name: str,
    model: str,
    system: str | None,
    environment_name: str,
    harness: HarnessConfig,
) -> tuple[str, str]:
    """Reuse matching named resources; refuse drift rather than rewriting them."""
    scope = Scope(
        tenant_id=str(tenant_id),
        account_id="operator",
        principal_id="cli",
        authorization_id="cli-deployment-operator",
    )
    agents = SelfHostedAgents(sessionmaker)
    environments = SelfHostedEnvironments(sessionmaker)
    agent_spec = AgentSpec(
        name=name, model=ModelRef(provider=LOOP.provider, id=model), system=system
    )
    environment_spec = EnvironmentSpec(
        name=environment_name,
        execution="self_hosted",
        native_config=ExtensionConfig(
            namespace="selfhosted.environment", version=1, value=harness.model_dump(mode="json")
        ),
    )
    try:
        agent = await agents.resolve(scope, name)
    except ProviderError as error:
        if error.category != "not_found":
            raise
        try:
            agent = await agents.create(
                scope, agent_spec, key=f"selfhosted-init:agent:{name}:{uuid4()}"
            )
        except ProviderError as conflict:
            if conflict.category != "conflict":
                raise
            agent = await agents.resolve(scope, name)
    agent = await agents.retrieve(scope, agent.ref)
    _matching(agent, agent_spec)
    try:
        environment = await environments.resolve(scope, environment_name)
    except ProviderError as error:
        if error.category != "not_found":
            raise
        try:
            environment = await environments.create(
                scope,
                environment_spec,
                key=f"selfhosted-init:environment:{environment_name}:{uuid4()}",
            )
        except ProviderError as conflict:
            if conflict.category != "conflict":
                raise
            environment = await environments.resolve(scope, environment_name)
    environment = await environments.retrieve(scope, environment.ref)
    _matching(environment, environment_spec)
    return agent.ref.id, environment.ref.id


def _matching(resource: Agent | Environment, spec: AgentSpec | EnvironmentSpec) -> None:
    current = resource.spec
    if (
        isinstance(current, EnvironmentSpec)
        and isinstance(spec, EnvironmentSpec)
        and current.native_config is not None
        and spec.native_config is not None
    ):
        current_harness = HarnessConfig.model_validate(dict(current.native_config.value))
        wanted_harness = HarnessConfig.model_validate(dict(spec.native_config.value))
        if current_harness == wanted_harness:
            current = current.model_copy(update={"native_config": spec.native_config})
    if resource.archived_at is not None or current != spec:
        raise ValueError(
            "A named resource already exists with different configuration or is archived"
        )


@selfhosted_app.command("init")
def selfhosted_init_command(
    tenant_id: Annotated[UUID, typer.Option("--tenant-id", help="Existing tenant UUID.")],
    harness_url: Annotated[str, typer.Option("--harness-url", help="Harness HTTP endpoint.")],
    database_url: Annotated[
        str,
        typer.Option(
            "--database-url",
            envvar=["DAIMON_DATABASE__URL", "DAIMON_DATABASE_URL"],
            show_envvar=True,
            help="Postgres connection URL.",
        ),
    ],
    model: Annotated[
        str, typer.Option("--model", help="Azure model deployment name.")
    ] = "gpt-6.1-sol",
    name: Annotated[str, typer.Option("--name")] = "daimon",
    environment_name: Annotated[str, typer.Option("--environment-name")] = "default",
    auth_mode: Annotated[
        str, typer.Option("--auth-mode", help="bearer_env or entra.")
    ] = "bearer_env",
    auth_ref: Annotated[
        str,
        typer.Option(
            "--auth-ref", help="DAIMON_SELFHOSTED__HARNESS_TOKEN_* variable or Entra audience."
        ),
    ] = "DAIMON_SELFHOSTED__HARNESS_TOKEN_PRIMARY",
    packages_note: Annotated[str | None, typer.Option("--packages-note")] = None,
    transport: Annotated[
        str, typer.Option("--transport", help="direct or foundry_invocations.")
    ] = "direct",
    agent_name: Annotated[
        str | None, typer.Option("--agent-name", help="Foundry hosted agent name.")
    ] = None,
    project_endpoint: Annotated[
        str | None, typer.Option("--project-endpoint", help="Foundry project endpoint.")
    ] = None,
    allow_insecure_http: Annotated[
        bool,
        typer.Option("--allow-insecure-http", help="Permit direct HTTP inside a private cluster."),
    ] = False,
    system_file: Annotated[
        Path | None, typer.Option("--system-file", exists=True, dir_okay=False)
    ] = None,
) -> None:
    """Create or reuse one agent and environment; stdout contains their IDs only."""
    try:
        harness = HarnessConfig.model_validate(
            {
                "harness_url": harness_url,
                "auth_mode": auth_mode,
                "auth_ref": auth_ref,
                "packages_note": packages_note,
                "transport": transport,
                "agent_name": agent_name,
                "project_endpoint": project_endpoint,
                "allow_insecure_http": allow_insecure_http,
            }
        )
    except ValidationError:
        raise typer.BadParameter(
            "Invalid harness configuration; use credential references only."
        ) from None

    async def run() -> tuple[str, str]:
        try:
            engine = build_engine(database_url)
        except Exception:
            # URL/dialect errors can contain isolated password fragments that
            # credential redactors cannot identify without the original DSN.
            raise typer.BadParameter(
                "database configuration invalid (scheme/host/port check)",
                param_hint="--database-url",
            ) from None
        try:
            return await initialize(
                async_sessionmaker(engine, expire_on_commit=False),
                tenant_id=tenant_id,
                name=name,
                model=model,
                system=system_file.read_text(encoding="utf-8") if system_file else None,
                environment_name=environment_name,
                harness=harness,
            )
        finally:
            await engine.dispose()

    try:
        agent_id, environment_id = asyncio.run(run())
    except typer.BadParameter:
        raise
    except (ProviderError, ValueError):
        raise typer.BadParameter(
            "Initialization refused; check resource names and existing configuration."
        ) from None
    except Exception:
        raise typer.BadParameter(
            "Initialization failed; check database access, migrations and the system file."
        ) from None
    typer.echo(agent_id)
    typer.echo(environment_id)
