"""Connect a coding agent to a Daimon agent with a pasted prompt and a single-use code.

A person with coding-key standing on an agent asks for a batch of codes, one per
coding tool, and pastes one tool's prompt into that tool. The prompt carries the
code, never a key: the tool's agent trades the code for the key with one shell
command that writes it into its config, so the key never enters the chat. The
code works once, within ten minutes, and the first one used kills the rest of
its batch. A new batch can replace the previous one the same person made.

A claim has no signed-in person, only the code, so it re-runs what a key made on
screen needs for the person who made it: the agent live and not archived, its
setup theirs to see, the access policy and their standing, as the verifier does
on every request. A code that is unknown, used, expired or whose maker has lost
any of that is refused alike, and the caller cannot tell which.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets
import uuid
from collections.abc import Callable

from daimon.core.agent_editing import AgentActor, AgentEditDeps, AgentEditError
from daimon.core.agent_target import load_target, require_setup
from daimon.core.authz import Subject, Surface
from daimon.core.coding_key_prompts import (
    CODE_TTL_MINUTES,
    CODING_CLIENTS,
    client_name,
    connect_prompt,
    server_slug,
)
from daimon.core.coding_keys import (
    authorize_coding_mint,
    coding_key_access,
)
from daimon.core.mcp_auth import coding_key_ttl_days, mint_agent_mcp_token, token_jti
from daimon.core.stores import coding_key_claims as claims_store
from daimon.core.stores import web_audit
from daimon.core.stores.accounts import get_account_with_tenant
from daimon.core.stores.domain import Role
from pydantic import BaseModel

CODE_TTL = dt.timedelta(minutes=CODE_TTL_MINUTES)

#: The audit row of a claim, the same action as a key made on screen.
CLAIM_AUDIT_ACTION = "Created coding key"


class ConnectPrompt(BaseModel):
    id: str
    name: str
    prompt: str


class ConnectBatch(BaseModel):
    """The prompts of one batch: each holds a code, shown here and held nowhere else."""

    batch: uuid.UUID
    expires_at: dt.datetime
    prompts: list[ConnectPrompt]


class ConnectStatus(BaseModel):
    """Whether one of the batch's codes was claimed, and for which tool."""

    connected: bool
    client: str | None


def hash_code(code: str) -> str:
    """A code at rest. The code is 192 random bits, so a plain digest is enough."""
    return hashlib.sha256(code.encode()).hexdigest()


def random_code() -> str:
    return secrets.token_urlsafe(24)


async def mint_connect_batch(
    deps: AgentEditDeps,
    *,
    actor: AgentActor,
    ma_agent_id: str,
    claim_url: str,
    now: dt.datetime,
    replaces: uuid.UUID | None = None,
    new_code: Callable[[], str] = random_code,
    new_batch: Callable[[], uuid.UUID] = uuid.uuid4,
) -> ConnectBatch:
    """Make one code per coding tool for the actor on this agent, as their prompts.

    Needs what a key made on screen needs: an admin or the agent's owner, whose key
    would pass the verifier. `replaces` is the actor's earlier batch for this agent,
    whose unused codes die with this call, so re-minting while a panel is open
    leaves one live batch.
    """
    target, _bound = await authorize_coding_mint(deps, actor=actor, ma_agent_id=ma_agent_id)
    if deps.public_url is None:
        raise AgentEditError("Coding keys aren't set up on this install.", kind="unavailable")
    slug = server_slug(target.agent.name, ma_agent_id)
    codes = {client: new_code() for client in CODING_CLIENTS}
    batch = new_batch()
    expires_at = now + CODE_TTL
    async with deps.session_factory() as session, session.begin():
        if replaces is not None:
            # Lock before killing, as a claim does, so the two take the batch's rows in
            # one order and cannot deadlock.
            await claims_store.lock_batch(session, batch=replaces)
            await claims_store.kill_unused_batch(
                session,
                batch=replaces,
                tenant_id=actor.tenant_id,
                account_id=actor.account_id,
                agent_id=target.agent_id,
                now=now,
            )
        await claims_store.insert_claims(
            session,
            tenant_id=actor.tenant_id,
            account_id=actor.account_id,
            agent_id=target.agent_id,
            ma_agent_id=ma_agent_id,
            agent_name=target.agent.name,
            batch=batch,
            created_at=now,
            expires_at=expires_at,
            code_hashes={client: hash_code(code) for client, code in codes.items()},
        )
    return ConnectBatch(
        batch=batch,
        expires_at=expires_at,
        prompts=[
            ConnectPrompt(
                id=client,
                name=client_name(client),
                prompt=connect_prompt(
                    client,
                    slug=slug,
                    public_url=deps.public_url,
                    claim_url=claim_url,
                    code=code,
                ),
            )
            for client, code in codes.items()
        ],
    )


async def connect_status(
    deps: AgentEditDeps, *, actor: AgentActor, ma_agent_id: str, batch: uuid.UUID
) -> ConnectStatus:
    """Whether the actor's batch for this agent has been claimed; not found for anyone else's."""
    target = await load_target(deps, actor=actor, ma_agent_id=ma_agent_id)
    require_setup(target, "Coding keys")
    if coding_key_access(is_admin=actor.subject.is_admin, owns_agent=target.owned) is None:
        raise AgentEditError("No such batch.", kind="not_found")
    async with deps.session_factory() as session:
        rows = await claims_store.batch_claims(
            session,
            batch=batch,
            tenant_id=actor.tenant_id,
            account_id=actor.account_id,
            agent_id=target.agent_id,
        )
    if not rows:
        raise AgentEditError("No such batch.", kind="not_found")
    claimed = next((row for row in rows if row.jti is not None), None)
    return ConnectStatus(
        connected=claimed is not None, client=None if claimed is None else claimed.client
    )


async def claim_coding_key(
    deps: AgentEditDeps, *, code: str, secret: bytes, now: dt.datetime
) -> str | None:
    """Trade a code for the coding key it stands for; None when it cannot be traded.

    What made the code is checked again now, as a key made on screen is: the maker
    must still be allowed to make one for this agent (`authorize_coding_mint`). That
    reads the live agent, so it runs before the batch is locked and no row lock is
    held across it. The code's whole batch is then row-locked before anything is
    decided, so two claims racing in one batch cannot both mint: the second waits,
    then finds its code killed. The key is made as `mint_coding_key` makes it
    (purpose "coding", a lifetime by the maker's role).
    """
    async with deps.session_factory() as session:
        seen = await claims_store.find_claim_by_hash(session, code_hash=hash_code(code))
        if seen is None or seen.used_at is not None or seen.expires_at <= now:
            return None
        identity = await get_account_with_tenant(session, account_id=seen.account_id)
    if identity is None or identity.tenant_id != seen.tenant_id:
        return None
    is_admin = identity.role is Role.ADMIN
    actor = AgentActor(
        tenant_id=seen.tenant_id,
        account_id=seen.account_id,
        subject=Subject(is_admin=is_admin, platform_user_id=str(seen.account_id)),
        surface=Surface.WEB,
    )
    try:
        # `_bound` is None on web (no channel binding), as for a key made on screen.
        target, _bound = await authorize_coding_mint(
            deps, actor=actor, ma_agent_id=seen.ma_agent_id
        )
    except AgentEditError:
        return None
    if target.agent_id != seen.agent_id:
        return None
    async with deps.session_factory() as session, session.begin():
        locked = await claims_store.lock_batch(session, batch=seen.batch)
        claim = next((row for row in locked if row.id == seen.id), None)
        if claim is None or claim.used_at is not None or claim.expires_at <= now:
            return None
        token = await mint_agent_mcp_token(
            session,
            account_id=claim.account_id,
            tenant_id=claim.tenant_id,
            agent_id=claim.agent_id,
            label=claim.agent_name,
            secret=secret,
            now=now,
            ttl_days=coding_key_ttl_days(Subject(is_admin=is_admin)),
            purpose="coding",
            client=claim.client,
        )
        await claims_store.mark_claimed(session, claim_id=claim.id, jti=token_jti(token), now=now)
        await claims_store.kill_batch(session, batch=claim.batch, now=now, except_id=claim.id)
        await web_audit.record(
            session,
            tenant_id=claim.tenant_id,
            actor_account_id=claim.account_id,
            action=CLAIM_AUDIT_ACTION,
            target=claim.agent_name,
            detail=client_name(claim.client),
            at=now,
        )
    return token
