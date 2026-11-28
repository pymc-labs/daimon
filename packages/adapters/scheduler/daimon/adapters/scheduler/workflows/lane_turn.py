"""An agent step's turn in its lane: a headless turn as the run's run-as person, metered.

`admit` decides the step's place as a routine fire's (`fire.admit.admit_place`); a place on
OpenAI or Gemini runs its turns through `ProviderLaneTurn`. The spend
is that person's, as for any turn of theirs: usage rows carry their id, so their allowance
counts it, and preflight gates each step on that same allowance.
"""

from __future__ import annotations

import functools
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Final

import structlog
from anthropic import AsyncAnthropic
from daimon.core.authz import build_agent_ref
from daimon.core.channel_changes import ChannelChangeRefused
from daimon.core.config import Settings, get_repo_memory_settings
from daimon.core.defaults.metadata import BUILTIN_AGENT_NAME
from daimon.core.defaults.provisioning import reconcile_tenant_defaults
from daimon.core.discord_membership import open_discord_dm
from daimon.core.dispatch_timeout import DispatchTimeoutSettings
from daimon.core.fire.admit import (
    AdmitReads,
    Refused,
    Waiting,
    admit_place,
    checks_ran_agent,
    ran_agent_refusal,
)
from daimon.core.github_credentials import build_multifernet
from daimon.core.headless_runner import StepTurn, run_turn
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.ma_resolver import ResolverCache, resolve_agent, resolve_environment
from daimon.core.membership import MembershipResolver
from daimon.core.mux_backend import resource_scope
from daimon.core.mux_compat import retrieve_agent
from daimon.core.pricing import MODEL_PRICING
from daimon.core.provider_routine import (
    STEP_SHELL_NEEDS_GRANT,
    ProviderRoutineRun,
    admit_provider_routine,
    refusal_text,
    run_provider_routine,
    workflow_scope_thread,
)
from daimon.core.routine_delivery import posted_places
from daimon.core.routine_files import stage_files
from daimon.core.routine_sandbox import RoutineSandbox, pin_at
from daimon.core.routines import mounts_personal_memory
from daimon.core.scope import DeploymentDefault, ScopeContext
from daimon.core.spend_gate import spend_person_of_account
from daimon.core.stores import identity, tenants, workflow_runs
from daimon.core.stores.domain import SandboxPin, pin_json
from daimon.core.stores.lead_intake import workflow_execution
from daimon.core.stores.scoped_config_read import resolve
from daimon.core.tool_safety import ToolSafetyPolicy
from daimon.core.turn.admission import Admission, AdmissionDenied
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.state import TurnState
from daimon.core.usage_recording import record_turn_usage
from daimon.core.workflows import engine
from daimon.core.workflows.agent_step import LaneReply
from daimon.core.workflows.lanes import agent_of
from daimon.core.workflows.place import StepPlace, step_place, workflow_subject
from daimon.core.workflows.preflight import spend_user_id
from daimon.core.workflows.runs import StepWork, lane_of
from daimon.core.workflows.spec import AgentNode, CodeNode, Deliver, FinishNode
from mux.contracts.config import DEFAULT_BACKEND, ConfigRevision, ResolvedBackend
from pydantic import TypeAdapter
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)
_PIN: Final = TypeAdapter(SandboxPin)
#: The `pins` key a `builtins` grant keeps what it covers under.
SANDBOX_PIN: Final = "sandbox"
_NO_PIN: Final = "Can't read the step's agent or its environment, so its built-in tools can't run."
_NO_TURN: Final = "The step's agent can't start a turn on its model provider here."
#: A deliberate difference from Managed Agents: a provider turn a lost worker left can't be
#: reattached, so its attempt fails `retryable` and the next one starts afresh.
NO_REATTACH: Final = "A provider turn can't be reattached after a lost worker."


@dataclass(frozen=True)
class ProviderLaneTurn:
    """An agent step's turn on OpenAI or Gemini: admitted as an unattended provider routine
    at the step's place, in its lane's own scope (`workflow_scope_thread`), which later steps
    of the lane continue. Step tools come through Daimon MCP, whose credential carries the
    run; the relay and the MCP server apply the step's decider (`running_step_decider`)."""

    deps: TurnDeps
    admit: Callable[..., Awaitable[Admission]] = admit_provider_routine
    run: Callable[..., Awaitable[ProviderRoutineRun]] = run_provider_routine

    async def __call__(
        self,
        work: StepWork,
        *,
        place: StepPlace,
        message: str,
        session_id: str | None,
        step: StepTurn,
    ) -> LaneReply | engine.Failed:
        node, run, sm = work.node, work.run, self.deps.sessionmaker
        subject, admitted = place.subject, place.admitted
        assert isinstance(node, AgentNode) and subject is not None and admitted is not None
        assert subject.created_by_user_id is not None, "admission refused an unlinked run-as"
        if step.resume:
            raise RuntimeError(NO_REATTACH)
        owner = subject.created_by_user_id
        try:
            admission = await self.admit(
                self.deps,
                # A private step is billed to and configured by its workflow's home.
                replace(subject, channel_id=subject.channel_id or subject.home_channel_id),
                platform=admitted.platform,
                owner=owner,
                subject=admitted.invoker,
                sandbox=place.sandbox,
                hold=admitted.hold,
                origin_place=admitted.origin_place,
                refusal=STEP_SHELL_NEEDS_GRANT,
            )
        except AdmissionDenied as error:
            return engine.Failed("gate", refusal_text(error) or _NO_TURN, error.reason)
        ran = admission.agent
        if checks_ran_agent(admitted) and (
            refused := ran_agent_refusal(
                admitted, subject, build_agent_ref(ran.name, ran.metadata, subject.agent_name)
            )
        ):
            return engine.Failed("gate", refused.error or refused.reason, refused.reason)
        admission = _with_step(admission, node.model, step)
        lane = lane_of(work)
        lifetime = DispatchTimeoutSettings().dispatch_timeout_s
        async with workflow_execution(sm, run.tenant_id, run.id, lane=lane, lifetime_s=lifetime):
            ran_turn = await self.run(
                self.deps,
                admission,
                routine_id=subject.id,
                tenant_id=run.tenant_id,
                platform=admitted.platform,
                owner=owner,
                prompt=message,
                scope_id=workflow_scope_thread(run.id, lane),
                reuse_existing=session_id is not None,
                on_session=step.on_session,
                on_render=step.on_render,
            )
        files, _ = await stage_files(sm, ran_turn.outputs, tenant_id=run.tenant_id)
        posted = await posted_places(
            sm,
            ran_turn.state,
            tenant_id=run.tenant_id,
            home=subject.home_channel_id,
            run_as=owner,
        )
        return LaneReply(ran_turn.text, posted, tuple(f.model_dump() for f in files))


def _with_step(admission: Admission, model: str | None, step: StepTurn) -> Admission:
    """The admission with the step's model (preflight checked it is the provider's) and its
    pinned skills."""
    skills = (*admission.channel_skills, *step.skills)
    revision = admission.backend_revision
    if model is None or revision is None or model == revision.model:
        return replace(admission, channel_skills=skills)
    fields = revision.model_dump(include=set(ResolvedBackend.model_fields)) | {"model": model}
    picked = ConfigRevision.create(
        revision.channel, revision.local, ResolvedBackend.model_validate(fields)
    )
    backend = admission.backend
    return replace(
        admission,
        channel_skills=skills,
        backend_revision=picked,
        backend=backend.model_copy(update={"model": model, "config_digest": picked.digest})
        if backend is not None
        else None,
    )


@dataclass(frozen=True)
class HeadlessLaneTurn:
    client: AsyncAnthropic
    sm: async_sessionmaker[AsyncSession]
    settings: Settings
    deployment_default: DeploymentDefault
    resolver_cache: ResolverCache

    membership_resolver: MembershipResolver | None = None
    #: Runs the steps whose place is on another provider; None runs every step here.
    provider: ProviderLaneTurn | None = None

    async def admit(self, work: StepWork) -> StepPlace | engine.Failed:
        """The step's place as the run-as person, decided as a routine fire's is.

        A run-as person with no chat identity here can't be checked, so the step fails; a
        step with no `at` is checked at home. A test run works there as the tester and
        never posts itself.
        """
        node, run = work.node, work.run
        assert isinstance(node, AgentNode), "a lane runs agent steps"
        workflow = work.place.workflow if work.place else None
        home = workflow.home_channel_id if workflow else None
        where = step_place(node.at, home=home, origin=run.origin)
        agent_id, environment_id = await self.targets(work)
        bare = StepPlace(where, agent_id=agent_id, environment_id=environment_id)
        person = await self._run_as(work)
        if workflow is None or person is None:
            return engine.Failed(
                "gate", "The person this workflow runs as isn't linked here.", "run_as_unlinked"
            )
        platform, run_as = person
        agent = agent_of(work.spec, node) or self.deployment_default.agent_name
        subject = workflow_subject(
            workflow,
            node,
            home=home,
            run_as=run_as,
            agent_name=agent or BUILTIN_AGENT_NAME,
            agent_id=agent_id,
            platform=platform,
            origin=run.origin,
        )
        reads = AdmitReads(self.sm, self.settings, self.membership_resolver)
        verdict = await admit_place(subject, reads)
        if not isinstance(verdict, Refused | Waiting) and checks_ran_agent(verdict):
            scope = resource_scope(tenant_id=str(run.tenant_id))
            ran = await retrieve_agent(self.client, agent_id, scope=scope, records=self.sm)
            ref = build_agent_ref(ran.name, ran.metadata, subject.agent_name)
            verdict = ran_agent_refusal(verdict, subject, ref) or verdict
        if isinstance(verdict, Waiting):
            return engine.Failed("retryable", verdict.reason, verdict.reason)
        if isinstance(verdict, Refused):
            reason = "run_as_lapsed" if verdict.lapsed else verdict.reason
            return engine.Failed("gate", verdict.error or verdict.reason, reason)
        may_post = run.mode == "live" and where is not None and verdict.direct_post == "allowed"
        admitted = replace(
            bare,
            may_post=may_post,
            subject=subject,
            admitted=verdict,
            dm_channel=await self._dm_channel(work, platform, run_as),
        )
        try:
            return replace(admitted, sandbox=await self._sandbox(work, admitted))
        except ChannelChangeRefused:
            return engine.Failed("gate", _NO_PIN, "builtins_unavailable")

    async def _run_as(self, work: StepWork) -> tuple[str, str] | None:
        """The workspace's platform and the run-as person's id on it, if they have one."""
        account_id = work.run.run_as_account_id
        if account_id is None:
            return None
        async with self.sm() as s:
            tenant = await tenants.get_tenant(s, work.run.tenant_id)
            linked = await identity.list_platform_principals_for_account(s, account_id=account_id)
        ids = sorted(
            p.external_id
            for p in linked
            if tenant is not None and p.tenant_id == tenant.id and p.platform == tenant.platform
        )
        return None if tenant is None or not ids else (tenant.platform, ids[0])

    async def _dm_channel(self, work: StepWork, platform: str, run_as: str) -> str | None:
        """The run-as person's Discord DM for a private step of a run that posts to them, so
        its calls sign room `dm` as a DM routine's do; None elsewhere or when it can't open."""
        node, discord = work.node, self.settings.discord
        posts = (
            d.to
            for n in work.spec.nodes.values()
            if (d := n.deliver if isinstance(n, FinishNode) else n) is not None
            and isinstance(d, Deliver)
        )
        if (
            not isinstance(node, AgentNode)
            or node.at is not None
            or platform != "discord"
            or discord is None
            or "run_as" not in posts
        ):
            return None
        try:
            return await open_discord_dm(discord.bot_token.get_secret_value(), run_as)
        except Exception:  # signs no DM claims; the step still runs
            log.warning("workflow.step_dm_unopened", run_id=str(work.run.id))
            return None

    async def _sandbox(self, work: StepWork, place: StepPlace) -> RoutineSandbox | None:
        """A live step's built-ins hold while the run matches what their grant pinned: what
        the agent and the place's environment were at the grant's first run. Raises
        `ChannelChangeRefused` when that first pin can't be read."""
        node, run = work.node, work.run
        if run.mode != "live" or not isinstance(node, AgentNode) or node.builtins in (None, "none"):
            return None
        async with self.sm() as s:
            grants = await workflow_runs.live_grants(
                s, workflow_id=run.workflow_id, version=run.version
            )
        grant = next(
            (g for g in grants if g.kind == "builtins" and g.node_id == work.step.node_id), None
        )
        if grant is None:
            return None
        if (stored := (grant.pins or {}).get(SANDBOX_PIN)) is not None:
            pin = _PIN.validate_python(stored)
        else:
            admitted = place.admitted
            pin = await pin_at(
                self.client,
                self.sm,
                tenant_id=run.tenant_id,
                agent_id=place.agent_id,
                channel_id=admitted.channel_id if admitted else None,
                provider=(admitted.native if admitted else None) or DEFAULT_BACKEND,
                default=self.deployment_default,
            )
            async with self.sm.begin() as s:
                await workflow_runs.pin_grant(s, grant.id, SANDBOX_PIN, pin_json(pin))

        async def drifted() -> None:
            async with self.sm.begin() as s:
                await workflow_runs.revoke_grant(s, grant.id, "sandbox_drift")

        return RoutineSandbox(pin, on_drift=drifted)

    async def __call__(
        self,
        work: StepWork,
        *,
        place: StepPlace,
        message: str,
        session_id: str | None,
        step: StepTurn,
        policy: ToolSafetyPolicy,
    ) -> LaneReply | engine.Failed:
        node, run, settings = work.node, work.run, self.settings
        assert isinstance(node, AgentNode), "a lane runs agent steps"
        if place.admitted is not None and place.admitted.native is not None:
            if self.provider is None:
                return engine.Failed("gate", _NO_TURN, "backend_unsupported")
            return await self.provider(
                work, place=place, message=message, session_id=session_id, step=step
            )
        channel = work.place.workflow.home_channel_id if work.place else None
        agent_id, environment_id = place.agent_id, place.environment_id
        spender = None
        if (account_id := run.run_as_account_id) is not None:
            async with self.sm() as s:
                person = await spend_person_of_account(
                    s, tenant_id=run.tenant_id, account_id=account_id
                )
            spender = spend_user_id(person, account_id)

        def usage(session: str, model_id: str) -> Callable[..., Awaitable[object]]:
            return functools.partial(
                record_turn_usage,
                sessionmaker=self.sm,
                platform_user_id=spender,
                managed_session_id=session,
                model_id=model_id,
                tenant_id=run.tenant_id,
                markup=settings.billing.markup,
                pricing=MODEL_PRICING.get(model_id),
                channel_id=channel,
                agent_id=agent_id,
                workflow_run_id=run.id,
            )

        keys = tuple(k.get_secret_value() for k in settings.crypto.keys)
        key = settings.github_app.private_key
        subject, admitted = place.subject, place.admitted
        states: list[TurnState] = []
        lifetime = DispatchTimeoutSettings().dispatch_timeout_s
        async with workflow_execution(
            self.sm, run.tenant_id, run.id, lane=lane_of(work), lifetime_s=lifetime
        ):
            text = await run_turn(
                anthropic=self.client,
                agent_id=agent_id,
                environment_id=environment_id,
                trigger_message=message,
                on_state=states.append,
                mcp_settings=settings.mcp,
                repo_memory=get_repo_memory_settings(settings),
                account_id=run.run_as_account_id,
                usage_record_factory=usage,
                tenant_id=run.tenant_id,
                agent_uuid=derive_agent_uuid(tenant_id=run.tenant_id, ma_agent_id=agent_id),
                session_factory=self.sm,
                fernet=build_multifernet(keys) if keys else None,
                github_app_id=settings.github_app.app_id,
                github_app_private_key=key.get_secret_value() if key is not None else None,
                agent_github_app=settings.github_app,
                github_mcp_router=settings.github.mcp_router,
                tool_safety=policy,
                budget_channel_id=channel,
                origin_place=admitted.origin_place if admitted else None,
                model_override=node.model,
                sandbox=place.sandbox,
                platform=admitted.platform if admitted else None,
                hold=admitted.hold if admitted else None,
                personal_memory=subject is not None
                and node.memory == "personal"
                and mounts_personal_memory(subject),
                feature_settings=settings,
                runs_as_user=subject.created_by_user_id if subject else None,
                membership_resolver=self.membership_resolver,
                session_id=session_id,
                step=step,
                step_dm_channel=place.dm_channel,
            )
        posted = (
            await posted_places(
                self.sm,
                states[-1],
                tenant_id=run.tenant_id,
                home=channel,
                run_as=subject.created_by_user_id if subject else None,
            )
            if states
            else frozenset[str]()
        )
        return LaneReply(text, posted)

    async def agent_id(
        self,
        tenant_id: uuid.UUID,
        agent: str | None,
        heal: Callable[[], Awaitable[object]] | None = None,
    ) -> str:
        """A workspace agent's id by its spec key (the default agent for None)."""
        public_url = str(self.settings.mcp.public_url) if self.settings.mcp.public_url else None

        def reconcile() -> Awaitable[object]:
            return reconcile_tenant_defaults(
                self.client,
                self.sm,
                self.settings.defaults_root,
                tenant_id=tenant_id,
                public_url=public_url,
            )

        return await resolve_agent(
            self.client,
            tenant_id=tenant_id,
            daimon_tag=agent or self.deployment_default.agent_name or BUILTIN_AGENT_NAME,
            cached_id=None,
            apply_callable=heal or reconcile,
            cache=self.resolver_cache,
        )

    async def targets(self, work: StepWork) -> tuple[str, str]:
        """The step's agent and environment ids: the node's own, else the workspace's."""
        node, run, settings = work.node, work.run, self.settings
        channel = work.place.workflow.home_channel_id if work.place else None
        public_url = str(settings.mcp.public_url) if settings.mcp.public_url else None

        def heal() -> Awaitable[object]:
            return reconcile_tenant_defaults(
                self.client,
                self.sm,
                settings.defaults_root,
                tenant_id=run.tenant_id,
                public_url=public_url,
            )

        agent = agent_of(work.spec, node) if isinstance(node, AgentNode) else None
        agent_id = await self.agent_id(run.tenant_id, agent, heal)
        async with self.sm() as s:
            scoped = await resolve(
                s,
                context=ScopeContext(tenant_id=run.tenant_id, channel_id=channel),
                default=self.deployment_default,
            )
        environment = node.environment if isinstance(node, CodeNode) else None
        environment_id = await resolve_environment(
            self.client,
            tenant_id=run.tenant_id,
            daimon_tag=environment or scoped.environment_name or "default",
            cached_id=None,
            apply_callable=heal,
            cache=self.resolver_cache,
        )
        return agent_id, environment_id
