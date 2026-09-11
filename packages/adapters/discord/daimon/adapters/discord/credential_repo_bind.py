"""Click-time authorization and clone-credential resolution for the
chat-initiated repo bind.

This module holds the shell halves of the chat-initiated repo bind: the
click-time gate below, and the clone-credential resolution the write itself
needs (`resolve_repo_binding_credential`).

`refuse_if_shared_and_not_admin_for_request` re-derives the panel gate's
(`refuse_if_shared_and_not_admin`) decision from primitives rather than
reusing it: a `credential_requests` row carries only a derived agent uuid,
not the roster-entry value the panel gate expects, so there is nothing to
hand it. The branch ORDER below is load-bearing and mirrors the panel
gate's order exactly — reordering it changes who can bind a repo to what.

The gate is called from two different response states and must work
correctly from both:

1. Before anything is acked, as a pre-filter ahead of opening the modal.
   The admin branch precedes every MA and database read, so an admin still
   opens the modal well inside the interaction budget. A refused member pays
   the MA and database reads before any response — which is the point of
   the pre-filter: a member who was always going to be refused is never
   asked to paste a token into a form that gets thrown away.
2. After the caller acks with a thinking response, immediately before the
   single-use consume — this is the actual authorization boundary. The
   pre-filter above does not replace it.

Because of (1), this gate must **never** ack the interaction on its own —
opening a modal is only possible from an un-acked interaction, so the
pre-filter has to finish its reads and either fall through or answer on
whichever half of `interaction.response.is_done()` is currently live,
without itself changing that state. That is why every ephemeral send below
routes through that same split rather than a fixed response call, and why
this module works unmodified from both call sites.

The pre-filter's caller bounds this gate with a timeout and opens the modal
anyway if it expires. That is sound only because call site (2) repeats every
check below before the write, so it is the real boundary — it is not a
"retry recovers it" situation: the underlying read paginates the operator's
entire MA org, so a slow read costs the same on every click, and a retry
fails identically. The timeout and its full rationale live with the
pre-filter itself.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Final

import httpx
import structlog
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.adapters.discord.agent_setup.write import load_agent_inline_pat, store_inline_pat
from daimon.adapters.discord.checks import is_guild_admin
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.defaults.ma_index import list_agents_by_tenant
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED
from daimon.core.errors import DaimonError
from daimon.core.github_repo_auth import normalize_owner_repo
from daimon.core.github_visibility import is_public_repo, pat_can_access_repo
from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
from daimon.core.stores.domain import RepoAccessProof
from daimon.core.stores.scoped_config_read import is_agent_reachable_in_tenant

import discord

log = structlog.get_logger()

_SHARED_AGENT_MESSAGE: Final[str] = (
    "Changing this shared agent's working repo needs a server admin (Manage Server). "
    "Ask a server admin to set the working repo in `/agent-setup`."
)


_AGENT_GONE_MESSAGE: Final[str] = (
    "That agent no longer exists — ask again and a fresh request will be posted."
)

_WRONG_GUILD_MESSAGE: Final[str] = (
    "This request isn't for this server — ask again from the server it was posted in."
)


async def resolve_ma_agent_for_uuid(
    client: AsyncAnthropic, *, tenant_id: uuid.UUID, agent_id: uuid.UUID
) -> BetaManagedAgentsAgent | None:
    """Reverse-resolve a derived agent uuid back to the MA agent it came from.

    `derive_agent_uuid` is a one-way `uuid5` over `(tenant_id, ma_agent_id)`,
    so there is no direct lookup from the derived uuid a `credential_requests`
    row carries back to the MA agent id. The only available resolution is
    forward: list every non-archived MA agent tagged with this tenant and
    recompute the derived uuid for each until one matches. Returns `None`
    when nothing matches — the agent was archived or deleted since the row
    was minted.
    """
    agents = await list_agents_by_tenant(client, tenant_id=tenant_id)
    for agent in agents:
        if derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=str(agent.id)) == agent_id:
            return agent
    return None


async def _send_ephemeral(interaction: discord.Interaction, content: str) -> None:  # type: ignore[type-arg]  # discord.Interaction default generic arg; only response/followup are used
    if interaction.response.is_done():
        await interaction.followup.send(content, ephemeral=True)
    else:
        await interaction.response.send_message(content, ephemeral=True)


async def refuse_if_shared_and_not_admin_for_request(
    interaction: discord.Interaction,  # type: ignore[type-arg]  # discord.Interaction default generic arg; only response/followup/guild_id/user are used
    *,
    runtime: DiscordRuntime,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
) -> bool:
    """Click-time re-check for the chat-initiated repo-bind write.

    Returns True when the caller must return immediately (the write is
    refused); False when the write may proceed. Order, each short-circuiting:

    1. Tenant re-derivation. Re-derive the tenant from the live interaction's
       guild and compare it against the `tenant_id` argument the row was
       minted with. This is pure and costs no I/O, so it precedes even the
       admin check — without it, the tenant half of the decision would be
       taken on trust from a row written at some earlier time rather than
       re-derived from live state. Defense in depth: the posted button lives
       in the channel the mint named, so a cross-guild click is not
       reachable through normal use; this guard exists so it stays
       unreachable by construction rather than by luck.
    2. A live guild admin (`is_guild_admin`) -> allow, before any MA call and
       before any database read. This ordering is what keeps an admin able
       to bind a repo to the seeded agent — the first-run onboarding step
       this whole path exists to enable.
    3. Resolve the MA agent the row's derived uuid points at. No match ->
       refuse, fail closed (the agent may have been archived or deleted
       since the row was minted).
    4. The resolved agent is defaults-managed
       (`metadata.get(MA_METADATA_KEY_MANAGED) == "true"`) -> refuse; it
       belongs to the deployment and every member of the install shares it.
    5. Otherwise, read reachability fresh from the database and refuse when
       the agent currently resolves for some channel or the workspace.
    6. Otherwise allow.

    Steps 2, 4, and 5 are `refuse_if_shared_and_not_admin`'s steps 2, 3, and
    4 in the same order, driven off primitives instead of a roster entry.

    The two shared-agent refusals (4 and 5) carry the same message on
    purpose, exactly as in the panel gate: which limb fired is not
    actionable, and the fix is the same either way — ask an admin, or bind
    to a private agent instead.

    This function never acks the interaction — see the module docstring.
    """
    if interaction.guild_id is None or (
        derive_tenant_uuid(platform="discord", workspace_id=str(interaction.guild_id)) != tenant_id
    ):
        log.warning(
            "credential_repo_bind.wrong_guild",
            request_tenant_id=str(tenant_id),
            interaction_guild_id=interaction.guild_id,
        )
        await _send_ephemeral(interaction, _WRONG_GUILD_MESSAGE)
        return True
    if is_guild_admin(interaction):  # pyright: ignore[reportArgumentType]  # discord.Interaction vs Interaction[commands.Bot]; is_guild_admin only reads user/guild
        return False
    agent = await resolve_ma_agent_for_uuid(
        runtime.anthropic, tenant_id=tenant_id, agent_id=agent_id
    )
    if agent is None:
        log.warning(
            "credential_repo_bind.agent_gone",
            tenant_id=str(tenant_id),
            agent_id=str(agent_id),
        )
        await _send_ephemeral(interaction, _AGENT_GONE_MESSAGE)
        return True
    if agent.metadata.get(MA_METADATA_KEY_MANAGED) == "true":
        await _send_ephemeral(interaction, _SHARED_AGENT_MESSAGE)
        return True
    async with runtime.sessionmaker() as session:
        reachable = await is_agent_reachable_in_tenant(
            session,
            tenant_id=tenant_id,
            agent_name=agent.name,
            default=runtime.deployment_default,
        )
    if reachable:
        await _send_ephemeral(interaction, _SHARED_AGENT_MESSAGE)
        return True
    return False


async def resolve_repo_binding_credential(
    runtime: DiscordRuntime,
    http_client: httpx.AsyncClient,
    *,
    agent_id: uuid.UUID,
    account_id: uuid.UUID,
    repo_url: str,
    pasted_pat: str | None,
    now: datetime,
) -> tuple[str, RepoAccessProof]:
    """Resolve the clone credential for a chat-initiated repo bind.

    Mirrors `agent_setup.modals.RepoAuthModal`'s repo branch step for step —
    same order, same messages, same precedence — because reusing the panel's
    resolution rather than writing a second one is what closes the
    ungated-repo-bind defect this decision guards against. There is
    deliberately no GitHub App tier here: an App installation is keyed by the
    repo, not by the tenant doing this bind, so its coverage proves nothing
    about whether *this* binder may read the repo (the panel dropped this
    same tier for the same reason, and re-adding it here would reopen the
    cross-tenant private-repo read it closed).

    Order, each short-circuiting:

    1. A non-empty `pasted_pat` must grant access to `repo_url` itself
       (`pat_can_access_repo`) before it is stored — a token that can't read
       the repo it's meant to clone must never reach `store_inline_pat`.
    2. Otherwise, a PAT already stored for this agent
       (`load_agent_inline_pat`) has unconditional precedence in
       `select_clone_auth`'s tier ordering, so — if one exists — it, not any
       other coverage, is what will clone this repo. It is re-verified
       against `repo_url` rather than trusted blindly: falling through to
       the public probe on a mismatch would let a "successful" bind produce
       a broken clone later.
    3. Otherwise, `repo_url` must be verifiably public (`is_public_repo`):
       the only credential that will ever clone an unauthenticated binding
       is the operator's public-read-only fallback PAT, so an unpinned bind
       can only be trusted against a repo anyone can read.

    Raises `DaimonError` — never a sentinel — before any write when the
    presented credential does not clear the repo it names. `http_client` is
    injected; this function carries no `discord` type in its signature, so
    it can be driven directly by a test with no Discord interaction at all.
    """
    owner_repo = normalize_owner_repo(repo_url)
    pat = (pasted_pat or "").strip()
    if pat:
        has_access = await pat_can_access_repo(http_client, owner_repo=owner_repo, pat=pat)
        if not has_access:
            raise DaimonError(
                "That token can't access this repo (or the repo doesn't "
                "exist). Paste a PAT that has access, or connect GitHub."
            )
        ma_secret_ref = await store_inline_pat(
            runtime, account_id=account_id, agent_id=agent_id, plaintext_pat=pat
        )
        return ma_secret_ref, RepoAccessProof(kind="pat", at=now, account_id=account_id)

    existing_pat = await load_agent_inline_pat(runtime, agent_id=agent_id)
    if existing_pat is not None:
        covers_new_repo = await pat_can_access_repo(
            http_client, owner_repo=owner_repo, pat=existing_pat
        )
        if not covers_new_repo:
            raise DaimonError(
                "This agent already has a stored GitHub token that can't "
                "access this repo. Paste a token that can, or clear the "
                "stored one, then bind again."
            )
        return f"inline-pat:{agent_id}", RepoAccessProof(kind="pat", at=now, account_id=account_id)

    public = await is_public_repo(http_client, owner_repo=owner_repo)
    if not public:
        raise DaimonError(
            "This repo isn't publicly readable (it's private, or it "
            "doesn't exist) — paste a GitHub token that can read it to "
            "bind it."
        )
    return "anon:", RepoAccessProof(kind="public", at=now, account_id=account_id)
