"""Idempotent per-agent MA vault bootstrap for the daimon-mcp credential.

One vault per agent, named `daimon-mcp:<account_uuid>:<agent_uuid>`. Cold path
creates vault + single static_bearer credential pointing at `public_url`.
Warm path returns the oldest existing vault with the matching display name
(MA enforces no uniqueness — verified against the live MA API).

Shell function; injects `AsyncAnthropic` and `now`. No global state, no
adapter imports. Called from `daimon.core.sessions.create_session`
conditionally when `settings.mcp.public_url` is set.
"""

from __future__ import annotations

import datetime as dt
import uuid

from anthropic import AsyncAnthropic
from daimon.core.mcp_auth import mint_jwt

GITHUB_COPILOT_MCP_URL = "https://api.githubcopilot.com/mcp"


async def ensure_agent_mcp_vault(
    client: AsyncAnthropic,
    *,
    account_id: uuid.UUID,
    agent_id: uuid.UUID,
    jwt_secret: bytes,
    public_url: str,
    now: dt.datetime,
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
    """
    display_name = f"daimon-mcp:{account_id}:{agent_id}"
    matching = [v async for v in client.beta.vaults.list() if v.display_name == display_name]
    if matching:
        oldest = min(matching, key=lambda v: v.created_at)
        # Inspect existing static_bearer credentials. Only act on URL mismatch:
        # no credential matches `public_url` — stale URL from an earlier deploy
        # (e.g. cloudflare-tunnel → fly URL migration), leaves MA unable to find
        # a credential for the agent's current mcp_server URL.
        # MA blocks PATCH (405) and POST-duplicate (409), so the only way to
        # update a credential is delete + recreate (verified against the live MA API).
        has_matching_url = False
        async for cred in client.beta.vaults.credentials.list(vault_id=oldest.id):
            if cred.auth.type != "static_bearer":
                continue
            if cred.auth.mcp_server_url == public_url:
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

    The caller (OAuth callback) runs this best-effort.
    If it raises, the local Fernet blob is already the source of truth;
    the operator can retry by re-OAuthing.

    Idempotent on retry: list existing credentials, delete any pointed at the
    GitHub Copilot URL, then create the new one.
    """
    async for cred in client.beta.vaults.credentials.list(vault_id=vault_id):
        if cred.auth.mcp_server_url == GITHUB_COPILOT_MCP_URL:
            await client.beta.vaults.credentials.delete(
                cred.id,
                vault_id=vault_id,
            )

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
    session_context: object | None = None,  # deprecated/ignored — 88-04 will remove call sites
) -> None:
    """Create or replace a `static_bearer` credential in the caller's
    per-agent vault for an external (user-supplied) MCP server.

    Looks up the vault by ``display_name == f"daimon-mcp:{account_id}:{agent_id}"``.
    Bootstraps the per-agent vault (creates it + mints the daimon-mcp JWT) when
    it does not yet exist — no longer raises on missing vault.

    Idempotent on retry: deletes any existing ``static_bearer`` credential
    whose ``auth.mcp_server_url`` matches ``mcp_server_url``, then creates the
    new one. Mirrors ``add_github_copilot_credential`` but accepts the URL as
    a parameter (per-user MCP servers each have distinct URLs).

    ``session_context`` is accepted but ignored (deprecated).
    The ``ensure_agent_mcp_vault`` bootstrap no longer accepts it — the vault
    credential is identity-stable and requires no per-call re-stamp.
    Call-site removal is deferred.
    """
    del session_context  # accepted-but-ignored; see docstring
    display_name = f"daimon-mcp:{account_id}:{agent_id}"
    matching = [v async for v in client.beta.vaults.list() if v.display_name == display_name]
    if matching:
        vault_id = min(matching, key=lambda v: v.created_at).id
    else:
        # Bootstrap: create the per-agent vault + daimon-mcp JWT when it doesn't exist yet.
        vault_id = await ensure_agent_mcp_vault(
            client,
            account_id=account_id,
            agent_id=agent_id,
            jwt_secret=jwt_secret,
            public_url=public_url,
            now=now,
        )

    async for cred in client.beta.vaults.credentials.list(vault_id=vault_id):
        if cred.auth.type != "static_bearer":
            continue
        if cred.auth.mcp_server_url == mcp_server_url:
            await client.beta.vaults.credentials.delete(cred.id, vault_id=vault_id)

    await client.beta.vaults.credentials.create(
        vault_id=vault_id,
        auth={
            "type": "static_bearer",
            "mcp_server_url": mcp_server_url,
            "token": token,
        },
    )
