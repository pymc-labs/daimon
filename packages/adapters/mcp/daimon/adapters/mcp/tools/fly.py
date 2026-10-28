"""Native owner-only Fly sessions bound to verified, attended conversation origins."""

from __future__ import annotations

import uuid

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.resource_ports import mcp_scope, retrieve_session
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools import _session_gate
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools._session_access import source_metadata_readable
from daimon.adapters.mcp.tools.setup_target import require_turn_origin
from daimon.adapters.mcp.tools.watches import fresh_admin
from daimon.core.compute_fly import FlyComputeProvider
from daimon.core.compute_models import ComputeSession, ComputeStatus
from daimon.core.compute_provider import ComputeProviderError
from daimon.core.compute_sessions import ComputeCaller, ComputeSessions
from daimon.core.errors import StoreError
from daimon.core.session_seal import origin_stamp
from daimon.core.stores.domain import TurnOriginRow
from daimon.core.stores.thread_sessions import get_live_thread_session
from daimon.core.tool_safety import DAIMON_SERVER_NAME
from daimon.core.turn.io import LegacyTurnIO
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import JsonValue


async def _caller(
    runtime: McpRuntime, ctx: Context, origin_id: str, *, stop: bool = False
) -> tuple[AuthIdentity, ComputeCaller]:
    auth = await _auth(ctx)
    if (
        auth.client
        or auth.agent_id is not None
        or auth.routine_id is not None
        or auth.execution_id is not None
        or auth.chat_agent_id is None
        or not auth.chat_session_id
    ):
        raise ToolError("Use a conversation where the machine owner is present.")
    origin = await require_turn_origin(runtime, auth, origin_id)
    if (
        origin.sender_foreign
        or not origin.attended
        or origin.human_invocation is not True
        or not auth.platform_user_id
    ):
        raise ToolError("The machine owner must be here in an active conversation.")
    return auth, ComputeCaller(
        tenant_id=auth.tenant_id,
        account_id=auth.account_id,
        origin=origin,
        chat_session_id=auth.chat_session_id,
        external_user_id=auth.platform_user_id,
        admin=await fresh_admin(runtime, auth) if stop else False,
    )


async def _approved(
    runtime: McpRuntime,
    auth: AuthIdentity,
    origin: TurnOriginRow,
    tool: str,
    arguments: dict[str, JsonValue],
    defaults: dict[str, JsonValue],
) -> tuple[str, dict[str, str]]:
    if await _session_gate.session_card_gap(runtime, auth, origin, tool_name=tool) is not None:
        raise ToolError("Approve this action in the owner's conversation first.")
    async with runtime.session_factory() as db:
        live = await get_live_thread_session(
            db,
            tenant_id=auth.tenant_id,
            platform=origin.platform,
            thread_id=origin.thread_id,
            account_id=auth.account_id,
        )
    if live is None:
        raise ToolError("The original conversation is unavailable.")
    parent = await retrieve_session(runtime.client, live.ma_session_id, scope=mcp_scope(auth))
    if parent.agent.id != origin.responder_ma_agent_id:
        raise ToolError("The conversation's agent changed. Try again there.")
    events = await LegacyTurnIO(runtime.client, parent.id, scope=mcp_scope(auth)).replay()
    approvals = {
        e.tool_use_id for e in events if e.type == "user.tool_confirmation" and e.result == "allow"
    }
    matches = [
        e
        for e in events
        if e.type == "agent.mcp_tool_use"
        and e.mcp_server_name == DAIMON_SERVER_NAME
        and e.name == tool
        and {**defaults, **e.input} == arguments
        and e.id in approvals
    ]
    if not matches:
        raise ToolError("Approval of this exact action and its limits is required.")
    return parent.id + ":" + matches[-1].id, {
        **parent.metadata,
        **origin_stamp(channel_id=origin.parent_channel_id, thread_id=origin.thread_id),
    }


async def _readable(
    runtime: McpRuntime, auth: AuthIdentity, origin_id: str, session: ComputeSession
) -> None:
    if not await source_metadata_readable(
        runtime, auth, session.source_metadata, origin_context_id=origin_id
    ):
        raise ToolError(
            "You can no longer read the source conversation. Ask an admin to stop the machine."
        )


def register_fly_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    service = ComputeSessions(
        runtime.session_factory,
        runtime.settings.compute,
        runtime.compute_provider or FlyComputeProvider(runtime.settings.compute),
    )

    @mcp.tool
    async def fly_launch_session(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        template: str,
        origin_context_id: str,
        region: str = "iad",
        ttl_hours: int | None = None,
    ) -> ComputeSession:
        """Launch your own temporary Fly machine from an operator image profile.

        Requires approval in an attended conversation. List profiles for fixed CPU/RAM,
        regions and image before choosing. No shared credentials are mounted. Sessions
        are deleted after their idle lease; export files before stopping or expiry.
        """
        auth, caller = await _caller(runtime, ctx, origin_context_id)
        key, metadata = await _approved(
            runtime,
            auth,
            caller.origin,
            "fly_launch_session",
            {
                "template": template,
                "origin_context_id": origin_context_id,
                "region": region,
                "ttl_hours": ttl_hours,
            },
            {"region": "iad", "ttl_hours": None},
        )
        if not await source_metadata_readable(
            runtime, auth, metadata, origin_context_id=origin_context_id
        ):
            raise ToolError("The source conversation is unavailable.")
        try:
            return await service.launch(
                caller,
                profile=template,
                region=region,
                ttl_hours=ttl_hours,
                approval_key=key,
                source_metadata=metadata,
            )
        except (StoreError, ComputeProviderError) as exc:
            raise ToolError(str(exc)) from None

    @mcp.tool
    async def fly_extend_session(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, session_id: uuid.UUID, hours: int, origin_context_id: str
    ) -> ComputeSession:
        """Set your session's expiry to this many hours from now, after owner approval.

        Use the original attended conversation. Hours cannot exceed the operator TTL.
        This does not change the idle window used by subsequent command/file writes.
        """
        auth, caller = await _caller(runtime, ctx, origin_context_id)
        try:
            await _readable(runtime, auth, origin_context_id, await service.get(caller, session_id))
            key, _ = await _approved(
                runtime,
                auth,
                caller.origin,
                "fly_extend_session",
                {
                    "session_id": str(session_id),
                    "hours": hours,
                    "origin_context_id": origin_context_id,
                },
                {},
            )
            return await service.extend(caller, session_id, hours=hours, approval_key=key)
        except (StoreError, ComputeProviderError) as exc:
            raise ToolError(str(exc)) from None

    @mcp.tool
    async def fly_stop_session(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, session_id: uuid.UUID, origin_context_id: str
    ) -> dict[str, str]:
        """Permanently delete a session and its files. Export needed files first.

        The owner may stop it in the original conversation. A current tenant admin
        may emergency-stop it without access to commands, files or other contents.
        Failed provider deletion remains pending for automatic cleanup.
        """
        _, caller = await _caller(runtime, ctx, origin_context_id, stop=True)
        try:
            result = await service.stop(caller, session_id)
            return {"session_id": str(result.id), "state": result.state}
        except (StoreError, ComputeProviderError) as exc:
            raise ToolError(str(exc)) from None

    async def call(
        ctx: Context,
        session_id: uuid.UUID,
        origin_id: str,
        tool: str,
        arguments: dict[str, JsonValue],
    ) -> JsonValue:
        auth, caller = await _caller(runtime, ctx, origin_id)
        try:
            await _readable(runtime, auth, origin_id, await service.get(caller, session_id))
            return await service.call(caller, session_id, tool=tool, arguments=arguments)
        except (StoreError, ComputeProviderError) as exc:
            raise ToolError(str(exc)) from None

    @mcp.tool
    async def fly_run_bash(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        session_id: uuid.UUID,
        command: str,
        origin_context_id: str,
        timeout: int = 40000,
    ) -> JsonValue:
        """Run bash inline on your session; return exit code and stdout.

        timeout is milliseconds, 1–40000. Longer/background work is not supported
        yet. Do not retry an uncertain response automatically: inspect files first.
        Commands and file writes renew the idle lease. Reads do not renew it.
        """
        if not command.strip() or len(command) > 8000 or not 1 <= timeout <= 40000:
            raise ToolError(
                "Use a command of 1–8000 characters and timeout of 1–40000 milliseconds."
            )
        return await call(
            ctx,
            session_id,
            origin_context_id,
            "run_bash",
            {"command": command, "timeout_s": max(1, (timeout + 999) // 1000)},
        )

    @mcp.tool
    async def fly_write_file(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, session_id: uuid.UUID, path: str, content: str, origin_context_id: str
    ) -> JsonValue:
        """Write UTF-8 text on your session and renew its idle lease. Creates parent directories."""
        if not path.startswith("/") or "\x00" in path or len(content.encode()) > 4 * 1024 * 1024:
            raise ToolError("Use an absolute file path and at most 4 MiB of text.")
        return await call(
            ctx, session_id, origin_context_id, "write_file", {"path": path, "content": content}
        )

    @mcp.tool
    async def fly_read_file(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        session_id: uuid.UUID,
        path: str,
        origin_context_id: str,
        offset: int = 0,
        limit: int = 65536,
    ) -> JsonValue:
        """Read a file from your session in byte chunks. This does not extend its lease."""
        if (
            not path.startswith("/")
            or "\x00" in path
            or offset < 0
            or not 1 <= limit <= 4 * 1024 * 1024
        ):
            raise ToolError(
                "Use an absolute path, nonnegative offset and a limit from 1 byte to 4 MiB."
            )
        return await call(
            ctx,
            session_id,
            origin_context_id,
            "read_file",
            {"path": path, "offset": offset, "limit": limit},
        )

    @mcp.tool
    async def fly_list_files(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, session_id: uuid.UUID, path: str, origin_context_id: str, pattern: str = "*"
    ) -> JsonValue:
        """List paths and sizes on your session. This does not extend its lease."""
        if not path.startswith("/") or "\x00" in path:
            raise ToolError("Use an absolute directory path.")
        return await call(
            ctx, session_id, origin_context_id, "list_files", {"path": path, "pattern": pattern}
        )

    @mcp.tool
    async def fly_list_sessions(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, origin_context_id: str
    ) -> list[ComputeSession]:
        """List your live sessions in this conversation. Other people's sessions are private."""
        auth, caller = await _caller(runtime, ctx, origin_context_id)
        try:
            rows = await service.list(caller)
            return [
                row
                for row in rows
                if await source_metadata_readable(
                    runtime, auth, row.source_metadata, origin_context_id=origin_context_id
                )
            ]
        except StoreError as exc:
            raise ToolError(str(exc)) from None

    @mcp.tool
    async def fly_get_session_status(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, session_id: uuid.UUID, origin_context_id: str
    ) -> ComputeStatus:
        """Read your session's machine state, capacity and expiry without renewing it."""
        auth, caller = await _caller(runtime, ctx, origin_context_id)
        try:
            session = await service.get(caller, session_id)
            await _readable(runtime, auth, origin_context_id, session)
            return await service.status(caller, session_id)
        except (StoreError, ComputeProviderError) as exc:
            raise ToolError(str(exc)) from None

    @mcp.tool
    async def fly_list_templates(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, origin_context_id: str
    ) -> dict[str, JsonValue]:
        """List operator image profiles, fixed CPU/RAM and allowed regions."""
        await _caller(runtime, ctx, origin_context_id)
        return {
            name: profile.model_dump(mode="json")
            for name, profile in runtime.settings.compute.profiles.items()
        }
