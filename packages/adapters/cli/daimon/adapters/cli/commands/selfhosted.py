"""Initialize tenant-owned self-hosted resources using only Postgres."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import typer
from daimon.core.config import HarnessAuthBinding, SelfhostedModelPrice
from daimon.core.db import build_engine
from daimon.core.stores.selfhosted_resources import (
    SelfHostedAgents,
    SelfHostedEnvironments,
)
from daimon.core.turn.selfhosted_deployment import SelfHostedDeployment
from mux.contracts.config import ResolvedBackend
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ModelRef, Scope
from mux.contracts.resources import Agent, AgentSpec, Environment, EnvironmentSpec
from mux.drivers.selfhosted import HarnessConfig
from mux.errors import ProviderError
from mux.profiles.selfhosted import LOOP
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_MANIFEST_ENV = "DAIMON_TURN__PROVIDER_RUNTIME_FILE"

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


def channel_rows(
    *,
    tenant_id: UUID,
    model: str,
    agent_id: str,
    environment_id: str,
    account_id: str | None,
    channels: list[tuple[str, str]],
    infrastructure_usd: Decimal,
    agents: list[UUID] | None = None,
) -> list[dict[str, object]]:
    """One runtime-file row per channel or agent, bound to the default config digest.

    An agent row grants that agent's own choice of this model, in any channel.
    """
    digest = ResolvedBackend(
        backend=LOOP.provider, profile=LOOP.profile_id, model=model
    ).content_digest()
    return [
        SelfHostedDeployment(
            tenant_id=str(tenant_id),
            platform=platform,
            channel_id=channel_id,
            account_id=account,
            config_digest=digest,
            agent_id=agent_id,
            environment_id=environment_id,
            infrastructure_usd=infrastructure_usd,
            source_agent_id=source,
        ).model_dump(mode="json")
        for platform, channel_id, account, source in [
            *((platform, channel_id, account_id or "", None) for platform, channel_id in channels),
            *(("*", "*", "*", source) for source in agents or ()),
        ]
    ]


def merge_rows(
    existing: list[dict[str, object]], rows: list[dict[str, object]]
) -> list[dict[str, object]]:
    """Replace rows that name the same tenant, channel, account and agent; keep the rest."""
    keys = ("profile", "tenant_id", "platform", "channel_id", "account_id", "source_agent_id")
    replaced = {tuple(row[key] for key in keys) for row in rows}
    kept = [row for row in existing if tuple(row.get(key) for key in keys) not in replaced]
    return [*kept, *rows]


def env_lines(
    *,
    tenant_id: UUID,
    model: str,
    harness: HarnessConfig,
    input_usd: Decimal,
    output_usd: Decimal,
    cache_read_usd: Decimal,
    today: date,
) -> list[str]:
    """The settings an operator adds to the environment, one `NAME=value` per line."""
    price = SelfhostedModelPrice(
        effective_from=today, input=input_usd, output=output_usd, cache_read=cache_read_usd
    )
    lines = ["DAIMON_SELFHOSTED__ADMISSION_ENABLED=true"]
    parsed = urlsplit(harness.project_endpoint or harness.harness_url)
    if parsed.scheme == "https":
        binding = HarnessAuthBinding(
            auth_mode=harness.auth_mode,  # pyright: ignore[reportArgumentType]
            name=harness.auth_ref,
            origin=f"{parsed.scheme}://{parsed.netloc}",
            tenants=(tenant_id,),
        )
        lines.append(
            "DAIMON_SELFHOSTED__HARNESS_AUTH_BINDINGS="
            + json.dumps([binding.model_dump(mode="json")], separators=(",", ":"))
        )
    lines.append(
        "DAIMON_BILLING__SELFHOSTED__DEPLOYMENTS="
        + json.dumps({model: model}, separators=(",", ":"))
    )
    lines.append(
        "DAIMON_BILLING__SELFHOSTED__MODELS="
        + json.dumps({model: [price.model_dump(mode="json")]}, separators=(",", ":"))
    )
    return lines


def _write_rows(path: Path, rows: list[dict[str, object]]) -> None:
    # Atomic: serving services read this file live.
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.tmp-{os.getpid()}")
    tmp.write_text(json.dumps(rows, indent=2) + "\n")
    tmp.replace(path)


def _price(value: str, flag: str, *, allow_zero: bool = False) -> Decimal:
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise typer.BadParameter("Enter a number.", param_hint=flag) from None
    if not amount.is_finite() or amount < 0 or (amount == 0 and not allow_zero):
        raise typer.BadParameter("Enter a positive number.", param_hint=flag)
    return amount


def _channel(value: str) -> tuple[str, str]:
    platform, _, channel_id = value.partition(":")
    if not platform or not channel_id:
        raise typer.BadParameter("Use platform:channel-id.", param_hint="--channel")
    return platform, channel_id


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
    input_usd: Annotated[
        str, typer.Option("--input-usd", help="Verified input price, USD per million tokens.")
    ],
    output_usd: Annotated[
        str, typer.Option("--output-usd", help="Verified output price, USD per million tokens.")
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
    cache_read_usd: Annotated[
        str | None,
        typer.Option("--cache-read-usd", help="Cached input price; the input price if unset."),
    ] = None,
    infrastructure_usd: Annotated[
        str, typer.Option("--infrastructure-usd", help="Audited cost per request; 0 if none.")
    ] = "0",
    manifest: Annotated[
        Path | None,
        typer.Option("--manifest", help=f"Runtime file path; defaults to ${_MANIFEST_ENV}."),
    ] = None,
    channel: Annotated[
        list[str] | None,
        typer.Option("--channel", help="platform:channel-id to grant; repeat for more."),
    ] = None,
    account_id: Annotated[
        str | None, typer.Option("--account-id", help="Account the channels belong to.")
    ] = None,
    agent: Annotated[
        list[UUID] | None,
        typer.Option("--agent", help="Agent ID that may pick this model itself; repeat for more."),
    ] = None,
) -> None:
    """Create or reuse one agent and environment, grant channels or agents, print the settings.

    Stdout is the agent ID, the environment ID, then `NAME=value` settings lines.
    """
    manifest_env = os.environ.get(_MANIFEST_ENV)
    manifest_path = manifest or (Path(manifest_env) if manifest_env else None)
    if manifest_path is None:
        raise typer.BadParameter(
            f"Pass --manifest or set {_MANIFEST_ENV}.", param_hint="--manifest"
        )
    input_price = _price(input_usd, "--input-usd")
    output_price = _price(output_usd, "--output-usd")
    cache_price = (
        _price(cache_read_usd, "--cache-read-usd", allow_zero=True)
        if cache_read_usd is not None
        else input_price
    )
    infrastructure = _price(infrastructure_usd, "--infrastructure-usd", allow_zero=True)
    channels = [_channel(value) for value in channel or ()]
    if not channels and not agent:
        raise typer.BadParameter("Pass --channel or --agent.", param_hint="--channel")
    if channels and not account_id:
        raise typer.BadParameter("--channel needs --account-id.", param_hint="--account-id")
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
    existing: list[dict[str, object]] = []
    if manifest_path.exists():
        existing = list(json.loads(manifest_path.read_text()))
    _write_rows(
        manifest_path,
        merge_rows(
            existing,
            channel_rows(
                tenant_id=tenant_id,
                model=model,
                agent_id=agent_id,
                environment_id=environment_id,
                account_id=account_id,
                channels=channels,
                infrastructure_usd=infrastructure,
                agents=agent,
            ),
        ),
    )
    typer.echo(agent_id)
    typer.echo(environment_id)
    lines = env_lines(
        tenant_id=tenant_id,
        model=model,
        harness=harness,
        input_usd=input_price,
        output_usd=output_price,
        cache_read_usd=cache_price,
        today=date.today(),
    )
    for line in lines:
        typer.echo(line)
    if not any(line.startswith("DAIMON_SELFHOSTED__HARNESS_AUTH_BINDINGS=") for line in lines):
        typer.echo(
            "The harness origin is not HTTPS, so no auth binding line was printed.", err=True
        )
