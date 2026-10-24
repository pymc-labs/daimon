"""Start approved jobs, manage them as owner/admin, and read under source policy."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from daimon.adapters.mcp.resource_ports import mcp_scope, retrieve_session
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools import _session_gate
from daimon.adapters.mcp.tools._channel_policy import load_channel_policy
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools._long_job_access import LongJobCaller, long_job_access
from daimon.adapters.mcp.tools.setup_target import require_turn_origin
from daimon.core.defaults.ma_index import find_environment_by_daimon_tag
from daimon.core.defaults.metadata import MA_METADATA_KEY_PRIVATE_DM
from daimon.core.errors import StoreError
from daimon.core.long_job_models import LongJob, LongJobCancelReply, LongJobRedacted
from daimon.core.long_jobs import require_admission
from daimon.core.permissions import limiting_ids_at
from daimon.core.scope import ScopeContext
from daimon.core.session_seal import origin_stamp, seal_ids
from daimon.core.stores import long_jobs as store
from daimon.core.stores.scoped_config_read import resolve
from daimon.core.stores.thread_sessions import get_live_thread_session
from daimon.core.tool_safety import DAIMON_SERVER_NAME
from daimon.core.turn.io import LegacyTurnIO
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError


def _same_usd(value: object, requested: float | None) -> bool:
    if value is None or requested is None:
        return value is None and requested is None
    try:
        return Decimal(str(value)) == Decimal(str(requested))
    except InvalidOperation:
        return False


def register_long_job_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool
    async def start_long_job(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        command: str,
        title: str,
        origin_context_id: str,
        deadline_minutes: int | None = None,
        max_usd: float | None = None,
    ) -> LongJob:
        """Run an exact human-approved shell command in a fresh isolated Managed Agents worker.

        Requires an attended owner turn and approval card. The parent thread goes idle;
        completion wakes it to read and report. Worker waits are bounded to 280 seconds.
        Tenant/agent admin policy must enable jobs. Omitted limits use effective policy;
        a deadline or dollar cap above its maximum is refused. No connectors, skills,
        shared memory or credentials are mounted. Jobs are billed to the owner.
        """
        auth = await _auth(ctx)
        origin = await require_turn_origin(runtime, auth, origin_context_id)
        if origin.platform != "discord":
            raise ToolError("long jobs report back only in Discord threads for now")
        if (
            not origin.attended
            or not auth.platform_user_id
            or not command.strip()
            or len(command) > 8000
            or not title.strip()
            or len(title) > 200
        ):
            raise ToolError(
                "The owner must be here to approve a command (1–8000 characters) and title (1–200 "
                "characters) are required"
            )
        gap = await _session_gate.session_card_gap(
            runtime, auth, origin, tool_name="start_long_job"
        )
        if gap is not None:
            raise ToolError("Approval of this exact command is required in this conversation")
        async with runtime.session_factory.begin() as db:
            live = await get_live_thread_session(
                db,
                tenant_id=auth.tenant_id,
                platform=origin.platform,
                thread_id=origin.thread_id,
                account_id=auth.account_id,
            )
            config = await resolve(
                db,
                context=ScopeContext(tenant_id=auth.tenant_id, channel_id=origin.parent_channel_id),
                default=runtime.deployment_default,
            )
            policy = await store.get_policy(
                db,
                tenant_id=auth.tenant_id,
                agent_id=origin.responder_ma_agent_id,
                ceilings=runtime.settings.long_jobs,
            )
        if live is None:
            raise ToolError("The original conversation is unavailable")
        parent = await retrieve_session(runtime.client, live.ma_session_id, scope=mcp_scope(auth))
        # Bind idempotency to provider evidence, never a caller-invented key.
        events = await LegacyTurnIO(runtime.client, parent.id, scope=mcp_scope(auth)).replay()
        approvals = {
            e.tool_use_id
            for e in events
            if e.type == "user.tool_confirmation" and e.result == "allow"
        }
        matches = [
            e
            for e in events
            if e.type == "agent.mcp_tool_use"
            and e.mcp_server_name == DAIMON_SERVER_NAME
            and e.name == "start_long_job"
            and e.input.get("command") == command
            and e.input.get("title") == title
            and e.input.get("origin_context_id") == origin_context_id
            and e.input.get("deadline_minutes") == deadline_minutes
            and _same_usd(e.input.get("max_usd"), max_usd)
            and e.id in approvals
        ]
        if not matches:
            raise ToolError(
                "The provider has no approved call matching this command and its exact limits"
            )
        environment = await find_environment_by_daimon_tag(
            runtime.client, tenant_id=auth.tenant_id, name=config.environment_name or "default"
        )
        if (
            environment is None
            or parent.agent.id != origin.responder_ma_agent_id
            or parent.environment_id != environment.id
        ):
            raise ToolError("Approved agent or environment is unavailable")
        privacy_policy = await load_channel_policy(runtime, auth)
        provenance = origin_stamp(
            channel_id=origin.parent_channel_id,
            thread_id=origin.thread_id,
            seal=seal_ids(parent.metadata)
            | limiting_ids_at(
                privacy_policy, channel_id=origin.parent_channel_id, thread_id=origin.thread_id
            ),
        )
        if MA_METADATA_KEY_PRIVATE_DM in parent.metadata:
            provenance[MA_METADATA_KEY_PRIVATE_DM] = parent.metadata[MA_METADATA_KEY_PRIVATE_DM]
        job = LongJob(
            id=uuid.uuid4(),
            tenant_id=auth.tenant_id,
            owner_account_id=auth.account_id,
            owner_external_user_id=auth.platform_user_id,
            agent_id=parent.agent.id,
            agent_name=origin.responder_name,
            agent_version=parent.agent.version,
            environment_id=environment.id,
            environment_updated_at=datetime.fromisoformat(environment.updated_at),
            platform=origin.platform,
            parent_channel_id=origin.parent_channel_id,
            thread_id=origin.thread_id,
            parent_session_id=parent.id,
            tool_use_id=matches[-1].id,
            source_metadata=provenance,
            command=command,
            title=title,
            deadline_minutes=deadline_minutes
            if deadline_minutes is not None
            else policy.default_deadline_minutes,
            max_usd=Decimal(str(max_usd)) if max_usd is not None else policy.max_usd,
            state="queued",
            version=0,
            created_at=datetime.now(UTC),
        )
        async with runtime.session_factory.begin() as db:
            existing = await store.get_registration(db, job)
        if existing is not None:
            return existing
        try:
            async with runtime.session_factory.begin() as db:
                registered = await store.register(db, job, ceilings=runtime.settings.long_jobs)
                if registered.id == job.id:
                    await require_admission(runtime.session_factory, job, session=db)
                return registered
        except StoreError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool
    async def get_long_job(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, job_id: uuid.UUID, origin_context_id: str | None = None
    ) -> LongJob | LongJobRedacted:
        """Read an owner/admin job; return source-restricted metadata when content is private.

        Supply origin_context_id from a turn inside a restricted source channel.
        Admin authority permits management, never a bypass of source privacy.
        """
        caller = await LongJobCaller.load(runtime, await _auth(ctx), origin_context_id)
        async with runtime.session_factory.begin() as db:
            job = await store.get(
                db,
                job_id,
                tenant_id=caller.auth.tenant_id,
                owner_account_id=caller.auth.account_id,
                admin=caller.is_admin,
            )
        access = await long_job_access(caller, job)
        if not access.act:
            raise ToolError("Long job not found")
        assert job is not None
        return access.view(job)

    @mcp.tool
    async def list_long_jobs(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, origin_context_id: str | None = None
    ) -> list[LongJob | LongJobRedacted]:
        """List your latest 100 jobs; current human tenant admins may list all tenant jobs.

        Source-restricted jobs contain only id, state, owner and scheduling metadata.
        Supply origin_context_id when reading inside a restricted source.
        """
        caller = await LongJobCaller.load(runtime, await _auth(ctx), origin_context_id)
        async with runtime.session_factory.begin() as db:
            jobs = await store.list_jobs(
                db,
                tenant_id=caller.auth.tenant_id,
                owner_account_id=caller.auth.account_id,
                admin=caller.is_admin,
            )
        result: list[LongJob | LongJobRedacted] = []
        for job in jobs:
            access = await long_job_access(caller, job)
            if access.act:
                result.append(access.view(job))
        return result

    @mcp.tool
    async def cancel_long_job(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, job_id: uuid.UUID, origin_context_id: str | None = None
    ) -> LongJobCancelReply:
        """Request interrupt and archive as owner or current human tenant admin.

        Cancellation requires management authority only: source privacy never blocks
        stopping a job. Reply and audit contain no command, summary or other content.
        The scheduler makes cancellation terminal after interrupt and archive.
        """
        caller = await LongJobCaller.load(runtime, await _auth(ctx), origin_context_id)
        async with runtime.session_factory.begin() as db:
            job = await store.get(
                db,
                job_id,
                tenant_id=caller.auth.tenant_id,
                owner_account_id=caller.auth.account_id,
                admin=caller.is_admin,
            )
            access = await long_job_access(caller, job)
            if not access.act:
                raise ToolError("Long job not found")
            cancelled = await store.request_cancel(
                db,
                job_id,
                tenant_id=caller.auth.tenant_id,
                owner_account_id=caller.auth.account_id,
                admin=caller.is_admin,
                requested_by=caller.auth.account_id,
            )
        assert cancelled is not None
        return LongJobCancelReply.model_validate(cancelled)
