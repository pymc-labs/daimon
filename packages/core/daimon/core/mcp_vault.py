"""Idempotent per-agent MA vault bootstrap for the daimon-mcp credential.

One vault per agent, named `daimon-mcp:<account_uuid>:<agent_uuid>`. Cold path
creates vault + single static_bearer credential pointing at `public_url`.
Warm path returns the oldest existing vault with the matching display name
(MA enforces no uniqueness — verified against the live MA API).

The get-or-create critical section (list-then-create) is serialized per
(account_id, agent_id) via a blocking `pg_advisory_xact_lock` — MA has no
server-side uniqueness constraint on `display_name`, so two concurrent
callers racing the empty-list check would otherwise each create a vault.

Shell function; injects `AsyncAnthropic` and `now`. No global state, no
adapter imports. Called from `daimon.core.sessions.create_session`
conditionally when `settings.mcp.public_url` is set.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsVault
from anthropic.types.beta.vaults.beta_managed_agents_environment_variable_auth_response import (
    BetaManagedAgentsEnvironmentVariableAuthResponse,
)
from daimon.core.mcp_auth import mint_jwt
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

GITHUB_COPILOT_MCP_URL = "https://api.githubcopilot.com/mcp"

# Namespace prefix for the pg_advisory_xact_lock key — keeps this subsystem's
# lock keys distinct from any other advisory-lock user in the same database.
LOCK_NAMESPACE = "mcp_vault"


async def _find_vault_by_name(
    client: AsyncAnthropic, *, display_name: str
) -> BetaManagedAgentsVault | None:
    """Return the oldest vault whose ``display_name`` matches, or ``None``.

    MA enforces no uniqueness on ``display_name`` — verified against the live
    MA API — so more than one vault can share a name; the oldest is always
    the canonical one (shared list-then-filter logic for both callers below).
    """
    matching = [v async for v in client.beta.vaults.list() if v.display_name == display_name]
    if not matching:
        return None
    return min(matching, key=lambda v: v.created_at)


async def _lock_vault_namespace(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    agent_id: uuid.UUID,
) -> None:
    """Acquire the blocking, transaction-scoped advisory lock for this
    (account_id, agent_id)'s vault get-or-create critical section.

    Blocking, not try-lock: the second concurrent caller must WAIT for the
    first to finish creating the vault, then proceed to see it on its own
    (post-lock) list call — a non-blocking try-lock would let the loser
    proceed with no vault_id, which is worse than the race it replaces.
    Hashed inside Postgres (``hashtextextended``), never with Python's
    ``hash()`` (PYTHONHASHSEED-salted, unstable across processes).
    """
    lock_key = f"{LOCK_NAMESPACE}:{account_id}:{agent_id}"
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": lock_key},
    )


@asynccontextmanager
async def hold_agent_vault_lock(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    account_id: uuid.UUID,
    agent_id: uuid.UUID,
) -> AsyncIterator[None]:
    """Hold this (account_id, agent_id)'s vault lock for the body of the block.

    A vault holds one credential per URL, and each writer below lists the
    slot and then writes it, so two of them interleaving can leave the loser
    with a 409 or a 404. Holding one lock makes each read-then-write see the
    other's finished result. The writers that hold it:

    - ``ensure_agent_mcp_vault`` (the daimon-mcp JWT at ``public_url``) and
      ``add_external_mcp_credential`` (a person's pasted token) take it
      themselves;
    - the OAuth grant (``complete_mcp_oauth_flow`` around
      ``put_mcp_oauth_credential``), the mirror of the agent's shared tokens
      (``create_session`` and ``sync_agent_mcp_credentials`` around
      ``mirror_credentials_into_vault``) and the Copilot PAT
      (``create_session`` around ``add_github_copilot_credential``) take it
      through this block.

    Two vault writers do not hold it. Neither can take a URL from a person's
    grant or the mirror:

    - ``set_repo_binding`` / ``clear_repo_binding`` (MCP self-edit) create
      a fresh credential at the ``https://github.com`` placeholder and delete
      only the ids their binding row recorded. They never list and replace
      a slot, and the placeholder is no MCP server's URL, so neither a grant
      nor the mirror writes it.
    - The operator's ``daimon mcp sweep-credentials`` deletes and recreates
      the JWT at ``public_url``. A turn's ``ensure_agent_mcp_vault`` landing
      between those two calls recreates the JWT itself (without ``is_admin``,
      which is what the sweep wants). The sweep's own create is then a 409
      and it stops, and re-running it is safe. A session that starts inside
      that gap can also start without the JWT. This is a known gap, accepted
      because the sweep is a manual, one-off backfill.

    Never call ``ensure_agent_mcp_vault`` or ``add_external_mcp_credential``
    inside the block: they take this lock on another connection and would
    wait on it forever.
    """
    async with session_factory() as session, session.begin():
        await _lock_vault_namespace(session, account_id=account_id, agent_id=agent_id)
        yield


async def _ensure_agent_mcp_vault_locked(
    client: AsyncAnthropic,
    *,
    account_id: uuid.UUID,
    agent_id: uuid.UUID,
    jwt_secret: bytes,
    public_url: str,
    now: dt.datetime,
) -> str:
    """Core get-or-create body, assuming the caller already holds the
    per-(account_id, agent_id) advisory lock for this transaction.

    Factored out so ``add_external_mcp_credential``'s bootstrap branch can
    invoke this directly (same already-locked session) instead of calling
    the public, lock-acquiring ``ensure_agent_mcp_vault`` — nesting a second
    lock-acquisition attempt on the same key from a *different* connection
    would deadlock against the outer transaction's own held lock.
    """
    display_name = f"daimon-mcp:{account_id}:{agent_id}"
    oldest = await _find_vault_by_name(client, display_name=display_name)
    if oldest is not None:
        # Only act on URL mismatch: no credential at `public_url` — stale URL
        # from an earlier deploy (e.g. cloudflare-tunnel → fly URL migration),
        # leaves MA unable to find a credential for the agent's current
        # mcp_server URL. Any auth type at that URL counts: a vault holds one
        # credential per URL, so creating next to an `mcp_oauth` grant would be
        # a 409 on every session create with nothing to heal it.
        has_matching_url = False
        async for cred in client.beta.vaults.credentials.list(vault_id=oldest.id):
            if isinstance(cred.auth, BetaManagedAgentsEnvironmentVariableAuthResponse):
                continue
            if same_server_url(cred.auth.mcp_server_url, public_url):
                has_matching_url = True
                break
        url_mismatch = not has_matching_url
        if url_mismatch:
            # No credential for the current URL yet — create fresh.
            await client.beta.vaults.credentials.create(
                vault_id=oldest.id,
                auth={
                    "type": "static_bearer",
                    "mcp_server_url": public_url,
                    "token": mint_jwt(
                        account_id=account_id,
                        secret=jwt_secret,
                        now=now,
                    ),
                },
            )
        return oldest.id

    vault = await client.beta.vaults.create(display_name=display_name)
    token = mint_jwt(
        account_id=account_id,
        secret=jwt_secret,
        now=now,
    )
    await client.beta.vaults.credentials.create(
        vault_id=vault.id,
        auth={
            "type": "static_bearer",
            "mcp_server_url": public_url,
            "token": token,
        },
    )
    return vault.id


def same_server_url(left: str, right: str) -> bool:
    """One vault slot per server: compare without a trailing slash.

    Whether MA itself treats `.../mcp` and `.../mcp/` as one URL is not
    verified; daimon treats them as one everywhere so its own comparisons
    agree with each other.
    """
    return left.rstrip("/") == right.rstrip("/")


async def ensure_agent_mcp_vault(
    client: AsyncAnthropic,
    *,
    account_id: uuid.UUID,
    agent_id: uuid.UUID,
    jwt_secret: bytes,
    public_url: str,
    now: dt.datetime,
    session_factory: async_sessionmaker[AsyncSession],
) -> str:
    """Return the ``ma_vault_id`` for this agent's daimon-mcp vault.

    Creates the vault + credential on cold path. On warm path, only acts when
    there is no credential matching the current ``public_url`` (URL-drift case,
    e.g. cloudflare-tunnel → fly URL migration) — a fresh credential is created
    at the new URL; the prior credential is left as an inert orphan.

    The long-lived credential is always non-admin and never carries the ``internal``
    discriminator claim — admin is resolved live from the DB ``role`` by the verifier
    on each request (ADMIN-01). A Discord vault token's baked ``is_admin`` claim alone
    never elevates a non-admin caller at the MCP gate (#162 escalation closed).

    Per-turn delete+recreate (the old re-stamp limb) is intentionally removed (Phase
    88-03 T-88-03-02): the credential is identity-stable (``sub`` = account, no
    ``is_admin``, no ``internal``), so nothing per-turn needs to mutate it. Removing
    the re-stamp limb eliminates the cross-thread race where an in-flight session
    re-reads the shared per-(account,agent) vault credential mid-turn (A3).

    We do NOT delete credentials on URL drift. The vault is shared with user-added
    external MCP credentials (``add_external_mcp_credential``) whose URLs we cannot
    authenticate as "ours". Deleting any cred that doesn't match the current
    ``public_url`` would silently nuke user data on the first deploy-URL change.
    Cost: O(deploys-with-URL-change) orphan creds per agent, bounded and harmless.

    The daimon-mcp JWT claims are account-scoped only — no agent claim is added (SC-4).
    Only the vault's storage location is per-agent.

    The entire list-then-create body runs inside a blocking Postgres
    advisory-transaction lock keyed on ``(account_id, agent_id)`` (SYNC-01):
    two concurrent callers for the same agent can never both pass the
    empty-match check and each create a vault — MA has no server-side
    uniqueness constraint on ``display_name`` to fall back on.
    """
    async with session_factory() as session, session.begin():
        await _lock_vault_namespace(session, account_id=account_id, agent_id=agent_id)
        return await _ensure_agent_mcp_vault_locked(
            client,
            account_id=account_id,
            agent_id=agent_id,
            jwt_secret=jwt_secret,
            public_url=public_url,
            now=now,
        )


async def add_github_copilot_credential(
    client: AsyncAnthropic,
    *,
    vault_id: str,
    token: str,
) -> None:
    """Create or replace the GitHub Copilot MCP `static_bearer` credential.

    The default GitHub Copilot MCP server (per MA SDK) consumes credentials
    via vault-injection at `mcp_server_url=https://api.githubcopilot.com/mcp`.
    This is the second credential in the per-agent vault — the first is the
    daimon-mcp JWT placed by `ensure_agent_mcp_vault`.

    The caller (``create_session``) holds the per-(account, agent) vault
    lock (``hold_agent_vault_lock``) and runs this best-effort. If it raises,
    the local Fernet blob is already the source of truth, and the next
    session create writes it again.

    Idempotent on retry: list existing credentials, delete any static one
    pointed at the GitHub Copilot URL, then create the new one. A person's own
    `mcp_oauth` grant at that URL is left in place and nothing is created:
    their sign-in outranks the agent's PAT, and the slot is taken anyway.
    """
    stale: list[str] = []
    async for cred in client.beta.vaults.credentials.list(vault_id=vault_id):
        if isinstance(cred.auth, BetaManagedAgentsEnvironmentVariableAuthResponse):
            continue
        if not same_server_url(cred.auth.mcp_server_url, GITHUB_COPILOT_MCP_URL):
            continue
        if cred.auth.type == "mcp_oauth":
            return
        stale.append(cred.id)
    # Collect first: deleting while the list paginates can skip an entry.
    for credential_id in stale:
        await client.beta.vaults.credentials.delete(credential_id, vault_id=vault_id)

    await client.beta.vaults.credentials.create(
        vault_id=vault_id,
        auth={
            "type": "static_bearer",
            "mcp_server_url": GITHUB_COPILOT_MCP_URL,
            "token": token,
        },
    )


async def add_external_mcp_credential(
    client: AsyncAnthropic,
    *,
    account_id: uuid.UUID,
    agent_id: uuid.UUID,
    jwt_secret: bytes,
    public_url: str,
    now: dt.datetime,
    mcp_server_url: str,
    token: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Create or replace a `static_bearer` credential in the caller's
    per-agent vault for an external (user-supplied) MCP server.

    Looks up the vault by ``display_name == f"daimon-mcp:{account_id}:{agent_id}"``.
    Bootstraps the per-agent vault (creates it + mints the daimon-mcp JWT) when
    it does not yet exist — no longer raises on missing vault.

    Idempotent on retry: deletes any existing credential at ``mcp_server_url``,
    a ``static_bearer`` or this person's own ``mcp_oauth`` grant, then creates
    the new one. A vault holds one credential per URL, so leaving the grant in
    place would make the create a 409; the person who pastes a token for a
    server they had signed in to is choosing the token. Mirrors
    ``add_github_copilot_credential`` but accepts the URL as a parameter
    (per-user MCP servers each have distinct URLs).

    The entire list-then-create body (including the bootstrap branch) runs
    inside the same blocking Postgres advisory-transaction lock as
    ``ensure_agent_mcp_vault``, keyed on ``(account_id, agent_id)`` (SYNC-01) —
    it shares the vault get-or-create race with that function and must
    serialize against it, not just against itself.
    """
    display_name = f"daimon-mcp:{account_id}:{agent_id}"
    async with session_factory() as session, session.begin():
        await _lock_vault_namespace(session, account_id=account_id, agent_id=agent_id)
        oldest = await _find_vault_by_name(client, display_name=display_name)
        if oldest is not None:
            vault_id = oldest.id
        else:
            # Bootstrap: create the per-agent vault + daimon-mcp JWT when it
            # doesn't exist yet. Already holding the lock acquired above —
            # call the locked-core helper directly, NOT the public
            # ensure_agent_mcp_vault (which would try to re-acquire the same
            # lock key from a fresh connection and deadlock against this one).
            vault_id = await _ensure_agent_mcp_vault_locked(
                client,
                account_id=account_id,
                agent_id=agent_id,
                jwt_secret=jwt_secret,
                public_url=public_url,
                now=now,
            )

        # Collect first: deleting while the list paginates can skip an entry.
        stale = [
            cred.id
            async for cred in client.beta.vaults.credentials.list(vault_id=vault_id)
            if not isinstance(cred.auth, BetaManagedAgentsEnvironmentVariableAuthResponse)
            and same_server_url(cred.auth.mcp_server_url, mcp_server_url)
        ]
        for credential_id in stale:
            await client.beta.vaults.credentials.delete(credential_id, vault_id=vault_id)

        await client.beta.vaults.credentials.create(
            vault_id=vault_id,
            auth={
                "type": "static_bearer",
                "mcp_server_url": mcp_server_url,
                "token": token,
            },
        )
