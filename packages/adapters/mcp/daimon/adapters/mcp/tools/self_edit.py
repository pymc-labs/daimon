"""MCP tools for an agent to edit its own ``agent_files`` and manage its
``agent_repo_binding`` from inside an MA turn.

Identity is read server-side from the JWT claims via ``_auth(ctx)``.
Cross-agent isolation is enforced by composite-PK at the store layer
(``(tenant_id, agent_id, key)`` for ``agent_files``;
``(tenant_id, agent_id)`` for ``agent_repo_binding``).

``register_self_edit_tools(mcp, runtime)`` wires the ``@mcp.tool`` closures.
Plan 02 added 4 file tools; Plan 03 adds 3 repo-binding tools; Plan 04 will
wire this registrar into ``create_mcp_app``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

import anthropic
import structlog
from anthropic import AsyncAnthropic
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import (
    _auth,  # pyright: ignore[reportPrivateUsage]
    _require_admin,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.broker import dispatch_mint_token
from daimon.core.broker.errors import NoBindingError, ProviderConfigError
from daimon.core.errors import StoreError
from daimon.core.stores.agent_files import (
    delete_agent_file,
    get_agent_file,
    list_agent_files,
    put_agent_file,
)
from daimon.core.stores.agent_repo_binding import (
    clear_binding,
    get_binding,
    set_binding,
)
from daimon.core.stores.domain import AgentFileRow, AgentRepoBindingRow
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict

logger = structlog.get_logger()


class AgentRepoBindingPublic(BaseModel):
    """Agent-visible projection of ``AgentRepoBindingRow``.

    Omits ``ma_secret_ref`` per D-17 — the vault URI never reaches the agent.
    The agent only sees the public binding shape (repo + branch + timestamps).
    """

    model_config = ConfigDict(frozen=True)

    tenant_id: uuid.UUID
    agent_id: uuid.UUID
    repo_url: str
    default_branch: str
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: AgentRepoBindingRow) -> AgentRepoBindingPublic:
        return cls(
            tenant_id=row.tenant_id,
            agent_id=row.agent_id,
            repo_url=row.repo_url,
            default_branch=row.default_branch,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


def _require_agent_id(auth: AuthIdentity) -> uuid.UUID:
    """Return ``auth.agent_id`` or raise ``ToolError`` if absent (D-20)."""
    if auth.agent_id is None:
        raise ToolError("agent_id missing — token was not minted for an agent session")
    return auth.agent_id


async def _vault_id_for_account(client: AsyncAnthropic, account_id: uuid.UUID) -> str:
    """Return the per-account daimon-mcp vault id (mirrors tools/vault.py)."""
    display_name = f"daimon-mcp:{account_id}"
    matching = [v async for v in client.beta.vaults.list() if v.display_name == display_name]
    if not matching:
        raise ToolError(
            "no MCP vault found for this account — run a session first to bootstrap the vault"
        )
    return min(matching, key=lambda v: v.created_at).id


async def _self_write_file_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    key: str,
    content: str,
) -> AgentFileRow:
    _require_admin(auth)
    agent_id = _require_agent_id(auth)
    try:
        async with runtime.session_factory.begin() as session:
            row = await put_agent_file(
                session,
                tenant_id=auth.tenant_id,
                agent_id=agent_id,
                key=key,
                content=content,
            )
    except StoreError as e:
        logger.warning(
            "self_write_file outcome=store_error agent=%s key=%s",
            agent_id,
            key,
        )
        raise ToolError(str(e)) from e
    logger.info("self_write_file outcome=success agent=%s key=%s", agent_id, key)
    return row


async def _self_read_file_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    key: str,
) -> AgentFileRow | None:
    agent_id = _require_agent_id(auth)
    async with runtime.session_factory() as session:
        row = await get_agent_file(
            session,
            tenant_id=auth.tenant_id,
            agent_id=agent_id,
            key=key,
        )
    logger.info(
        "self_read_file outcome=%s agent=%s key=%s",
        "hit" if row is not None else "miss",
        agent_id,
        key,
    )
    return row


async def _self_list_files_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
) -> list[AgentFileRow]:
    agent_id = _require_agent_id(auth)
    async with runtime.session_factory() as session:
        rows = await list_agent_files(
            session,
            tenant_id=auth.tenant_id,
            agent_id=agent_id,
        )
    logger.info("self_list_files outcome=success agent=%s count=%d", agent_id, len(rows))
    return rows


async def _self_delete_file_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    key: str,
) -> dict[str, object]:
    _require_admin(auth)
    agent_id = _require_agent_id(auth)
    # delete_agent_file is silently idempotent at the store layer (Pitfall 2).
    async with runtime.session_factory.begin() as session:
        await delete_agent_file(
            session,
            tenant_id=auth.tenant_id,
            agent_id=agent_id,
            key=key,
        )
    logger.info("self_delete_file outcome=success agent=%s key=%s", agent_id, key)
    return {"deleted": True, "key": key}


async def _set_repo_binding_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    repo_url: str,
    default_branch: str,
    service: Literal["github"] = "github",
) -> AgentRepoBindingPublic:
    """Bind the calling agent to a git repo.

    Order of operations (D-08): mint PAT → vault credential create → DB row
    write → best-effort old vault credential delete. Vault upload happens
    BEFORE the DB write so a vault failure leaves no binding row.
    """
    _require_admin(auth)
    agent_id = _require_agent_id(auth)

    # 1. Mint plaintext PAT via the broker.
    try:
        token = await dispatch_mint_token(
            service=service,
            account_id=auth.account_id,
            agent_id=agent_id,
            sessionmaker=runtime.session_factory,
            settings=runtime.settings,
        )
    except NoBindingError as e:
        logger.warning(
            "set_repo_binding outcome=no_binding service=%s agent=%s",
            service,
            agent_id,
        )
        raise ToolError(
            "no GitHub credential for this account — run /agent-setup → "
            "Repo+Auth and connect a github account"
        ) from e
    except ProviderConfigError as e:
        logger.warning(
            "set_repo_binding outcome=provider_config_error service=%s",
            service,
        )
        raise ToolError(str(e)) from e

    # 2. Discover per-account vault.
    vault_id = await _vault_id_for_account(runtime.client, auth.account_id)

    # 3. Capture old ref (separate read session) so we can delete after success.
    async with runtime.session_factory() as session:
        old = await get_binding(
            session,
            tenant_id=auth.tenant_id,
            agent_id=agent_id,
        )
    old_ref = old.ma_secret_ref if old is not None else None

    # 4. Upload new vault credential. D-08: this MUST succeed before any DB write.
    try:
        new_cred = await runtime.client.beta.vaults.credentials.create(
            vault_id=vault_id,
            auth={
                "type": "static_bearer",
                "mcp_server_url": "https://github.com",  # Plan 01 probe-validated placeholder
                "token": token,
            },
            metadata={
                "service": service,
                "agent_id": str(agent_id),
                "repo_url": repo_url,
            },
        )
    except anthropic.APIError as e:
        # Do NOT format `e` into the message — APIError stringification can
        # include response body fragments.
        logger.warning(
            "set_repo_binding outcome=vault_upload_failed service=%s agent=%s",
            service,
            agent_id,
        )
        raise ToolError("vault upload failed") from e

    # 5. Write binding row with NEW ref. D-08 ordering: row only after upload OK.
    #    Symmetric BL-01 guard: if the DB write fails after vault.create succeeded,
    #    best-effort delete the freshly-minted credential before re-raising so we
    #    don't leak an orphan vault cred on retries.
    try:
        async with runtime.session_factory.begin() as session:
            row = await set_binding(
                session,
                tenant_id=auth.tenant_id,
                agent_id=agent_id,
                repo_url=repo_url,
                default_branch=default_branch,
                ma_secret_ref=new_cred.id,
            )
    except Exception:
        try:
            await runtime.client.beta.vaults.credentials.delete(
                new_cred.id,
                vault_id=vault_id,
            )
        except anthropic.APIError:
            logger.warning(
                "set_repo_binding outcome=orphan_new_vault_cred agent=%s cred=%s",
                agent_id,
                new_cred.id,
            )
        raise

    # 6. Best-effort delete old vault cred AFTER DB commit (D-08).
    if old_ref is not None and old_ref != new_cred.id:
        try:
            await runtime.client.beta.vaults.credentials.delete(
                old_ref,
                vault_id=vault_id,
            )
        except anthropic.APIError:
            logger.warning(
                "set_repo_binding outcome=orphan_old_vault_cred agent=%s",
                agent_id,
            )
            # Orphan acceptable per D-08; new credential is already live.

    logger.info(
        "set_repo_binding outcome=success service=%s account=%s agent=%s",
        service,
        auth.account_id,
        agent_id,
    )
    return AgentRepoBindingPublic.from_row(row)


async def _get_repo_binding_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
) -> AgentRepoBindingPublic | None:
    agent_id = _require_agent_id(auth)
    async with runtime.session_factory() as session:
        row = await get_binding(
            session,
            tenant_id=auth.tenant_id,
            agent_id=agent_id,
        )
    logger.info(
        "get_repo_binding outcome=%s agent=%s",
        "hit" if row is not None else "miss",
        agent_id,
    )
    if row is None:
        return None
    return AgentRepoBindingPublic.from_row(row)


async def _clear_repo_binding_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
) -> dict[str, bool]:
    """Remove the binding. Idempotent (D-11) and tolerant of vault delete failure (D-09)."""
    _require_admin(auth)
    agent_id = _require_agent_id(auth)

    # Read the binding to capture the vault ref for best-effort cleanup.
    async with runtime.session_factory() as session:
        existing = await get_binding(
            session,
            tenant_id=auth.tenant_id,
            agent_id=agent_id,
        )

    # D-11 idempotency: enforced by structure (pre-check), not by swallowing
    # StoreError. If no binding exists, return immediately — no vault cleanup,
    # no DB delete attempt.
    if existing is None:
        logger.info(
            "clear_repo_binding outcome=success account=%s agent=%s",
            auth.account_id,
            agent_id,
        )
        return {"cleared": True}

    # Best-effort vault delete (D-09). WR-05: narrow the ToolError catch to the
    # lookup only — if credentials.delete itself ever raised ToolError it would
    # be the wrong outcome label.
    try:
        vault_id = await _vault_id_for_account(runtime.client, auth.account_id)
    except ToolError:
        # No vault for this account — nothing to delete remotely.
        logger.warning(
            "clear_repo_binding outcome=no_vault_to_clean agent=%s",
            agent_id,
        )
    else:
        try:
            await runtime.client.beta.vaults.credentials.delete(
                existing.ma_secret_ref,
                vault_id=vault_id,
            )
        except anthropic.APIError:
            logger.warning(
                "clear_repo_binding outcome=vault_delete_failed agent=%s",
                agent_id,
            )

    # Delete DB row. We already know `existing` is not None, so clear_binding
    # has a row to clear — let any StoreError propagate.
    async with runtime.session_factory.begin() as session:
        await clear_binding(
            session,
            tenant_id=auth.tenant_id,
            agent_id=agent_id,
        )

    logger.info(
        "clear_repo_binding outcome=success account=%s agent=%s",
        auth.account_id,
        agent_id,
    )
    return {"cleared": True}


def register_self_edit_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    """Register the 7 self-edit tools (4 file tools + 3 repo-binding tools)."""

    @mcp.tool(tags={"admin"})
    async def self_write_file(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        key: str,
        content: str,
    ) -> AgentFileRow:
        """Write or overwrite a per-agent file under `key`.

        Stored in your private agent_files namespace; isolated from other agents.
        """
        return await _self_write_file_impl(runtime, await _auth(ctx), key=key, content=content)

    @mcp.tool
    async def self_read_file(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        key: str,
    ) -> AgentFileRow | None:
        """Read a per-agent file by `key`. Returns null if no file exists at that key."""
        return await _self_read_file_impl(runtime, await _auth(ctx), key=key)

    @mcp.tool
    async def self_list_files(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
    ) -> list[AgentFileRow]:
        """List all keys + metadata for files in your private agent_files namespace."""
        return await _self_list_files_impl(runtime, await _auth(ctx))

    @mcp.tool(tags={"admin"})
    async def self_delete_file(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        key: str,
    ) -> dict[str, object]:
        """Delete a per-agent file by `key`. Idempotent — succeeds whether or not a file existed."""
        return await _self_delete_file_impl(runtime, await _auth(ctx), key=key)

    @mcp.tool(tags={"admin"})
    async def set_repo_binding(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        repo_url: str,
        default_branch: str,
        service: Literal["github"] = "github",
    ) -> AgentRepoBindingPublic:
        """Bind your agent to a git repo.

        Mints a credential, stores it in your MA Vault, and records the binding.
        Replaces any existing binding for your agent.
        """
        return await _set_repo_binding_impl(
            runtime,
            await _auth(ctx),
            repo_url=repo_url,
            default_branch=default_branch,
            service=service,
        )

    @mcp.tool
    async def get_repo_binding(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
    ) -> AgentRepoBindingPublic | None:
        """Return the current repo binding for your agent, or null if unbound."""
        return await _get_repo_binding_impl(runtime, await _auth(ctx))

    @mcp.tool(tags={"admin"})
    async def clear_repo_binding(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
    ) -> dict[str, bool]:
        """Remove the repo binding for your agent.

        Idempotent — succeeds whether or not a binding existed.
        """
        return await _clear_repo_binding_impl(runtime, await _auth(ctx))
