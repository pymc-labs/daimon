"""Agent-scoped external MCP credentials: store them once, mirror them per session.

An external MCP server is attached to the *agent*, so every caller who mentions
that agent gets its toolset. The credential MA uses to talk to that server is
resolved from the vault mounted on the session, and that vault is per
(account, agent) — the caller's. A token written only into the attacher's vault
therefore fails every other caller's turn at MCP init, with retries exhausted:

    MCP server '<name>' initialize failed: no credential is stored for this
    server URL — check that the agent's MCP server URL matches the URL in the vault

MA credentials are write-only, so the token cannot be copied out of the
attacher's vault after the fact. Keeping it here — encrypted, at (tenant, agent)
— is what lets ``create_session`` mirror it into whichever vault the current
caller has, exactly as the per-agent PAT reaches the Copilot credential.

Encryption reuses the GitHub PAT's MultiFernet helpers; they are generic and
key rotation is shared.

A rotated token reaches every caller too: each credential this module writes
carries a version stamp, and a vault whose stamp is stale (or missing) gets an
in-place update on the next turn.

No try/except — exceptions propagate. An empty tuple means "this agent has no
external MCP credentials", never "something broke".
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Mapping
from dataclasses import dataclass

import anthropic
import structlog
from anthropic import AsyncAnthropic
from cryptography.fernet import MultiFernet
from daimon.core.github_credentials import decrypt_token, encrypt_token
from daimon.core.mcp_personal_servers import hidden_mcp_server_names
from daimon.core.mcp_vault import ensure_agent_mcp_vault
from daimon.core.stores import agent_mcp_credentials as cred_store
from daimon.core.stores import mcp_oauth_flows as flows_store
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

# Stamped on every credential this module writes, so a later turn can tell
# whether a vault's credential carries the token currently in the DB. The value
# is the DB row's ``updated_at``, so both sides of the comparison come from OUR
# clock — no dependence on MA's, and nothing derived from the token itself.
# An unstamped credential (written before this existed, or by
# ``add_external_mcp_credential``) reads as "unknown version" and is refreshed
# once, which self-heals vaults already holding a stale token.
METADATA_VERSION_KEY = "daimon_token_version"


@dataclass(frozen=True, slots=True)
class ResolvedMcpCredential:
    """A decrypted credential, ready to mirror into a vault."""

    mcp_server_url: str
    token: str
    version: str


async def save_agent_mcp_credential(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    fernet: MultiFernet,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    mcp_server_url: str,
    plaintext_token: str,
) -> None:
    """Encrypt and UPSERT the token for one of the agent's MCP servers."""
    async with sessionmaker() as session, session.begin():
        await cred_store.upsert_credential(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            # Stored verbatim so it matches the URL attached to the agent spec;
            # detach compares slash-insensitively (delete_credential rtrims).
            mcp_server_url=mcp_server_url,
            encrypted_token=encrypt_token(fernet, plaintext_token),
        )


async def resolve_agent_mcp_credentials(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    fernet: MultiFernet,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
) -> tuple[ResolvedMcpCredential, ...]:
    """Every stored credential for this agent, decrypted."""
    async with sessionmaker() as session:
        rows = await cred_store.list_credentials(session, tenant_id=tenant_id, agent_id=agent_id)
    return tuple(
        ResolvedMcpCredential(
            mcp_server_url=row.mcp_server_url,
            token=decrypt_token(fernet, row.encrypted_token),
            version=row.updated_at.isoformat(),
        )
        for row in rows
    )


async def mirror_credentials_into_vault(
    client: AsyncAnthropic,
    *,
    vault_id: str,
    credentials: tuple[ResolvedMcpCredential, ...],
) -> None:
    """Bring ``vault_id`` in line with the agent's stored credentials.

    Three cases per stored credential, decided by the ``METADATA_VERSION_KEY``
    stamp on the vault's credential at that URL:

    - nothing at that URL → create it (the caller who never had one)
    - stamp differs, or is absent → the token was rotated (or the credential
      predates the stamp) → update it in place
    - stamp matches → already current, no call

    **Never deletes.** Rotation is an in-place ``credentials.update``: MA accepts
    a token-only ``static_bearer`` auth body and keeps the URL, verified against
    the live API on 2026-08-14 (a body carrying ``mcp_server_url`` is a 400 —
    the URL is immutable, and a duplicate POST at the same URL is still a 409).
    That matters because this vault is shared across every thread of one
    (account, agent) and this runs on reused sessions: a delete could yank a
    credential out from under a concurrent in-flight turn in another thread —
    the A3 race that got the per-turn re-stamp limb removed from
    ``ensure_agent_mcp_vault``. An update has no window where the credential is
    absent, so that race does not exist here.

    Note: ``mcp_vault.py`` still documents PATCH as 405-blocked. That was true
    when it was written and is not any more.

    A URL the vault already holds as an ``mcp_oauth`` grant is left alone: that
    is this person's own sign-in, which outranks the agent's shared token, and
    a vault holds one credential per URL, so creating there is a 409 that
    failed every turn after the first Notion connect (staging, 2026-09-15).

    Deliberately NOT degrade-not-block: unlike the Copilot and memory mounts,
    a missing credential here means MA hard-fails the whole turn at MCP init.
    Swallowing an error would only convert this clear failure into that
    confusing one, so ``anthropic.APIError`` propagates (the loud-failure
    precedent is ``resolve_clone_token``). Two exceptions, both concurrent
    writers this function does not lock out: a 409 on create means the
    credential exists, whether a concurrent turn or an auth type this code
    does not know wrote it, and MA can use it as it is; a 404 on update means
    the listed credential was deleted underneath us (a person's OAuth grant
    replacing the agent's token), so the slot is re-read once and decided
    again. A second 404 propagates.
    """
    if not credentials:
        return
    existing_by_url, held_by_grant = await _read_vault_slots(client, vault_id=vault_id)
    for cred in credentials:
        key = _url_key(cred.mcp_server_url)
        try:
            await _mirror_one(
                client,
                vault_id=vault_id,
                cred=cred,
                found=existing_by_url.get(key),
                held_by_grant=key in held_by_grant,
            )
        except anthropic.NotFoundError:
            # The credential listed at this URL was deleted before the update
            # landed, typically by a person's OAuth grant replacing the agent's
            # token. Re-read the slot once and decide again: a grant now holds
            # it (leave it), it is empty (create), or it holds a newer static.
            log.info(
                "mcp_credentials.mirror_update_raced",
                vault_id=vault_id,
                mcp_server_url=cred.mcp_server_url,
            )
            fresh_by_url, fresh_grants = await _read_vault_slots(client, vault_id=vault_id)
            await _mirror_one(
                client,
                vault_id=vault_id,
                cred=cred,
                found=fresh_by_url.get(key),
                held_by_grant=key in fresh_grants,
            )


async def _read_vault_slots(
    client: AsyncAnthropic, *, vault_id: str
) -> tuple[dict[str, tuple[str, str | None]], set[str]]:
    """One list call: static credentials by URL key (id, stamped version), and
    the URL keys a person's `mcp_oauth` grant holds."""
    existing_by_url: dict[str, tuple[str, str | None]] = {}
    held_by_grant: set[str] = set()
    async for existing in client.beta.vaults.credentials.list(vault_id=vault_id):
        if existing.auth.type == "mcp_oauth":
            held_by_grant.add(_url_key(existing.auth.mcp_server_url))
            continue
        if existing.auth.type != "static_bearer":
            continue
        metadata = existing.metadata or {}
        existing_by_url[_url_key(existing.auth.mcp_server_url)] = (
            existing.id,
            metadata.get(METADATA_VERSION_KEY),
        )
    return existing_by_url, held_by_grant


async def _mirror_one(
    client: AsyncAnthropic,
    *,
    vault_id: str,
    cred: ResolvedMcpCredential,
    found: tuple[str, str | None] | None,
    held_by_grant: bool,
) -> None:
    """Skip, create or update one URL, per the cases `mirror_credentials_into_vault` lists."""
    if held_by_grant:
        return
    if found is None:
        try:
            await client.beta.vaults.credentials.create(
                vault_id=vault_id,
                auth={
                    "type": "static_bearer",
                    "mcp_server_url": cred.mcp_server_url,
                    "token": cred.token,
                },
                metadata={METADATA_VERSION_KEY: cred.version},
            )
        except anthropic.ConflictError:
            log.info(
                "mcp_credentials.mirror_raced",
                vault_id=vault_id,
                mcp_server_url=cred.mcp_server_url,
            )
        return
    credential_id, stamped_version = found
    if stamped_version == cred.version:
        return
    await client.beta.vaults.credentials.update(
        credential_id,
        vault_id=vault_id,
        # Token only: MA rejects an update body carrying mcp_server_url.
        auth={"type": "static_bearer", "token": cred.token},
        metadata={METADATA_VERSION_KEY: cred.version},
    )


def _url_key(url: str) -> str:
    """Dict key: the slash-insensitive form `same_server_url` compares on."""
    return url.rstrip("/")


async def sync_agent_mcp_credentials(
    client: AsyncAnthropic,
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    fernet: MultiFernet,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    account_id: uuid.UUID,
    jwt_secret: bytes,
    public_url: str,
    now: dt.datetime,
) -> None:
    """Ensure this caller's vault holds the agent's external MCP credentials.

    For the REUSED-session path. ``create_session`` mirrors at create time, but a
    reused thread session never calls it — so a credential added after that
    session was created would never reach it, and the caller would keep failing
    at MCP init until their session happened to be recreated. That is precisely
    the "someone else has to re-add it" outcome this whole fix exists to remove.

    A reused session's ``vault_ids`` are fixed at create time, but the vault's
    *contents* are read at each turn's MCP init — so writing into the vault the
    session already mounts does reach it on the next turn.

    Cheap for the common case: the DB read gates every MA call, so an agent with
    no external MCP servers pays one indexed query and nothing else.
    """
    credentials = await resolve_agent_mcp_credentials(
        sessionmaker=sessionmaker,
        fernet=fernet,
        tenant_id=tenant_id,
        agent_id=agent_id,
    )
    if not credentials:
        return
    vault_id = await ensure_agent_mcp_vault(
        client,
        account_id=account_id,
        agent_id=agent_id,
        jwt_secret=jwt_secret,
        public_url=public_url,
        now=now,
        session_factory=sessionmaker,
    )
    await mirror_credentials_into_vault(client, vault_id=vault_id, credentials=credentials)


async def resolve_hidden_mcp_server_names(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    account_id: uuid.UUID,
    server_urls: Mapping[str, str],
) -> frozenset[str]:
    """The servers to leave off this caller's session, read from the DB.

    The OAuth twin of `mirror_credentials_into_vault`: a token stored HERE
    reaches every caller, so the server stays; a grant somebody drove through
    their own browser reaches only them, so it comes off everyone else's
    session (`mcp_personal_servers` decides which is which).

    `server_urls` is the agent's own `{name: url}`. An agent whose URLs
    nobody in the tenant has signed in to costs one indexed read and stops
    there, which is every agent until someone connects an OAuth server.
    """
    if not server_urls:
        return frozenset()
    async with sessionmaker() as session:
        grants = await flows_store.list_completed_grants(
            session, tenant_id=tenant_id, server_urls=server_urls.values()
        )
        if not grants:
            return frozenset()
        credentials = await cred_store.list_credentials(
            session, tenant_id=tenant_id, agent_id=agent_id
        )
    return hidden_mcp_server_names(
        grants,
        agent_id=agent_id,
        account_id=account_id,
        shared_server_urls=[credential.mcp_server_url for credential in credentials],
        server_urls=server_urls,
    )
