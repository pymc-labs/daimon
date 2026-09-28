"""Cross-adapter primitives for blank-agent creation, fork-copy and delete archival.

Extracted from Discord's `agent_setup.write` module (the original,
audited implementation) so Slack and MCP get a behavior-identical copy of
these security-sensitive paths rather than a per-adapter reimplementation.

Signatures take explicit primitives (`anthropic`, `sessionmaker`, `fernet`,
`oauth_scopes`, tenant/agent UUIDs) — NEVER a Runtime/Protocol object —
because `daimon.core` must not import any `daimon.adapters` module
(import-linter contract), and the three adapter runtimes (Discord, Slack,
MCP) use different field names for the same collaborators.
"""

from __future__ import annotations

import uuid

import anthropic as anthropic_errors
import structlog
from anthropic import AsyncAnthropic
from cryptography.fernet import MultiFernet
from daimon.core.defaults.ma_index import find_agents_by_daimon_tag
from daimon.core.defaults.reconcile_agents import reconcile_agent
from daimon.core.defaults.report import ResourceOutcome
from daimon.core.errors import DaimonError
from daimon.core.github_credentials import get_pat, upsert_credential_encrypted
from daimon.core.memory_resource import archive_memory_store_for_agent
from daimon.core.specs import AgentSpec
from daimon.core.stores import agent_mcp_credentials as mcp_credentials_store
from daimon.core.stores.agent_github_binding import set_agent_github_binding
from daimon.core.stores.agent_repo_binding import copy_binding, get_binding
from daimon.core.stores.domain import AgentRepoBindingRow
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_log = structlog.get_logger()


async def create_blank_agent(
    anthropic: AsyncAnthropic,
    *,
    tenant_id: uuid.UUID,
    name: str,
    system: str | None,
    model: str,
    account_id: uuid.UUID,
    public_url: str | None,
) -> ResourceOutcome:
    """Create an unrouted, user-owned agent from a panel's New agent form.

    Names are tenant-wide identity (reconcile dedup and the resolver key on
    tenant and name only), so any existing agent with `name` refuses it.
    `managed=False`: a seeded-looking agent would be swept by the next
    defaults apply because it is not in the seeded spec list.
    """
    if await find_agents_by_daimon_tag(anthropic, tenant_id=tenant_id, name=name):
        raise DaimonError(
            f"This workspace already has an agent named {name}. Pick a different name."
        )
    try:
        spec = AgentSpec.model_validate({"name": name, "model": model, "system": system})
    except ValidationError as err:
        raise DaimonError(f"Spec validation failed: {err}") from err
    return await reconcile_agent(
        anthropic,
        spec,
        tenant_id=tenant_id,
        dry_run=False,
        account_id=account_id,
        public_url=public_url,
        managed=False,
    )


async def copy_credential_and_repo_binding(
    *,
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    fernet: MultiFernet,
    oauth_scopes: tuple[str, ...],
    tenant_id: uuid.UUID,
    source_agent_uuid: uuid.UUID,
    fork_agent_uuid: uuid.UUID,
) -> None:
    """Re-key the source's per-agent GitHub credential onto the fork, copy
    its repo binding, and copy its agent-wide MCP tokens.

    Every fork path copies the source's `mcp_servers` raw; the tokens in
    `agent_mcp_credentials` now travel with them, or the fork mounted servers
    nothing could authenticate. OAuth grants cannot travel — they sit in MA
    vaults keyed by (person, source agent) — so a copied sign-in server
    stays hidden on the fork until someone signs in there
    (`mcp_personal_servers`). This helper is the single place fork
    credentials are re-keyed, so a further credential-backed kind is
    additive here rather than a new branch in a caller's fork_agent.

    The one check that can fail the fork runs before any write, and the
    token copy is the last write, so a fork reported as failed does not
    leave the source's tokens behind.

    The fork's credential is written under `principal_id=fork_agent_uuid`
    — never aliased to the source principal — mirroring `store_inline_pat`.
    The repo binding is copied with `ma_secret_ref` rewritten to the
    fork's own `inline-pat:` ref for private repos; `anon:` (and any other
    non-inline-pat ref) is copied verbatim.
    A source binding backed by `inline-pat:` with no resolvable/
    decryptable source credential fails the fork loud.

    Fork is a deep copy, by design: the fork inherits the source agent's
    credential AND the source agent's recorded proof of repo access exactly
    as they stand (via `copy_binding`, which carries `repo_url`,
    `default_branch`, and all three proof columns forward verbatim,
    including when they are NULL), and it may later be re-pointed at any
    repo the inherited credential can read. This is intended, not an
    oversight — do not "harden" this by re-deriving proof against the
    forking principal or by gating fork behind an authorization check. A
    source binding with no recorded proof yields a fork binding with no
    recorded proof, which fails closed at clone time exactly the way the
    source does.
    """
    async with sessionmaker() as session:
        source_binding = await get_binding(session, tenant_id=tenant_id, agent_id=source_agent_uuid)

    source_pat: str | None = None
    if source_binding is not None and source_binding.ma_secret_ref.startswith("inline-pat:"):
        source_pat = await get_pat(
            principal_id=source_agent_uuid,
            agent_id=source_agent_uuid,
            sessionmaker=sessionmaker,
            fernet=fernet,
        )
        if source_pat is None:
            raise DaimonError(
                "Fork failed: the source agent's github git-proxy has no resolvable "
                "credential to copy — reconnect GitHub on the source agent and try again."
            )

    if source_binding is not None:
        await _copy_repo_binding(
            sessionmaker,
            fernet=fernet,
            oauth_scopes=oauth_scopes,
            tenant_id=tenant_id,
            source_agent_uuid=source_agent_uuid,
            fork_agent_uuid=fork_agent_uuid,
            source_binding=source_binding,
            source_pat=source_pat,
        )

    async with sessionmaker.begin() as session:
        copied = await mcp_credentials_store.copy_credentials(
            session,
            tenant_id=tenant_id,
            source_agent_id=source_agent_uuid,
            target_agent_id=fork_agent_uuid,
        )
    if copied:
        _log.info(
            "agent_lifecycle.mcp_credentials_copied",
            source_agent_id=str(source_agent_uuid),
            fork_agent_id=str(fork_agent_uuid),
            count=copied,
        )


async def _copy_repo_binding(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    fernet: MultiFernet,
    oauth_scopes: tuple[str, ...],
    tenant_id: uuid.UUID,
    source_agent_uuid: uuid.UUID,
    fork_agent_uuid: uuid.UUID,
    source_binding: AgentRepoBindingRow,
    source_pat: str | None,
) -> None:
    """The GitHub half: re-key an inline PAT under the fork, copy the binding."""
    if source_pat is not None:
        await upsert_credential_encrypted(
            sessionmaker=sessionmaker,
            fernet=fernet,
            principal_id=fork_agent_uuid,
            github_login="(inline-pat)",
            plaintext_token=source_pat,
            scopes=oauth_scopes,
        )
        async with sessionmaker.begin() as session:
            await set_agent_github_binding(
                session, agent_id=fork_agent_uuid, principal_id=fork_agent_uuid
            )
        fork_secret_ref = f"inline-pat:{fork_agent_uuid}"
    else:
        # anon: (or any other non-per-agent-secret ref) carries no secret — copy verbatim.
        fork_secret_ref = source_binding.ma_secret_ref

    async with sessionmaker.begin() as session:
        await copy_binding(
            session,
            tenant_id=tenant_id,
            source_agent_id=source_agent_uuid,
            target_agent_id=fork_agent_uuid,
            ma_secret_ref=fork_secret_ref,
        )


async def archive_memory_store_best_effort(
    *,
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    log_context: dict[str, object],
) -> None:
    """Archive the agent's memory store, degrading best-effort on failure.

    Callers invoke this after the MA agent itself has already been archived
    and the retry path is dead (archived agents are filtered from lookup),
    so a transient memory-store archive failure must not strand the delete
    flow in a failed state — mirrors the mount-side degrade policy in
    `memory_resource.py`. Catches ONLY `anthropic.APIError`; any other
    exception propagates.
    """
    try:
        await archive_memory_store_for_agent(
            anthropic,
            sessionmaker,
            tenant_id=tenant_id,
            agent_id=agent_id,
        )
    except anthropic_errors.APIError:
        # Best-effort degrade: the caller's own delete flow has already
        # committed (e.g. the MA agent is archived and the retry path is
        # dead), so a transient memory-store archive failure must not
        # strand it in a failed state — mirrors the mount-side policy in
        # memory_resource.py.
        _log.warning("agent_lifecycle.memory_store_archive_failed", **log_context)
