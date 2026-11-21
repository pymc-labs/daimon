"""An agent step's turn in its lane: a headless turn as the run's run-as person, metered.

The spend is that person's, as for any turn of theirs: usage rows carry their id, so
their allowance counts it, and preflight gates each step on that same allowance.
"""

from __future__ import annotations

import functools
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from anthropic import AsyncAnthropic
from daimon.core.config import Settings, get_repo_memory_settings
from daimon.core.defaults.metadata import BUILTIN_AGENT_NAME
from daimon.core.defaults.provisioning import reconcile_tenant_defaults
from daimon.core.github_credentials import build_multifernet
from daimon.core.headless_runner import StepTurn, run_turn
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.ma_resolver import ResolverCache, resolve_agent, resolve_environment
from daimon.core.pricing import MODEL_PRICING
from daimon.core.scope import DeploymentDefault, ScopeContext
from daimon.core.spend_gate import spend_person_of_account
from daimon.core.stores.scoped_config_read import resolve
from daimon.core.tool_safety import ToolSafetyPolicy
from daimon.core.usage_recording import record_turn_usage
from daimon.core.workflows.lanes import agent_of
from daimon.core.workflows.preflight import spend_user_id
from daimon.core.workflows.runs import StepWork
from daimon.core.workflows.spec import AgentNode, CodeNode
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@dataclass(frozen=True)
class HeadlessLaneTurn:
    client: AsyncAnthropic
    sm: async_sessionmaker[AsyncSession]
    settings: Settings
    deployment_default: DeploymentDefault
    resolver_cache: ResolverCache

    async def __call__(
        self,
        work: StepWork,
        *,
        message: str,
        session_id: str | None,
        step: StepTurn,
        policy: ToolSafetyPolicy,
    ) -> None:
        node, run, settings = work.node, work.run, self.settings
        assert isinstance(node, AgentNode), "a lane runs agent steps"
        channel = work.place.workflow.home_channel_id if work.place else None
        agent_id, environment_id = await self.targets(work)
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
        await run_turn(
            anthropic=self.client,
            agent_id=agent_id,
            environment_id=environment_id,
            trigger_message=message,
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
            model_override=node.model,
            feature_settings=settings,
            session_id=session_id,
            step=step,
        )

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
