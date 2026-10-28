"""Repository-scoped persistent memory, authorized by the executing turn."""

from __future__ import annotations

import uuid
from typing import Literal, TypedDict

import httpx
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._channel_policy import load_channel_policy
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools.setup_target import require_turn_origin
from daimon.core.github_app_session import REQUESTER_CACHE, effective_app_grant_view
from daimon.core.github_requester_access import linked_permissions
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.permissions import readers_limited_at
from daimon.core.repo_memory import MemoryOrigin, new_entry
from daimon.core.stores.github_access import AuthorizedRepo
from daimon.core.stores.github_connect import requester_linked_github_user_id
from daimon.core.stores.repo_memory import delete_entry, get_entry, list_entries, save_entry
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError


class _RepoKey(TypedDict):
    tenant_id: uuid.UUID
    repo_id: int


async def resolve_repo(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    origin_context_id: str,
    repo: str | None = None,
    write: bool = False,
) -> AuthorizedRepo:
    if not runtime.settings.repo_memory.enabled:
        raise ToolError("Native repository memory is disabled.")
    if auth.routine_id is not None or auth.execution_id is not None or auth.is_external_agent:
        raise ToolError("Repository memory tools need an interactive conversation.")
    origin = await require_turn_origin(runtime, auth, origin_context_id)
    policy = await load_channel_policy(runtime, auth)
    is_dm = origin.thread_id.startswith("dm:")
    facts = MemoryOrigin(
        interactive=origin.attended and origin.human_invocation is True,
        restricted=readers_limited_at(
            policy, channel_id=origin.parent_channel_id, thread_id=origin.thread_id
        ),
        held=auth.hold is not None,
        foreign=auth.client or origin.sender_foreign or origin.shared_channel,
        read_only=origin.memory_read_only is not False or (is_dm and policy.dm_memory_read_only),
    )
    try:
        facts.require(write=write)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    agent_id = derive_agent_uuid(tenant_id=auth.tenant_id, ma_agent_id=origin.responder_ma_agent_id)
    rows, _, _, _ = await effective_app_grant_view(
        runtime.session_factory, tenant_id=auth.tenant_id, agent_id=agent_id, fernet=runtime.fernet
    )
    working = next((r for g, r, _ in rows if g.is_working_repo), None)
    if repo is None or (
        working is not None and repo.casefold() == working.repo_full_name.casefold()
    ):
        if working is None:
            raise ToolError("No readable working repository is bound to this agent.")
        return working
    target = next((r for _, r, _ in rows if r.repo_full_name.casefold() == repo.casefold()), None)
    if target is None:
        raise ToolError("That repository is outside this agent's active GitHub grants.")
    async with runtime.session_factory() as session:
        user_id = await requester_linked_github_user_id(session, account_id=auth.account_id)
    config = runtime.settings.github_app
    if (
        user_id is None
        or runtime.fernet is None
        or not config.client_id
        or not config.client_secret
    ):
        raise ToolError("Reading another repository requires your linked GitHub read access.")
    async with httpx.AsyncClient() as client:
        permissions = await linked_permissions(
            runtime.session_factory,
            client,
            user_id=user_id,
            installation_id=target.installation_id,
            fernet=runtime.fernet,
            client_id=config.client_id,
            client_secret=config.client_secret.get_secret_value(),
            cache=REQUESTER_CACHE,
        )
    if permissions.get(target.repo_id, "none") not in ("read", "write"):
        raise ToolError("Your linked GitHub account cannot read that repository.")
    return target


async def repo_memory_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    operation: Literal["list", "view", "save", "delete"],
    origin_context_id: str,
    repo: str | None = None,
    name: str = "",
    title: str = "",
    body: str = "",
    kind: str = "reference",
    scope: str = "repo",
) -> str:
    target = await resolve_repo(
        runtime,
        auth,
        origin_context_id=origin_context_id,
        repo=repo,
        write=operation in ("save", "delete"),
    )
    key: _RepoKey = {"tenant_id": auth.tenant_id, "repo_id": target.repo_id}
    async with runtime.session_factory.begin() as session:
        if operation == "list":
            entries = await list_entries(session, **key, account_id=auth.account_id)
            lines = [f"Repo memories for {target.repo_full_name}:"]
            lines.extend(f"{e.name}: {e.title} [{e.kind}, {e.scope}]" for e in entries)
            return "\n".join(lines) if entries else f"No repo memories for {target.repo_full_name}."
        if operation == "view":
            entry = await get_entry(session, **key, name=name)
            if entry is not None and (
                entry.scope == "repo" or entry.created_by_account_id == auth.account_id
            ):
                return entry.body
        elif operation == "delete":
            if await delete_entry(session, **key, name=name, account_id=auth.account_id):
                return f"Deleted repo memory '{name}' from {target.repo_full_name}."
        else:
            try:
                entry = new_entry(
                    **key,
                    repo_name=target.repo_full_name,
                    account_id=auth.account_id,
                    title=title,
                    body=body,
                    kind=kind,
                    scope=scope,
                )
                previous = await save_entry(session, entry=entry)
            except ValueError as exc:
                raise ToolError(str(exc)) from exc
            if previous is not None and previous != entry.title:
                return (
                    f"Overwrote '{entry.name}', previously titled '{previous}'. "
                    "Its old content is gone; use a distinct title for a different memory."
                )
            return f"Saved repo memory '{entry.name}' for {target.repo_full_name}."
    raise ToolError(f"Repo memory '{name}' not found for {target.repo_full_name}.")


def register_repo_memory_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    if not runtime.settings.repo_memory.enabled:
        return

    @mcp.tool()
    async def repo_memory_list(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, origin_context_id: str, repo: str | None = None
    ) -> str:
        """List repository memories: shared entries and your own personal directives.

        Omit repo for the agent's bound working repository. Reading another
        granted repo requires your linked GitHub account's read access.
        Use this reply's origin_context_id. Routine callers cannot use this tool.
        """
        return await repo_memory_impl(
            runtime,
            await _auth(ctx),
            operation="list",
            origin_context_id=origin_context_id,
            repo=repo,
        )

    @mcp.tool()
    async def repo_memory_view(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, name: str, origin_context_id: str, repo: str | None = None
    ) -> str:
        """Read a repository memory's full body by its listed slug.

        Omit repo for the working repository; another repo requires personal
        GitHub read access. Use this reply's origin_context_id.
        """
        return await repo_memory_impl(
            runtime,
            await _auth(ctx),
            operation="view",
            origin_context_id=origin_context_id,
            repo=repo,
            name=name,
        )

    @mcp.tool()
    async def repo_memory_save(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        title: str,
        body: str,
        origin_context_id: str,
        kind: Literal["reference", "directive"] = "reference",
        scope: Literal["repo", "user"] = "repo",
    ) -> str:
        """Remember information for the bound working repository across sessions.

        Save only at the person's request. References appear in a catalog;
        directives are standing rules included in turns, within a size budget.
        Personal preferences use kind=directive, scope=user. Other people cannot
        see them. Shared entries can be updated by anyone using this repo.
        Titles become slugs (64 characters); re-saving a slug replaces content
        and cannot change scope. Restricted conversations and routines refuse.
        Use this reply's origin_context_id. Do not put repo memories in MA files.
        """
        return await repo_memory_impl(
            runtime,
            await _auth(ctx),
            operation="save",
            origin_context_id=origin_context_id,
            title=title,
            body=body,
            kind=kind,
            scope=scope,
        )

    @mcp.tool()
    async def repo_memory_delete(ctx: Context, name: str, origin_context_id: str) -> str:  # pyright: ignore[reportUnusedFunction]
        """Forget a memory by slug only when the person explicitly asks.

        Deletes from the bound working repo. Anyone using the repo can delete
        shared entries; only the creator can delete a personal directive.
        Restricted conversations and routines refuse. Use this reply's origin_context_id.
        """
        return await repo_memory_impl(
            runtime,
            await _auth(ctx),
            operation="delete",
            origin_context_id=origin_context_id,
            name=name,
        )
