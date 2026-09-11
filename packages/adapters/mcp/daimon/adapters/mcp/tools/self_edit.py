"""MCP tools for an agent to edit its own ``agent_files`` and manage its
``agent_repo_binding`` from inside an MA turn.

Identity is read server-side from the JWT claims via ``_auth(ctx)``.
Cross-agent isolation is enforced by composite-PK at the store layer
(``(tenant_id, agent_id, key)`` for ``agent_files``;
``(tenant_id, agent_id)`` for ``agent_repo_binding``).

All seven tools are tagged ``agent-chat`` and are therefore visible only
to a session whose token carries an agent identity — the same identity
their ``_require_agent_id`` precondition needs to succeed. An ordinary
chat session never discovers this group; chat users bind working repos with
``request_repo_binding`` and manage keys with ``request_agent_key``,
``list_agent_keys`` and ``remove_agent_key``.

``register_self_edit_tools(mcp, runtime)`` wires the ``@mcp.tool`` closures.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Literal

import anthropic
import httpx
import structlog
from anthropic import AsyncAnthropic
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.core.broker import dispatch_mint_token
from daimon.core.broker.errors import NoBindingError, ProviderConfigError
from daimon.core.errors import StoreError
from daimon.core.github_visibility import pat_can_access_repo
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
from daimon.core.stores.domain import AgentFileRow, AgentRepoBindingRow, RepoAccessProof
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict

logger = structlog.get_logger()


def _owner_repo_from_url(url: str) -> str:
    """Extract canonical ``owner/repo`` from a GitHub URL or short path.

    Must stay byte-identical to
    ``daimon.core.stores.agent_repo_binding._normalize_owner_repo`` — probing
    a differently-canonicalized string would verify a different repo than the
    one the binding actually records.
    """
    return (
        url.removeprefix("https://github.com/")
        .removeprefix("http://github.com/")
        .removeprefix("github.com/")
        .removesuffix(".git")
        .rstrip("/")
    )


class AgentRepoBindingPublic(BaseModel):
    """Agent-visible projection of ``AgentRepoBindingRow``.

    Omits ``ma_secret_ref`` — the vault URI never reaches the agent.
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


# Secret refs the setup panels write are not MA vault entries: ``anon:`` marks a
# public repo with no stored secret, and ``inline-pat:`` points at an encrypted
# row in our own database. Anything else is the id of a vault credential this
# module created, and revoking the binding means deleting it.
_NON_VAULT_SECRET_REF_PREFIXES = ("anon:", "inline-pat:")


def _is_vault_credential_ref(ma_secret_ref: str) -> bool:
    """True when the binding's secret ref names an MA vault credential."""
    return not ma_secret_ref.startswith(_NON_VAULT_SECRET_REF_PREFIXES)


def _require_agent_id(auth: AuthIdentity) -> uuid.UUID:
    """Return ``auth.agent_id`` or raise ``ToolError`` if absent."""
    if auth.agent_id is None:
        raise ToolError("agent_id missing — token was not minted for an agent session")
    return auth.agent_id


async def _vault_id_for_agent(
    client: AsyncAnthropic, *, account_id: uuid.UUID, agent_id: uuid.UUID
) -> str:
    """Return the per-agent daimon-mcp vault id.

    Mirrors the name core provisioning writes when it bootstraps an agent's
    vault (mirrors tools/vault.py).
    """
    display_name = f"daimon-mcp:{account_id}:{agent_id}"
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
    http_client: httpx.AsyncClient | None = None,
) -> AgentRepoBindingPublic:
    """Bind the calling agent to a git repo.

    Order of operations: mint PAT → verify repo access → vault credential
    create → DB row write → old vault credential delete. The access check runs
    BEFORE any vault or DB work, so a refused bind uploads nothing and writes
    no row — there is no orphan credential to compensate for. Vault upload
    happens BEFORE the DB write so a vault failure leaves no binding row. The
    rebind is committed before the old credential is revoked, so a revocation
    failure raises against an already-updated binding rather than rolling it
    back.

    ``http_client`` is optional: when None, this creates and closes its own
    ``httpx.AsyncClient`` around the verification call only. Callers pass one
    only from tests that need to inject a mock transport.
    """
    agent_id = _require_agent_id(auth)

    # 1. Mint plaintext PAT via the broker. Deliberately does NOT opt into the
    # operator service default: the minted token is uploaded as a durable MA
    # vault static_bearer credential below, so opting in would write the
    # shared operator secret into a per-agent vault — the exact durable-copy
    # failure mode this must avoid.
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
            f"No GitHub token is available to bind {repo_url}. Tell the user to ask "
            "Daimon in Discord or Slack chat to connect this agent to that repository; "
            "that chat can use request_repo_binding to collect access privately. "
            "This agent-scoped session cannot call that tool. Do not retry here."
        ) from e
    except ProviderConfigError as e:
        logger.warning(
            "set_repo_binding outcome=provider_config_error service=%s",
            service,
        )
        raise ToolError(str(e)) from e

    # 2. Verify the minted credential can actually read the target repo,
    # before any vault upload or DB write. Canonicalize repo_url with the
    # same rules the store applies, so what gets probed is what gets stored.
    owner_repo = _owner_repo_from_url(repo_url)

    async def _check_access(client: httpx.AsyncClient) -> bool:
        return await pat_can_access_repo(client, owner_repo=owner_repo, pat=token)

    if http_client is not None:
        can_access = await _check_access(http_client)
    else:
        async with httpx.AsyncClient() as owned_client:
            can_access = await _check_access(owned_client)
    if not can_access:
        logger.warning(
            "set_repo_binding outcome=repo_access_denied service=%s agent=%s",
            service,
            agent_id,
        )
        raise ToolError(
            "this agent's GitHub credential cannot read that repo (or the "
            "repo does not exist) — connect a token with access to it first"
        )

    # 3. Discover per-agent vault.
    vault_id = await _vault_id_for_agent(
        runtime.client, account_id=auth.account_id, agent_id=agent_id
    )

    # 4. Capture old ref (separate read session) so we can delete after success.
    async with runtime.session_factory() as session:
        old = await get_binding(
            session,
            tenant_id=auth.tenant_id,
            agent_id=agent_id,
        )
    old_ref = old.ma_secret_ref if old is not None else None

    # 5. Upload new vault credential. this MUST succeed before any DB write.
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

    # 6. Write binding row with NEW ref and a pat-kind proof (the check just
    #    above proved a credentialed read of this exact repo). ordering: row
    #    only after upload OK. Symmetric BL-01 guard: if the DB write fails
    #    after vault.create succeeded, best-effort delete the freshly-minted
    #    credential before re-raising so we don't leak an orphan vault cred
    #    on retries.
    try:
        async with runtime.session_factory.begin() as session:
            row = await set_binding(
                session,
                tenant_id=auth.tenant_id,
                agent_id=agent_id,
                repo_url=repo_url,
                default_branch=default_branch,
                ma_secret_ref=new_cred.id,
                proof=RepoAccessProof(kind="pat", at=datetime.now(UTC), account_id=auth.account_id),
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

    # 7. Delete the credential the previous binding pointed at, AFTER DB commit.
    #    A failure here means that token is still live with nothing pointing at
    #    it any more — most likely because the previous binding was made from a
    #    different account, whose vault this caller cannot reach. The rebind
    #    itself stands; say so rather than reporting a clean success.
    if old_ref is not None and _is_vault_credential_ref(old_ref) and old_ref != new_cred.id:
        try:
            await runtime.client.beta.vaults.credentials.delete(
                old_ref,
                vault_id=vault_id,
            )
        except anthropic.APIError as e:
            logger.warning(
                "set_repo_binding outcome=orphan_old_vault_cred agent=%s cred=%s",
                agent_id,
                old_ref,
            )
            raise ToolError(
                "the repo is now bound, but the credential from the previous binding "
                "could not be revoked and is still live"
            ) from e

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
    """Remove the binding, revoking the credential it points at.

    Idempotent when no binding exists. Refuses when the stored credential
    cannot be revoked: the binding row is per (tenant, agent) while the vault
    is per (account, agent), so a caller from a different account than the one
    that bound the repo resolves a different vault — or none — and deleting the
    row there would drop the only pointer to a token that is still live.
    """
    agent_id = _require_agent_id(auth)

    # Read the binding to capture the ref of the credential to revoke.
    async with runtime.session_factory() as session:
        existing = await get_binding(
            session,
            tenant_id=auth.tenant_id,
            agent_id=agent_id,
        )

    # Idempotency: enforced by structure (pre-check), not by swallowing
    # StoreError. If no binding exists, return immediately — no vault cleanup,
    # no DB delete attempt.
    if existing is None:
        logger.info(
            "clear_repo_binding outcome=success account=%s agent=%s",
            auth.account_id,
            agent_id,
        )
        return {"cleared": True}

    # Revocation must actually revoke. Both failure modes below mean the token
    # this binding points at survives, so neither may reach the row delete.
    # Narrow the ToolError catch to the lookup only — if credentials.delete
    # itself ever raised ToolError it would be the wrong outcome label.
    if _is_vault_credential_ref(existing.ma_secret_ref):
        try:
            vault_id = await _vault_id_for_agent(
                runtime.client, account_id=auth.account_id, agent_id=agent_id
            )
        except ToolError as e:
            logger.warning(
                "clear_repo_binding outcome=no_vault_to_revoke_from agent=%s",
                agent_id,
            )
            raise ToolError(
                "the stored credential cannot be revoked from this account, so the "
                "binding was left in place — clear it from the account that bound it"
            ) from e
        try:
            await runtime.client.beta.vaults.credentials.delete(
                existing.ma_secret_ref,
                vault_id=vault_id,
            )
        except anthropic.APIError as e:
            logger.warning(
                "clear_repo_binding outcome=vault_delete_failed agent=%s cred=%s",
                agent_id,
                existing.ma_secret_ref,
            )
            raise ToolError(
                "the stored credential could not be revoked and is still live, so the "
                "binding was left in place"
            ) from e

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
    """Register the 7 self-edit tools (4 file tools + 3 repo-binding tools).

    All seven carry the ``agent-chat`` tag, so the ``agent-chat`` ``Visibility``
    baseline in ``server.py`` hides them by default, and the identity
    middleware's narrowing reveals them only when the token carries an
    ``agent_id`` claim — the same precondition ``_require_agent_id`` enforces
    at the impl layer. A session with no agent identity (e.g. an ordinary
    chat turn) never sees this group; chat users bind working repos with
    ``request_repo_binding`` and manage keys with ``request_agent_key``,
    ``list_agent_keys`` and ``remove_agent_key``.
    """

    @mcp.tool(tags={"agent-chat"})  # pyright: ignore[reportArgumentType]
    async def self_write_file(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        key: str,
        content: str,
    ) -> AgentFileRow:
        """Write or overwrite a per-agent file under `key`.

        Stored in your private agent_files namespace; isolated from other agents.
        """
        return await _self_write_file_impl(runtime, await _auth(ctx), key=key, content=content)

    @mcp.tool(tags={"agent-chat"})  # pyright: ignore[reportArgumentType]
    async def self_read_file(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        key: str,
    ) -> AgentFileRow | None:
        """Read a per-agent file by `key`. Returns null if no file exists at that key."""
        return await _self_read_file_impl(runtime, await _auth(ctx), key=key)

    @mcp.tool(tags={"agent-chat"})  # pyright: ignore[reportArgumentType]
    async def self_list_files(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
    ) -> list[AgentFileRow]:
        """List all keys + metadata for files in your private agent_files namespace."""
        return await _self_list_files_impl(runtime, await _auth(ctx))

    @mcp.tool(tags={"agent-chat"})  # pyright: ignore[reportArgumentType]
    async def self_delete_file(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        key: str,
    ) -> dict[str, object]:
        """Delete a per-agent file by `key`. Idempotent — succeeds whether or not a file existed."""
        return await _self_delete_file_impl(runtime, await _auth(ctx), key=key)

    @mcp.tool(tags={"agent-chat"})  # pyright: ignore[reportArgumentType]
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

    @mcp.tool(tags={"agent-chat"})  # pyright: ignore[reportArgumentType]
    async def get_repo_binding(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
    ) -> AgentRepoBindingPublic | None:
        """Return the current repo binding for your agent, or null if unbound."""
        return await _get_repo_binding_impl(runtime, await _auth(ctx))

    @mcp.tool(tags={"agent-chat"})  # pyright: ignore[reportArgumentType]
    async def clear_repo_binding(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
    ) -> dict[str, bool]:
        """Remove the repo binding for your agent.

        Idempotent — succeeds whether or not a binding existed.
        """
        return await _clear_repo_binding_impl(runtime, await _auth(ctx))
