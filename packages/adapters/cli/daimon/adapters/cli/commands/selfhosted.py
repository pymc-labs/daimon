"""Set up tenant-owned self-hosted resources and move them to another model."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated
from uuid import UUID, uuid4

import typer
from daimon.core.channel_backend import channel_ref, set_channel_backend
from daimon.core.db import build_engine
from daimon.core.errors import DaimonError
from daimon.core.stores import mux_state
from daimon.core.stores.access_policy import policy_write_transaction
from daimon.core.stores.selfhosted_resources import (
    SelfHostedAgents,
    SelfHostedEnvironments,
    list_live_agent_facts,
    lock_resources,
)
from daimon.core.thread_release import caller_slot, release_pending
from daimon.core.turn.selfhosted_deployment import selfhosted_config
from mux.contracts.config import resolve_default
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ChannelRef, ModelRef, Revision, Scope
from mux.contracts.resources import Agent, AgentPatch, AgentSpec, Environment, EnvironmentSpec
from mux.drivers.selfhosted import HarnessConfig
from mux.errors import BindingConflict, ProviderError
from mux.profiles.selfhosted import LOOP
from mux.state.store import binding_slot, released_successor
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

if TYPE_CHECKING:
    from daimon.adapters.cli.runtime import CliRuntime
    from daimon.core.turn.run import RunOutcome

selfhosted_app = typer.Typer(help="Manage tenant-owned self-hosted agents and environments.")

DatabaseUrl = Annotated[
    str,
    typer.Option(
        "--database-url",
        envvar=["DAIMON_DATABASE__URL", "DAIMON_DATABASE_URL"],
        show_envvar=True,
        help="Postgres connection URL.",
    ),
]


def _operator(tenant_id: UUID) -> Scope:
    """The deployment operator's scope: no personal account, the tenant's records only."""
    return Scope(
        tenant_id=str(tenant_id),
        account_id="operator",
        principal_id="cli",
        authorization_id="cli-deployment-operator",
    )


@asynccontextmanager
async def _database(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
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
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


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
    scope = _operator(tenant_id)
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
    database_url: DatabaseUrl,
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
        async with _database(database_url) as sessionmaker:
            return await initialize(
                sessionmaker,
                tenant_id=tenant_id,
                name=name,
                model=model,
                system=system_file.read_text(encoding="utf-8") if system_file else None,
                environment_name=environment_name,
                harness=harness,
            )

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


class ModelChangeRefused(DaimonError):
    """A model change that cannot go ahead as its dry run showed."""


@dataclass(frozen=True)
class ModelChange:
    """Move a tenant's agents, conversations and threads from `from_model` to `model`.

    `conversations` names the conversations to move; empty means every one on `from_model`.
    While agents move it must name every one, as each conversation answers with them.
    """

    tenant_id: UUID
    from_model: str
    model: str
    conversations: tuple[str, ...] = ()


class _Planned(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class PlannedThread(_Planned):
    """A caller's thread answered on the old model, at the binding generation seen."""

    thread_id: str
    account_id: UUID
    generation: int


class PlannedConversation(_Planned):
    """A conversation on the old model, at the revision seen, and its threads there."""

    platform: str
    channel_id: str
    revision: int
    threads: tuple[PlannedThread, ...]


class PlannedAgent(_Planned):
    """An agent on the old model, at the revision seen."""

    id: str
    name: str
    revision: int


class ModelChangePlan(_Planned):
    """What a dry run found on the old model: exactly what `apply_model_change` writes."""

    tenant_id: UUID
    from_model: str
    model: str
    agents: tuple[PlannedAgent, ...]
    conversations: tuple[PlannedConversation, ...]


async def _threads_on(
    session: AsyncSession, channel: ChannelRef, digest: str
) -> tuple[PlannedThread, ...]:
    """The channel's caller threads bound to one of its revisions configured as `digest`,
    except those whose next turn already selects afresh."""
    threads: list[PlannedThread] = []
    for binding in await mux_state.channel_bindings(session, channel):
        if binding.legacy_account_id is None:
            continue  # A shared thread: only a caller's own thread is released.
        bound = await mux_state.get_config_revision(session, channel, binding.config_revision)
        if (
            bound is not None
            and bound.digest == digest
            and not await release_pending(session, binding_slot(binding))
        ):
            threads.append(
                PlannedThread(
                    thread_id=binding.thread.thread_id,
                    account_id=UUID(binding.legacy_account_id),
                    generation=binding.generation,
                )
            )
    return tuple(threads)


async def _plan(session: AsyncSession, change: ModelChange) -> ModelChangePlan:
    """Conversations on the old model, and ones already on the new model with
    threads still on the old one (as a `register` run before the change leaves them)."""
    old, new = (
        resolve_default(selfhosted_config(model)).content_digest()
        for model in (change.from_model, change.model)
    )
    revisions = await mux_state.channels_configured_as(
        session, tenant_id=change.tenant_id, digests=(old, new)
    )
    conversations: list[PlannedConversation] = []
    for revision in revisions:
        threads = await _threads_on(session, revision.channel, old)
        if revision.digest == old or threads:
            conversations.append(
                PlannedConversation(
                    platform=revision.channel.platform,
                    channel_id=revision.channel.channel_id,
                    revision=revision.local,
                    threads=threads,
                )
            )
    agents = tuple(
        PlannedAgent(id=agent.id, name=agent.name, revision=agent.version)
        for agent in sorted(
            await list_live_agent_facts(session, tenant_id=change.tenant_id),
            key=lambda agent: agent.id,
        )
        if agent.model.id == change.from_model
    )
    if change.conversations:
        listed = set(change.conversations)
        if missing := sorted(listed - {c.channel_id for c in conversations}):
            raise ModelChangeRefused(f"not on {change.from_model}: {', '.join(missing)}")
        # Every conversation shares the tenant's agents: one left on the old model
        # while they move could not answer.
        if agents and (left := [c.channel_id for c in conversations if c.channel_id not in listed]):
            raise ModelChangeRefused(
                f"the agents move too, so list every conversation on {change.from_model}; "
                f"missing: {', '.join(left)}"
            )
        conversations = [c for c in conversations if c.channel_id in listed]
    return ModelChangePlan(
        tenant_id=change.tenant_id,
        from_model=change.from_model,
        model=change.model,
        agents=agents,
        conversations=tuple(conversations),
    )


async def plan_model_change(
    sessionmaker: async_sessionmaker[AsyncSession], change: ModelChange
) -> ModelChangePlan:
    """The dry run: what `change` would move, read without writing anything."""
    async with sessionmaker() as session:
        return await _plan(session, change)


async def apply_model_change(
    sessionmaker: async_sessionmaker[AsyncSession], change: ModelChange, plan: ModelChangePlan
) -> None:
    """Write `plan` if a dry run of `change` finds it still; refuse otherwise.

    One transaction holds the tenant's policy and resource locks: each conversation
    gets a new revision on the new model, and each planned thread is released at the
    generation seen, through the binding compare-and-swap, so its next message starts
    a new session there. A thread whose binding moved meanwhile refuses the whole
    change. Then each agent moves at the revision seen. Configuration history is only
    appended to, and a cleared conversation is never planned.
    """
    config = selfhosted_config(change.model)
    stale = f"what is on {change.from_model} changed since the dry run; nothing was written"
    async with policy_write_transaction(sessionmaker, tenant_id=change.tenant_id) as session:
        await lock_resources(session, change.tenant_id)
        if await _plan(session, change) != plan:
            raise ModelChangeRefused(stale)
        for conversation in plan.conversations:
            channel = channel_ref(change.tenant_id, conversation.platform, conversation.channel_id)
            await set_channel_backend(session, channel, config)
            for thread in conversation.threads:
                slot = caller_slot(
                    tenant_id=change.tenant_id,
                    platform=conversation.platform,
                    channel_id=conversation.channel_id,
                    thread_id=thread.thread_id,
                    account_id=thread.account_id,
                )
                bound = await mux_state.get_binding(session, slot)
                if bound is None:
                    raise ModelChangeRefused(stale)
                try:
                    await mux_state.put_binding(
                        session, released_successor(bound), expected_generation=thread.generation
                    )
                except BindingConflict:
                    raise ModelChangeRefused(stale) from None
    agents, scope = SelfHostedAgents(sessionmaker), _operator(change.tenant_id)
    for planned in plan.agents:
        try:
            agent = await agents.resolve(scope, planned.id)
            await agents.update(
                scope,
                agent.ref,
                AgentPatch(model=agent.spec.model.model_copy(update={"id": change.model})),
                expected=Revision(local=planned.revision),
                key=f"selfhosted-change-model:{planned.id}:{planned.revision}",
            )
        except ProviderError:
            raise ModelChangeRefused(
                f"agent {planned.name} changed since the dry run; the conversations moved, "
                "so run the dry run again to move the agent"
            ) from None


@selfhosted_app.command("change-model")
def selfhosted_change_model_command(
    tenant_id: Annotated[UUID, typer.Option("--tenant-id", help="Existing tenant UUID.")],
    from_model: Annotated[
        str, typer.Option("--from-model", help="The model the conversations are on now.")
    ],
    model: Annotated[str, typer.Option("--model", help="Azure model deployment to move to.")],
    database_url: DatabaseUrl,
    conversations: Annotated[
        list[str] | None,
        typer.Option(
            "--conversation",
            help="A channel or chat ID to move; repeat it. Default, and needed while agents "
            "move: every one on --from-model.",
        ),
    ] = None,
    apply: Annotated[
        typer.FileText | None,
        typer.Option("--apply", help="Write this plan, as the dry run printed it; - reads stdin."),
    ] = None,
) -> None:
    """Move a tenant's agents, conversations and threads to another model.

    Without --apply, print the plan as JSON and write nothing. With it, write the
    plan only if a dry run would print it now.
    """
    if not from_model.strip() or not model.strip() or from_model == model:
        raise typer.BadParameter("name two different models", param_hint="--model")
    change = ModelChange(
        tenant_id=tenant_id,
        from_model=from_model,
        model=model,
        conversations=tuple(conversations or ()),
    )
    try:
        planned = ModelChangePlan.model_validate_json(apply.read()) if apply else None
    except ValidationError:
        raise typer.BadParameter("not a plan a dry run printed", param_hint="--apply") from None

    async def run() -> ModelChangePlan:
        async with _database(database_url) as sessionmaker:
            if planned is None:
                return await plan_model_change(sessionmaker, change)
            await apply_model_change(sessionmaker, change, planned)
            return planned

    try:
        plan = asyncio.run(run())
    except ModelChangeRefused as refused:
        typer.echo(f"Refused: {refused}.", err=True)
        raise typer.Exit(code=1) from None
    if planned is None:
        typer.echo(plan.model_dump_json(indent=2))
        typer.echo("Dry run: nothing was written. Pass this plan to --apply to write it.", err=True)
        return
    threads = sum(len(conversation.threads) for conversation in plan.conversations)
    typer.echo(
        f"Moved to {model}: {len(plan.agents)} agent(s), {len(plan.conversations)} "
        f"conversation(s); {threads} thread(s) start a new session at their next message."
    )


async def operator_turn(
    rt: CliRuntime, *, tenant_id: UUID, channel: str, thread: str, caller: str, message: str
) -> RunOutcome:
    """Use the production channel chokepoints, including their billing recorder."""
    import sys
    from datetime import UTC, datetime

    from daimon.adapters.cli.turn_lifecycle import NdjsonLifecycle
    from daimon.core.anthropic_guard import guard_selfhosted_channel
    from daimon.core.billing import load_billing_config
    from daimon.core.channel_backend import channel_ref, current_backend
    from daimon.core.turn import admit, bind_session, run_prepared_turn
    from daimon.core.turn.deps import build_turn_deps
    from daimon.core.turn_hold import RequesterOrg

    async with rt.sessionmaker() as db:
        revision = await current_backend(db, channel_ref(tenant_id, "cli", channel))
    if revision is None or revision.backend != "selfhosted":
        raise ValueError("Channel must explicitly select selfhosted")
    deps = build_turn_deps(
        rt.settings,
        rt.anthropic,
        rt.sessionmaker,
        deployment_default=rt.deployment_default,
        resolver_cache=rt.resolver_cache,
        billing_config=load_billing_config(),
    )
    async with guard_selfhosted_channel(
        rt.sessionmaker,
        tenant_id=tenant_id,
        platform="cli",
        channel_id=channel,
        thread_id=thread,
        caller_id=caller,
        force=True,
    ):
        admitted = await admit(
            deps,
            tenant_id=tenant_id,
            platform="cli",
            external_user_id=caller,
            channel_id=channel,
            thread_id=thread,
            now=datetime.now(UTC),
            requester_org=RequesterOrg.HOST,
            origin="chat",
        )
        if admitted.backend_revision is None or admitted.backend_revision.backend != "selfhosted":
            raise ValueError("Admission must select selfhosted")
        prepared = await bind_session(
            deps,
            admitted,
            tenant_id=tenant_id,
            platform="cli",
            external_user_id=caller,
            thread_id=thread,
            session_account_id=admitted.account_id,
            reuse_existing=True,
        )
        lifecycle = NdjsonLifecycle(sys.stdout, prepared.ma_session_id, str(uuid4()))

        async def reseed() -> str:
            return message

        return await run_prepared_turn(
            deps,
            prepared,
            tenant_id=tenant_id,
            platform="cli",
            thread_id=thread,
            external_user_id=caller,
            user_message=message,
            lifecycle=lifecycle,
            cancel=asyncio.Event(),
            reseed_user_message=reseed,
            recovery_lifecycle=lambda cancel: lifecycle,
            attended=False,
        )


@selfhosted_app.command("turn")
def selfhosted_turn_command(
    tenant_id: Annotated[UUID, typer.Option("--tenant-id")],
    message: Annotated[str, typer.Option("--message")],
    channel: Annotated[str, typer.Option("--channel")] = "selfhosted-operator",
    thread: Annotated[str, typer.Option("--thread")] = "default",
    caller: Annotated[str, typer.Option("--caller")] = "operator",
) -> None:
    """Run a billed, admitted turn; reuse --thread for persistent session files."""
    from daimon.adapters.cli.runtime import build_runtime
    from daimon.core.config import load_settings

    async def run() -> RunOutcome:
        async with build_runtime(load_settings()) as rt:
            return await operator_turn(
                rt,
                tenant_id=tenant_id,
                channel=channel,
                thread=thread,
                caller=caller,
                message=message,
            )

    outcome = asyncio.run(run())
    if outcome.state.error is not None:
        raise typer.Exit(code=1)
