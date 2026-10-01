"""Pure JWT minting for daimon-mcp tokens.

Shared primitive between `daimon.core.mcp_vault.ensure_mcp_vault` and the
CLI's `daimon mcp mint-token` command — both need to sign, neither should
cross the core/adapter boundary to reach the FastMCP verifier.

No I/O, no globals. `now` is injected per guideline:architecture so tests
are deterministic and the function stays pure.

Server-side verification lives in `daimon.adapters.mcp.auth.verifier`
(FastMCP's `JWTVerifier`, which is authlib-backed). We don't verify here.

`mint_internal_mcp_token` is a v1.1 seam used by the headless routine runner
and the `/mcp-token mint` command. A future variant may
supersede it with a tenant-aware variant — keep the signature stable.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid

import jwt as pyjwt
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import (
    Action,
    AgentRef,
    Decision,
    Place,
    Subject,
    Surface,
    authorize,
    build_subject,
    build_turn_place,
)
from sqlalchemy.ext.asyncio import AsyncSession


def mint_jwt(
    *,
    account_id: uuid.UUID,
    secret: bytes,
    now: dt.datetime,
    agent_id: uuid.UUID | None = None,
    is_admin: bool = False,
    chat_agent_id: uuid.UUID | None = None,
    slack_turn_context_id: uuid.UUID | None = None,
) -> str:
    """Sign `{sub: <account_uuid>, iat: <unix ts>}` HS256 with `secret`.

    This is the Discord/CLI mint path. It intentionally NEVER emits the ``internal``
    claim, so a Discord vault token cannot be treated as a trusted internal token
    by the MCP admin gate (ADMIN-01/closes #162). Admin elevation for Discord tokens
    comes exclusively from the live DB ``role`` re-read by the verifier each turn.

    - ``sub`` is the string form of the account UUID (stable across principal renames).
    - ``iat`` is ``int(now.timestamp())`` (UTC assumed; caller owns tz).
    - No ``exp`` — role is resolved live from the DB on each request.
    - When ``agent_id`` is supplied, the optional ``"agent_id"`` claim is added
      Used by the MCP ``get_cli_token`` tool to mint per-service tokens
      scoped to the agent without trusting tool-supplied parameters.
    - ``chat_agent_id`` identifies the agent executing an ordinary chat session
      without selecting the restricted external agent-chat tool surface.
    - ``slack_turn_context_id`` is an execution grant used only by isolated Slack
      DM session credentials. Readers require the matching live tenant/account row.
    - When ``is_admin`` is ``True``, an ``"is_admin": True`` claim is added
      Omitted when ``False`` to keep non-admin tokens minimal. Note: the MCP
      admin gate only trusts ``is_admin`` when the token also carries ``internal=True``
      (minted only by ``mint_internal_mcp_token``), so a Discord vault token's baked
      ``is_admin`` claim alone never elevates a non-admin caller.
    """
    claims: dict[str, str | int | bool] = {
        "sub": str(account_id),
        "iat": int(now.timestamp()),
    }
    if agent_id is not None:
        claims["agent_id"] = str(agent_id)
    if chat_agent_id is not None:
        claims["chat_agent_id"] = str(chat_agent_id)
    if slack_turn_context_id is not None:
        claims["slack_turn_context_id"] = str(slack_turn_context_id)
    if is_admin:
        claims["is_admin"] = True
    return pyjwt.encode(claims, secret, algorithm="HS256")


def mint_internal_mcp_token(
    *,
    account_id: uuid.UUID,
    secret: bytes,
    now: dt.datetime,
) -> str:
    """Sign a token for headless (routine-driven) MCP calls.

    Claims: ``{sub: <account_uuid>, iat, is_admin: True, internal: True}``.
    Signed HS256 with ``secret``. ``sub`` is the stringified account UUID (stable
    across principal renames); ``iat`` is ``int(now.timestamp())``.

    ``is_admin: True`` is always minted per D-50b — headless/routine tokens run with
    admin privileges.

    ``internal: True`` is the trusted-token discriminator (ADMIN-02). The MCP admin
    gate keys on ``is_admin AND internal`` together: a Discord vault token minted by
    ``mint_jwt`` never carries ``internal``, so a stale pre-sweep Discord vault cred
    with ``is_admin=True`` and ``role=user`` is DENIED admin elevation at the gate.
    Only tokens minted here (CLI/scheduler/headless) carry both claims and can gain
    admin via the claim path.

    A future variant may supersede this with a tenant-aware
    variant — the signature is the seam, keep it stable. The
    ``/mcp-token mint`` reuses this helper directly.
    """
    return pyjwt.encode(
        {
            "sub": str(account_id),
            "iat": int(now.timestamp()),
            "is_admin": True,
            "internal": True,
        },
        secret,
        algorithm="HS256",
    )


async def mint_agent_mcp_token(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    label: str | None,
    secret: bytes,
    now: dt.datetime,
    ttl_days: int = 90,
    platform: str | None = None,
    channel_id: str | None = None,
) -> str:
    """Mint a long-lived, revocable, agent-scoped MCP JWT.

    Writes an mcp_tokens row (jti registry) then signs a token carrying
    exactly {sub, agent_id, jti, exp} per A1 — no role, no tenant_id.
    The verifier re-derives role and tenant from the DB on each request.

    `agent_id` is the derived per-agent UUID (A2, derive_agent_uuid output).
    It is stringified into both the row column and the `agent_id` claim.

    `now` and `secret` are injected per guideline:architecture — no globals,
    no datetime.now() inside core logic.

    `ttl_days` defaults to 90 days (long-lived, revoked via revoke_mcp_token
    rather than token rotation).

    `platform` and `channel_id` bind the token to the channel it was minted in
    (`coding_token_channel`). They live on the row only, never in the JWT, so
    the verifier reads the binding from the database like the revocation.
    """
    from daimon.core.stores.mcp_tokens import create_mcp_token_row

    assert (platform is None) == (channel_id is None), "a channel binding needs both"
    jti = uuid.uuid4()
    await create_mcp_token_row(
        session,
        jti=jti,
        account_id=account_id,
        tenant_id=tenant_id,
        agent_id=str(agent_id),
        label=label,
        created_at=now,
        platform=platform,
        channel_id=channel_id,
    )
    exp = int((now + dt.timedelta(days=ttl_days)).timestamp())
    return pyjwt.encode(
        {
            "sub": str(account_id),
            "agent_id": str(agent_id),
            "jti": str(jti),
            "exp": exp,
        },
        secret,
        algorithm="HS256",
    )


def coding_token_channel(
    policy: TenantAccessPolicy, *, agent: AgentRef, channel_id: str | None
) -> str | None:
    """The channel a coding-tool token minted in `channel_id` is bound to, or None.

    `channel_id` is where the panel was opened (a thread's parent channel).
    The token is bound there when the channel is sealed, or when the agent is
    pinned and the channel is inside its pin: those are the places where an
    agent key from outside every channel could not reach what the channel's
    own turns do. Anywhere else the token stays unbound, as before. The pin
    is decided as the key's own turns will be (`build_subject` with
    ``via_agent_key``, the channel as a `build_turn_place`).
    """
    if channel_id is None:
        return None
    if channel_id in policy.sealed_channel_ids:
        return channel_id
    pinned = any(name is not None and name in policy.agent_channel_pins for name in agent.names)
    if pinned and authorize(
        policy,
        subject=build_subject(is_admin=False, platform_user_id=None, via_agent_key=True),
        action=Action.RUN_AGENT,
        surface=Surface.AGENT_CHAT,
        agent=agent,
        place=build_turn_place(channel_id=channel_id, thread_id=None),
    ):
        return channel_id
    return None


def authorize_coding_token(
    policy: TenantAccessPolicy, *, subject: Subject, agent: AgentRef, channel_id: str | None
) -> tuple[Decision, str | None]:
    """Whether `subject` may mint a coding-tool token pressed in `channel_id`, and its binding.

    The binding is `coding_token_channel`'s; `authorize(MINT_CODING_TOKEN)`
    then decides the mint at that place, so a server admin mints as before and
    a channel admin only a token bound to a channel they administer.
    """
    bound_channel_id = coding_token_channel(policy, agent=agent, channel_id=channel_id)
    decision = authorize(
        policy,
        subject=subject,
        action=Action.MINT_CODING_TOKEN,
        surface=Surface.CONFIG,
        agent=agent,
        place=Place()
        if bound_channel_id is None
        else build_turn_place(channel_id=bound_channel_id, thread_id=None),
    )
    return decision, bound_channel_id


def token_jti(token: str) -> uuid.UUID:
    """The jti of a token this process just signed.

    Unverified on purpose: the caller minted it a statement earlier, and the
    registry row, not the signature, is the authority on revocation.
    """
    claims: dict[str, object] = pyjwt.decode(token, options={"verify_signature": False})
    raw = claims.get("jti")
    assert isinstance(raw, str), "a minted agent token always carries a jti claim"
    return uuid.UUID(raw)


def coding_tool_config(*, agent_name: str, public_url: str, jwt: str) -> tuple[str, str]:
    """The `claude mcp add` one-liner and the `.mcp.json` snippet for one agent token.

    The server key is `daimon-<agent-name>` so several agents share one config file.
    """
    key_name = f"daimon-{agent_name}"
    config = {key_name: {"url": public_url, "headers": {"Authorization": f"Bearer {jwt}"}}}
    cli = (
        f'claude mcp add --transport http "{key_name}" "{public_url}" '
        f'--header "Authorization: Bearer {jwt}"'
    )
    return cli, json.dumps(config, indent=2)
