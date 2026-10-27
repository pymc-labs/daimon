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
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from typing import Any

from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsVault
from anthropic.types.beta.vaults.beta_managed_agents_credential import (
    BetaManagedAgentsCredential,
)
from anthropic.types.beta.vaults.beta_managed_agents_environment_variable_auth_response import (
    BetaManagedAgentsEnvironmentVariableAuthResponse,
)
from daimon.core.mcp_auth import CHAT_TOKEN_REFRESH_MARGIN, chat_token_label, mint_chat_mcp_token
from daimon.core.mcp_hold import hold_from_metadata, hold_metadata_value, hold_vault_suffix
from daimon.core.mcp_identity import RelayBinding
from daimon.core.mcp_server_url import canonical_mcp_url, same_mcp_url
from daimon.core.mux_compat import (
    archive_vault,
    create_vault,
    delete_credential,
    list_credentials,
    list_vaults,
    retrieve_vault,
    store_credential,
    update_credential,
)
from daimon.core.stores.accounts import get_account_with_tenant
from daimon.core.stores.mcp_tokens import get_mcp_token
from daimon.core.turn_hold import TurnHold
from mux.contracts.ids import Scope
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

GITHUB_COPILOT_MCP_URL = "https://api.githubcopilot.com/mcp"

# Namespace prefix for the pg_advisory_xact_lock key — keeps this subsystem's
# lock keys distinct from any other advisory-lock user in the same database.
LOCK_NAMESPACE = "mcp_vault"

#: Credential metadata key that records the hold a held credential was minted with.
HOLD_METADATA_KEY = "daimon_chat_hold"
#: Marks a session's signed-identity relay credential; the binding keys follow it.
RELAY_METADATA_KEY = "daimon_chat_relay"
RELAY_AGENT_NAME_METADATA_KEY = "daimon_relay_agent_name"
RELAY_SESSION_METADATA_KEY = "daimon_relay_session"
RELAY_ISOLATED_METADATA_KEY = "daimon_relay_isolated"
#: Display-name prefix of a session's own relay vault.
RELAY_VAULT_PREFIX = "daimon-relay:"


async def _find_vault_by_name(
    client: AsyncAnthropic,
    *,
    display_name: str,
    scope: Scope | None = None,
) -> BetaManagedAgentsVault | None:
    """Return the oldest vault whose ``display_name`` matches, or ``None``.

    MA enforces no uniqueness on ``display_name`` — verified against the live
    MA API — so more than one vault can share a name; the oldest is always
    the canonical one (shared list-then-filter logic for both callers below).
    """
    scope = scope or Scope.legacy_host_authorized(
        call_site="daimon.core.mcp_vault:_find_vault_by_name"
    )
    matching = [v async for v in list_vaults(client, scope=scope) if v.display_name == display_name]
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


async def chat_mcp_credential(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    agent_id: uuid.UUID,
    jwt_secret: bytes,
    public_url: str,
    now: dt.datetime,
    slack_turn_context_id: uuid.UUID | None = None,
    hold: TurnHold | None = None,
    relay: RelayBinding | None = None,
) -> dict[str, Any]:
    """Build one vault payload and register its individually revocable JWT.

    A ``hold`` or ``relay`` binding is signed into the JWT and recorded in the
    credential's metadata, where a refresh finds it again.
    """
    token, jti, expires_at = await mint_chat_mcp_token(
        session,
        account_id=account_id,
        agent_id=agent_id,
        secret=jwt_secret,
        now=now,
        slack_turn_context_id=slack_turn_context_id,
        hold=hold,
        relay=relay,
    )
    metadata = {
        "daimon_chat_identity": str(agent_id),
        "daimon_chat_jti": str(jti),
        "daimon_chat_expires_at": expires_at.isoformat(),
    }
    if slack_turn_context_id is not None:
        metadata["daimon_chat_context"] = str(slack_turn_context_id)
    if hold is not None:
        metadata[HOLD_METADATA_KEY] = hold_metadata_value(hold)
    if relay is not None:
        metadata[RELAY_METADATA_KEY] = "true"
        metadata[RELAY_AGENT_NAME_METADATA_KEY] = relay.agent_name
        if relay.session_key is not None:
            metadata[RELAY_SESSION_METADATA_KEY] = str(relay.session_key)
        if relay.isolated:
            metadata[RELAY_ISOLATED_METADATA_KEY] = "true"
    return {
        "metadata": metadata,
        "auth": {"type": "static_bearer", "mcp_server_url": public_url, "token": token},
    }


async def _refresh_chat_credential_locked(
    client: AsyncAnthropic,
    *,
    session: AsyncSession,
    vault_id: str,
    account_id: uuid.UUID,
    agent_id: uuid.UUID,
    jwt_secret: bytes,
    public_url: str,
    now: dt.datetime,
    scope: Scope,
    slack_turn_context_id: uuid.UUID | None = None,
    hold: TurnHold | None = None,
    preserve_context: bool = False,
    registered_payload: dict[str, Any] | None = None,
    create_missing: bool = True,
) -> None:
    target = None
    metadata: dict[str, str] = {}
    async for cred in list_credentials(client, vault_id, scope=scope):
        if isinstance(cred.auth, BetaManagedAgentsEnvironmentVariableAuthResponse):
            continue
        if not same_server_url(cred.auth.mcp_server_url, public_url):
            continue
        if cred.auth.type != "static_bearer":
            return  # A person's OAuth credential retains its slot and authority.
        metadata = cred.metadata or {}
        if preserve_context and metadata.get("daimon_chat_context") is not None:
            slack_turn_context_id = uuid.UUID(metadata["daimon_chat_context"])
        if preserve_context and metadata.get(HOLD_METADATA_KEY) is not None:
            # A mounted vault keeps the hold it was minted with; a refresh never
            # widens or drops it.
            hold = hold_from_metadata(metadata[HOLD_METADATA_KEY])
        raw_jti = metadata.get("daimon_chat_jti")
        try:
            jti = uuid.UUID(raw_jti) if isinstance(raw_jti, str) else None
        except ValueError:
            jti = None
        row = None if jti is None else await get_mcp_token(session, jti=jti)
        identity = await get_account_with_tenant(session, account_id=account_id)
        if (
            identity is not None
            and row is not None
            and row.kind == "agent"
            and row.label == chat_token_label(row.jti, held=hold is not None)
            and row.account_id == account_id
            and row.tenant_id == identity.tenant_id
            and row.agent_id == str(agent_id)
            and row.revoked_at is None
            and row.expires_at is not None
            and row.expires_at > now + CHAT_TOKEN_REFRESH_MARGIN
            and metadata.get("daimon_chat_identity") == str(agent_id)
            and metadata.get("daimon_chat_context")
            == (None if slack_turn_context_id is None else str(slack_turn_context_id))
            and metadata.get(HOLD_METADATA_KEY)
            == (None if hold is None else hold_metadata_value(hold))
            and (row.platform, row.channel_id)
            == ((None, None) if hold is None else (hold.platform, hold.channel_id))
        ):
            return
        target = cred
        break

    if target is None and not create_missing:
        return
    if registered_payload is None:
        payload = await chat_mcp_credential(
            session,
            account_id=account_id,
            agent_id=agent_id,
            jwt_secret=jwt_secret,
            public_url=public_url,
            now=now,
            slack_turn_context_id=slack_turn_context_id,
            hold=hold,
        )
        # A running session can observe the provider write before its response
        # returns. Commit first, then re-lock and re-read so another refresh or
        # OAuth replacement wins safely. No second pooled connection is held.
        await session.commit()
        await _lock_vault_namespace(session, account_id=account_id, agent_id=agent_id)
        await _refresh_chat_credential_locked(
            client,
            session=session,
            vault_id=vault_id,
            account_id=account_id,
            agent_id=agent_id,
            jwt_secret=jwt_secret,
            public_url=public_url,
            now=now,
            scope=scope,
            slack_turn_context_id=slack_turn_context_id,
            hold=hold,
            registered_payload=payload,
            create_missing=create_missing,
        )
        return

    if target is None:
        await store_credential(client, vault_id, registered_payload, scope=scope)
        return
    registered_payload["metadata"] = {
        **{
            key: value
            for key, value in metadata.items()
            if key not in ("daimon_chat_context", HOLD_METADATA_KEY)
        },
        **registered_payload["metadata"],
    }
    # Update in place: no empty slot and no cross-thread delete/recreate race.
    registered_payload["auth"].pop("mcp_server_url")
    await update_credential(client, vault_id, target.id, registered_payload, scope=scope)


async def refresh_agent_mcp_vault(
    client: AsyncAnthropic,
    *,
    vault_id: str,
    account_id: uuid.UUID,
    agent_id: uuid.UUID,
    jwt_secret: bytes,
    public_url: str,
    now: dt.datetime,
    session_factory: async_sessionmaker[AsyncSession],
    scope: Scope,
) -> None:
    """Refresh the vault already mounted by a reused ordinary chat session."""
    async with session_factory() as session:
        await _lock_vault_namespace(session, account_id=account_id, agent_id=agent_id)
        await _refresh_chat_credential_locked(
            client,
            session=session,
            vault_id=vault_id,
            account_id=account_id,
            agent_id=agent_id,
            jwt_secret=jwt_secret,
            public_url=public_url,
            now=now,
            scope=scope,
            preserve_context=True,
            create_missing=False,
        )

        await session.commit()


async def refresh_mounted_mcp_vaults(
    client: AsyncAnthropic,
    *,
    vault_ids: Iterable[str],
    account_id: uuid.UUID,
    agent_id: uuid.UUID,
    jwt_secret: bytes,
    public_url: str,
    now: dt.datetime,
    session_factory: async_sessionmaker[AsyncSession],
    scope: Scope,
    relay_urls: Sequence[str] = (),
) -> None:
    """Renew every actually mounted Daimon slot, without creating missing slots.

    With ``relay_urls`` (the agent's signed-identity relays) a mounted relay vault
    is renewed too and gains any of them attached since the session started.
    """
    for vault_id in dict.fromkeys(vault_ids):
        await refresh_agent_mcp_vault(
            client,
            vault_id=vault_id,
            account_id=account_id,
            agent_id=agent_id,
            jwt_secret=jwt_secret,
            public_url=public_url,
            now=now,
            session_factory=session_factory,
            scope=scope,
        )
        if relay_urls:
            await refresh_relay_vault(
                client,
                vault_id=vault_id,
                account_id=account_id,
                agent_id=agent_id,
                jwt_secret=jwt_secret,
                relay_urls=relay_urls,
                now=now,
                session_factory=session_factory,
                scope=scope,
            )


async def _ensure_agent_mcp_vault_locked(
    client: AsyncAnthropic,
    *,
    session: AsyncSession,
    account_id: uuid.UUID,
    agent_id: uuid.UUID,
    jwt_secret: bytes,
    public_url: str,
    now: dt.datetime,
    slack_turn_context_id: uuid.UUID | None = None,
    hold: TurnHold | None = None,
    scope: Scope,
) -> str:
    display_name = f"daimon-mcp:{account_id}:{agent_id}"
    if slack_turn_context_id is not None:
        display_name += f":slack-turn:{slack_turn_context_id}"
    if hold is not None:
        display_name += hold_vault_suffix(hold)
    oldest = await _find_vault_by_name(client, display_name=display_name)
    if oldest is not None:
        await _refresh_chat_credential_locked(
            client,
            session=session,
            vault_id=oldest.id,
            account_id=account_id,
            agent_id=agent_id,
            jwt_secret=jwt_secret,
            public_url=public_url,
            now=now,
            scope=scope,
            slack_turn_context_id=slack_turn_context_id,
            hold=hold,
        )
        return oldest.id
    vault = await create_vault(client, display_name, scope=scope)
    payload = await chat_mcp_credential(
        session,
        account_id=account_id,
        agent_id=agent_id,
        jwt_secret=jwt_secret,
        public_url=public_url,
        now=now,
        slack_turn_context_id=slack_turn_context_id,
        hold=hold,
    )
    await store_credential(client, vault.id, payload, scope=scope)
    return vault.id


async def create_relay_vault(
    client: AsyncAnthropic,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    account_id: uuid.UUID,
    agent_id: uuid.UUID,
    jwt_secret: bytes,
    relay_urls: Sequence[str],
    relay: RelayBinding,
    now: dt.datetime,
    scope: Scope,
    slack_turn_context_id: uuid.UUID | None = None,
    hold: TurnHold | None = None,
) -> str:
    """Create one session's own vault for its signed-identity relay URLs.

    Every URL gets the same chat credential, carrying the session's ``relay``
    binding and the hold and Slack context of the session's Daimon credential, so
    it never reaches further than that one. No other session mounts this vault,
    so the relay can tell which session made a request: the caller's shared
    vault would let a routine or a stale session pass as someone's live turn.
    """
    vault = await create_vault(client, f"{RELAY_VAULT_PREFIX}{account_id}:{agent_id}", scope=scope)
    try:
        async with session_factory() as session, session.begin():
            payload = await chat_mcp_credential(
                session,
                account_id=account_id,
                agent_id=agent_id,
                jwt_secret=jwt_secret,
                public_url=relay_urls[0],
                now=now,
                slack_turn_context_id=slack_turn_context_id,
                hold=hold,
                relay=relay,
            )
        for url in dict.fromkeys(relay_urls):
            await store_credential(
                client,
                vault.id,
                {**payload, "auth": {**payload["auth"], "mcp_server_url": url}},
                scope=scope,
            )
    except BaseException:
        await archive_vault(client, vault.id, scope=scope)
        raise
    return vault.id


async def archive_relay_vaults(
    client: AsyncAnthropic, vault_ids: Iterable[str], *, scope: Scope
) -> None:
    """Archive the relay vaults among a closed session's mounted vaults.

    `mcp_vault_janitor.archive_idle_relay_vaults` archives any this misses.
    """
    for vault_id in dict.fromkeys(vault_ids):
        vault = await retrieve_vault(client, vault_id, scope=scope)
        if vault.display_name.startswith(RELAY_VAULT_PREFIX) and vault.archived_at is None:
            await archive_vault(client, vault_id, scope=scope)


async def refresh_relay_vault(
    client: AsyncAnthropic,
    *,
    vault_id: str,
    account_id: uuid.UUID,
    agent_id: uuid.UUID,
    jwt_secret: bytes,
    relay_urls: Sequence[str],
    now: dt.datetime,
    session_factory: async_sessionmaker[AsyncSession],
    scope: Scope,
) -> None:
    """Renew a session's relay credentials and add any relay URL attached since.

    The binding, hold and Slack context come from the credential's own metadata,
    never from the caller. A vault without relay credentials is left alone.
    """
    slots: dict[str, BetaManagedAgentsCredential] = {}
    async for cred in list_credentials(client, vault_id, scope=scope):
        if (
            not isinstance(cred.auth, BetaManagedAgentsEnvironmentVariableAuthResponse)
            and cred.auth.type == "static_bearer"
            and (cred.metadata or {}).get(RELAY_METADATA_KEY) == "true"
            and (cred.metadata or {}).get("daimon_chat_identity") == str(agent_id)
        ):
            slots[cred.auth.mcp_server_url] = cred
    if not slots:
        return
    # One slot per server: a URL differing only in form from a slot is that slot.
    held = {canonical_mcp_url(url) for url in slots}
    missing = [url for url in relay_urls if canonical_mcp_url(url) not in held]
    metadata = next(iter(slots.values())).metadata or {}
    expiries = {(cred.metadata or {}).get("daimon_chat_expires_at") for cred in slots.values()}
    expires_at = next(iter(expiries)) if len(expiries) == 1 else None
    if (
        not missing
        and expires_at is not None
        and dt.datetime.fromisoformat(expires_at) > now + CHAT_TOKEN_REFRESH_MARGIN
    ):
        return
    raw_key = metadata.get(RELAY_SESSION_METADATA_KEY)
    context = metadata.get("daimon_chat_context")
    # No vault lock: only the session that mounts this vault refreshes it, while
    # preparing its own turn.
    async with session_factory() as session, session.begin():
        payload = await chat_mcp_credential(
            session,
            account_id=account_id,
            agent_id=agent_id,
            jwt_secret=jwt_secret,
            public_url=next(iter(slots)),
            now=now,
            slack_turn_context_id=None if context is None else uuid.UUID(context),
            hold=hold_from_metadata(metadata.get(HOLD_METADATA_KEY)),
            relay=RelayBinding(
                agent_name=metadata[RELAY_AGENT_NAME_METADATA_KEY],
                session_key=None if raw_key is None else uuid.UUID(raw_key),
                isolated=metadata.get(RELAY_ISOLATED_METADATA_KEY) == "true",
            ),
        )
    # Registered before the provider write, as for the shared vault.
    for url in [*slots, *missing]:
        existing = slots.get(url)
        auth: dict[str, str] = {**payload["auth"], "mcp_server_url": url}
        if existing is None:
            await store_credential(client, vault_id, {**payload, "auth": auth}, scope=scope)
        else:
            # In place, as for the shared vault: the slot is never empty.
            auth.pop("mcp_server_url")
            await update_credential(
                client, vault_id, existing.id, {**payload, "auth": auth}, scope=scope
            )


def same_server_url(left: str, right: str) -> bool:
    """One vault slot per server: compare in `canonical_mcp_url` form.

    Whether MA itself treats `.../mcp` and `.../mcp/` as one URL is not
    verified; daimon treats them as one everywhere so its own comparisons
    agree with each other.
    """
    return same_mcp_url(left, right)


async def ensure_agent_mcp_vault(
    client: AsyncAnthropic,
    *,
    account_id: uuid.UUID,
    agent_id: uuid.UUID,
    jwt_secret: bytes,
    public_url: str,
    now: dt.datetime,
    session_factory: async_sessionmaker[AsyncSession],
    slack_turn_context_id: uuid.UUID | None = None,
    hold: TurnHold | None = None,
    scope: Scope | None = None,
) -> str:
    """Return the ``ma_vault_id`` for this agent's daimon-mcp vault.

    A Slack DM execution ID selects a separate vault namespace and signed claim;
    it never modifies the shared agent vault. Only sessions for that logical turn
    receive the isolated vault. Completed execution rows revoke DM-read authority.

    A ``hold`` does the same for a held turn: its own vault per (account, agent,
    platform, channel, client or member), whose credential carries the signed
    hold and is bound to that channel in the registry. A warm vault whose
    recorded hold differs is re-minted, never reused. An unheld turn keeps the
    ordinary vault, which never carries a hold.

    Creates the vault + credential on cold path. On warm path, upgrades legacy
    static-bearer credentials once to carry ``chat_agent_id``. If no credential
    matches ``public_url`` (URL drift), creates one at the new URL and leaves
    the prior credential as an inert orphan. OAuth credentials are preserved.

    The one-hour registered credential is always non-admin and never carries the ``internal``
    discriminator claim — admin is resolved live from the DB ``role`` by the verifier
    on each request. A Discord vault token's baked ``is_admin`` claim alone
    never elevates a non-admin caller at the MCP gate.

    Every JWT has a registered jti and one-hour expiry. Warm calls read the
    registry and refresh in place when revoked, unregistered or within fifty
    minutes of expiry, covering the forty-five-minute turn ceiling plus margin.
    A healthy fresh credential is unchanged. Rotation leaves old unrevoked
    tokens valid only to their original expiry so in-flight turns keep working.
    Reused sessions refresh their actual mounted vault before every turn,
    independently of external credentials, encryption or configuration changes.

    We do NOT delete credentials on URL drift. The vault is shared with user-added
    external MCP credentials (``add_external_mcp_credential``) whose URLs we cannot
    authenticate as "ours". Deleting any cred that doesn't match the current
    ``public_url`` would silently nuke user data on the first deploy-URL change.
    Cost: O(deploys-with-URL-change) orphan creds per agent, bounded and harmless.

    The JWT subject stays account-scoped. Its separate chat_agent_id claim is
    binds session reads and Google brokering to the executing agent without
    conferring the separate external agent-chat tool authority.

    The entire list-then-create body runs inside a blocking Postgres
    advisory-transaction lock keyed on ``(account_id, agent_id)``:
    two concurrent callers for the same agent can never both pass the
    empty-match check and each create a vault — MA has no server-side
    uniqueness constraint on ``display_name`` to fall back on.
    """
    scope = scope or Scope.legacy_host_authorized(
        call_site="daimon.core.mcp_vault:ensure_agent_mcp_vault"
    )
    async with session_factory() as session:
        await _lock_vault_namespace(session, account_id=account_id, agent_id=agent_id)
        vault_id = await _ensure_agent_mcp_vault_locked(
            client,
            session=session,
            account_id=account_id,
            agent_id=agent_id,
            jwt_secret=jwt_secret,
            public_url=public_url,
            now=now,
            slack_turn_context_id=slack_turn_context_id,
            hold=hold,
            scope=scope,
        )

        await session.commit()
        return vault_id


async def add_github_copilot_credential(
    client: AsyncAnthropic,
    *,
    vault_id: str,
    token: str,
    in_place: bool = False,
    scope: Scope | None = None,
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
    pointed at the GitHub Copilot URL, then create the new one. With
    ``in_place`` (GitHub App rotation during a running turn) the first static
    credential is updated instead, so the slot is never empty, and any
    duplicates are deleted. A person's own `mcp_oauth` grant at that URL is
    left in place and nothing is created: their sign-in outranks the agent's
    PAT, and the slot is taken anyway.
    """
    scope = scope or Scope.legacy_host_authorized(
        call_site="daimon.core.mcp_vault:add_github_copilot_credential"
    )
    existing: list[str] = []
    async for cred in list_credentials(client, vault_id, scope=scope):
        if isinstance(cred.auth, BetaManagedAgentsEnvironmentVariableAuthResponse):
            continue
        if not same_server_url(cred.auth.mcp_server_url, GITHUB_COPILOT_MCP_URL):
            continue
        if cred.auth.type == "mcp_oauth":
            return
        existing.append(cred.id)
    if not in_place:
        # Collect first: deleting while the list paginates can skip an entry.
        for credential_id in existing:
            await delete_credential(client, vault_id, credential_id, scope=scope)
        existing = []
    if existing:
        await update_credential(
            client,
            vault_id,
            existing[0],
            {"auth": {"type": "static_bearer", "token": token}},
            scope=scope,
        )
        for duplicate_id in existing[1:]:
            await delete_credential(client, vault_id, duplicate_id, scope=scope)
    else:
        await store_credential(
            client,
            vault_id,
            {
                "auth": {
                    "type": "static_bearer",
                    "mcp_server_url": GITHUB_COPILOT_MCP_URL,
                    "token": token,
                }
            },
            scope=scope,
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
    scope: Scope | None = None,
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
    ``ensure_agent_mcp_vault``, keyed on ``(account_id, agent_id)`` —
    it shares the vault get-or-create race with that function and must
    serialize against it, not just against itself.
    """
    scope = scope or Scope.legacy_host_authorized(
        call_site="daimon.core.mcp_vault:add_external_mcp_credential"
    )
    display_name = f"daimon-mcp:{account_id}:{agent_id}"
    async with session_factory() as session:
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
                session=session,
                account_id=account_id,
                agent_id=agent_id,
                jwt_secret=jwt_secret,
                public_url=public_url,
                now=now,
                scope=scope,
            )

        # Collect first: deleting while the list paginates can skip an entry.
        stale = [
            cred.id
            async for cred in list_credentials(client, vault_id, scope=scope)
            if not isinstance(cred.auth, BetaManagedAgentsEnvironmentVariableAuthResponse)
            and same_server_url(cred.auth.mcp_server_url, mcp_server_url)
        ]
        for credential_id in stale:
            await delete_credential(client, vault_id, credential_id, scope=scope)

        await store_credential(
            client,
            vault_id,
            {
                "auth": {
                    "type": "static_bearer",
                    "mcp_server_url": mcp_server_url,
                    "token": token,
                }
            },
            scope=scope,
        )
        await session.commit()
