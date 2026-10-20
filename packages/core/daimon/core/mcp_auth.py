"""JWT minting and registry checks for daimon-mcp caller credentials.

Shared primitive between `daimon.core.mcp_vault.ensure_mcp_vault` and the
CLI's `daimon mcp mint-token` command — both need to sign, neither should
cross the core/adapter boundary to reach the FastMCP verifier.

Signing functions are pure; registered mint functions use the injected session.
The injected `now` keeps lifetimes deterministic in tests. Short-lived remote
credentials are read back before provider exposure.

Server-side verification lives in `daimon.adapters.mcp.auth.verifier`
(FastMCP's `JWTVerifier`, which is authlib-backed). It owns tool authorization.

Historical internal credentials have no production minter and are rejected by
the verifier. Chat workers and CLI commands use registered mint functions.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import stat
import uuid
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path

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
from daimon.core.operator_tokens import OperatorScope
from daimon.core.permissions import agent_permissions, channel_rule
from daimon.core.stores.domain import McpTokenRow
from mux.contracts.ids import Scope
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def read_private_credential_bytes(path: Path) -> bytes:
    """Read bounded, explicitly owned credential bytes without following links."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.getuid()
        ):
            raise ValueError("credential file must be private and owned")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(16_385)
        if len(raw) > 16_384:
            raise ValueError("invalid credential")
        return raw
    finally:
        os.close(descriptor)


def read_private_credential(path: Path) -> str:
    """Read a bounded, explicitly owned bearer file without following links."""
    token = read_private_credential_bytes(path).decode().strip()
    if not token or any(ord(character) < 33 or ord(character) > 126 for character in token):
        raise ValueError("invalid credential")
    return token


async def resolve_agent_mcp_credential(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    path: Path,
    secret: bytes,
    scope: Scope,
    agent_id: uuid.UUID,
    destination: str,
    tools: tuple[str, ...],
    now: dt.datetime,
    chat_execution: bool,
) -> str:
    """Recheck registered caller authority and its exact remote catalogue before exposure."""
    from daimon.core.mcp_oauth.discovery import list_mcp_tools

    token = read_private_credential(path)
    await validate_agent_mcp_token(
        sessionmaker,
        token=token,
        secret=secret,
        scope=scope,
        agent_id=agent_id,
        now=now,
        chat_execution=chat_execution,
    )
    if await list_mcp_tools(destination, token) != frozenset(tools):
        raise ValueError("target MCP catalogue differs from captured source")
    return token


def mint_jwt(
    *,
    account_id: uuid.UUID,
    secret: bytes,
    now: dt.datetime,
    agent_id: uuid.UUID | None = None,
    is_admin: bool = False,
    jti: uuid.UUID | None = None,
    expires_at: dt.datetime | None = None,
    chat_agent_id: uuid.UUID | None = None,
    slack_turn_context_id: uuid.UUID | None = None,
) -> str:
    """Sign `{sub: <account_uuid>, iat: <unix ts>}` HS256 with `secret`.

    This low-level compatibility signer never emits the ``internal`` claim.
    Registered chat minting supplies both a jti and expiry, so a vault token
    cannot be treated as a trusted internal token
    by the MCP admin gate. Admin elevation for Discord tokens
    comes exclusively from the live DB ``role`` re-read by the verifier each turn.

    - ``sub`` is the string form of the account UUID (stable across principal renames).
    - ``iat`` is ``int(now.timestamp())`` (UTC assumed; caller owns tz).
    - Registry-backed callers supply ``jti`` and ``expires_at``; vault chat always does.
    - Role is resolved live from the DB on each request.
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
      (historical unregistered internal tokens are refused by the verifier), so a
      Discord vault token's baked
      ``is_admin`` claim alone never elevates a non-admin caller.
    """
    claims: dict[str, str | int | bool] = {
        "sub": str(account_id),
        "iat": int(now.timestamp()),
    }
    if (jti is None) != (expires_at is None):
        raise ValueError("registered tokens require both jti and expiry")
    if jti is not None and expires_at is not None:
        if expires_at <= now:
            raise ValueError("token expiry must be in the future")
        claims["jti"] = str(jti)
        claims["exp"] = int(expires_at.timestamp())
    if agent_id is not None:
        claims["agent_id"] = str(agent_id)
    if chat_agent_id is not None:
        claims["chat_agent_id"] = str(chat_agent_id)
    if slack_turn_context_id is not None:
        claims["slack_turn_context_id"] = str(slack_turn_context_id)
    if is_admin:
        claims["is_admin"] = True
    return pyjwt.encode(claims, secret, algorithm="HS256")


# One hour covers the existing 45-minute turn ceiling plus vault propagation.
CHAT_TOKEN_TTL = dt.timedelta(hours=1)
CHAT_TOKEN_REFRESH_MARGIN = dt.timedelta(minutes=50)
CHAT_TOKEN_LABEL_PREFIX = "vault-chat:"
PANEL_TOKEN_LABEL = "billing-panel"
EPHEMERAL_TOKEN_RETENTION = dt.timedelta(days=1)


def chat_token_label(jti: uuid.UUID) -> str:
    """Bind the registry discriminator to a generated id, leaving user labels free."""
    return f"{CHAT_TOKEN_LABEL_PREFIX}{jti}"


async def mint_chat_mcp_token(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    agent_id: uuid.UUID,
    secret: bytes,
    now: dt.datetime,
    slack_turn_context_id: uuid.UUID | None = None,
    ttl: dt.timedelta = CHAT_TOKEN_TTL,
) -> tuple[str, uuid.UUID, dt.datetime]:
    """Register one expiring ordinary-chat credential with the existing token owner.

    An agent-kind row binds the executing agent, but the JWT deliberately uses
    chat_agent_id rather than the restricted external agent-chat agent_id claim.
    Old tokens remain independently revocable until their bounded expiry; rotating
    a shared vault does not revoke credentials of an already-running turn.
    """
    from daimon.core.stores.accounts import get_account_with_tenant
    from daimon.core.stores.mcp_tokens import create_mcp_token_row

    identity = await get_account_with_tenant(session, account_id=account_id)
    if identity is None:
        raise ValueError("chat credential requires a live account")
    jti = uuid.uuid4()
    if ttl <= dt.timedelta(0):
        raise ValueError("chat token TTL must be positive")
    expires_at = now + ttl
    await create_mcp_token_row(
        session,
        jti=jti,
        account_id=account_id,
        tenant_id=identity.tenant_id,
        agent_id=str(agent_id),
        label=chat_token_label(jti),
        created_at=now,
        expires_at=expires_at,
    )
    token = mint_jwt(
        account_id=account_id,
        chat_agent_id=agent_id,
        secret=secret,
        now=now,
        slack_turn_context_id=slack_turn_context_id,
        jti=jti,
        expires_at=expires_at,
    )
    return token, jti, expires_at


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
    chat_execution: bool = False,
) -> str:
    """Mint a long-lived, revocable, agent-scoped MCP JWT.

    Writes an mcp_tokens row (jti registry) then signs a token carrying
    exactly {sub, agent_id, jti, exp} — no role, no tenant_id. Explicit
    ``chat_execution`` instead signs ``chat_agent_id`` and requires a TTL of
    at most 900 seconds, retaining the ordinary source chat tool surface.
    Existing external agent credentials retain ``agent_id`` and their narrower
    surface; callers must validate the expected mode before provider exposure.
    The verifier re-derives role and tenant from the DB on each request.

    `agent_id` is the derived per-agent UUID (derive_agent_uuid output).
    It is stringified into both the row column and the `agent_id` claim.

    `now` and `secret` are injected per guideline:architecture — no globals,
    no datetime.now() inside core logic.

    `ttl_days` defaults to 90 days (long-lived, revoked via revoke_mcp_token
    rather than token rotation).

    `platform` and `channel_id` bind the token to the channel it was minted in
    (`coding_token_channel`). They live on the row only, never in the JWT, so
    the verifier reads the binding from the database like the revocation.
    """
    # Registered chat execution retains the ordinary chat surface. External
    # agent keys keep their existing restricted agent-chat surface. The signed
    # identity claim selects the mode; both modes use the same registry owner.
    from daimon.core.stores.mcp_tokens import create_mcp_token_row

    if chat_execution and not 0 < ttl_days * 86400 <= 900:
        raise ValueError("registered chat execution requires a TTL of at most 900 seconds")
    assert (platform is None) == (channel_id is None), "a channel binding needs both"
    jti = uuid.uuid4()
    expires_at = now + dt.timedelta(days=ttl_days)
    await create_mcp_token_row(
        session,
        jti=jti,
        account_id=account_id,
        tenant_id=tenant_id,
        agent_id=str(agent_id),
        label=label,
        created_at=now,
        expires_at=expires_at,
        platform=platform,
        channel_id=channel_id,
    )
    return pyjwt.encode(
        {
            "sub": str(account_id),
            "chat_agent_id" if chat_execution else "agent_id": str(agent_id),
            "jti": str(jti),
            "exp": int(expires_at.timestamp()),
        },
        secret,
        algorithm="HS256",
    )


def registered_agent_claims_match(
    row: McpTokenRow,
    claims: Mapping[str, object],
    *,
    now: dt.datetime,
    chat_execution: bool,
    max_ttl_s: int = 900,
) -> bool:
    """Share the remote credential's exact mint shape with server admission.

    Vault chat credentials have their own label, issuance claim and lifetime.
    Remote chat credentials retain the mint owner's four-claim template and
    bounded registry lifetime; an arbitrary label cannot select the vault mode.
    """
    identity_claim = "chat_agent_id" if chat_execution else "agent_id"
    expiry = claims.get("exp")
    return (
        0 < max_ttl_s <= 900
        and set(claims) == {"sub", identity_claim, "jti", "exp"}
        and claims["sub"] == str(row.account_id)
        and claims[identity_claim] == row.agent_id
        and claims["jti"] == str(row.jti)
        and type(expiry) is int
        and row.expires_at is not None
        and expiry == int(row.expires_at.timestamp())
        and now.timestamp() < expiry <= now.timestamp() + max_ttl_s
        and row.created_at <= now
        and (not chat_execution or 0 < (row.expires_at - row.created_at).total_seconds() <= 900)
    )


async def validate_agent_mcp_token(
    sessions: async_sessionmaker[AsyncSession],
    *,
    token: str,
    secret: bytes,
    scope: Scope,
    agent_id: uuid.UUID,
    now: dt.datetime,
    max_ttl_s: int = 900,
    chat_execution: bool = False,
) -> None:
    """Check a minted caller-bound token before exposing it to a remote provider.

    This checks the mint owner's own claims and registry facts; the MCP server
    still owns tool authorization. No provider id is substituted for the
    canonical Daimon agent identity, and a bearer never becomes a stored fact.
    """
    from daimon.core.stores.accounts import get_account_with_tenant
    from daimon.core.stores.mcp_tokens import get_mcp_token

    identity_claim = "chat_agent_id" if chat_execution else "agent_id"
    try:
        claims = pyjwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            options={
                "verify_exp": False,
                "verify_iat": False,
                "verify_nbf": False,
                "require": ["sub", identity_claim, "jti", "exp"],
            },
        )
        if (
            scope.is_platform
            or scope.is_legacy_host_authorized
            or claims["sub"] != scope.account_id
            or claims[identity_claim] != str(agent_id)
        ):
            raise ValueError("invalid scoped agent token")
        jti = uuid.UUID(claims["jti"])
        async with sessions() as db:
            identity = await get_account_with_tenant(db, account_id=uuid.UUID(scope.account_id))
            row = await get_mcp_token(db, jti=jti)
        if (
            identity is None
            or str(identity.tenant_id) != scope.tenant_id
            or identity.role.value != "user"
            or row is None
            or row.kind != "agent"
            or row.scopes
            or str(row.account_id) != scope.account_id
            or str(row.tenant_id) != scope.tenant_id
            or row.agent_id != str(agent_id)
            or row.revoked_at is not None
            or not registered_agent_claims_match(
                row, claims, now=now, chat_execution=chat_execution, max_ttl_s=max_ttl_s
            )
        ):
            raise ValueError("invalid scoped agent token")
    except (pyjwt.InvalidTokenError, KeyError, TypeError, ValueError):
        raise ValueError("invalid scoped agent token") from None


async def mint_operator_mcp_token(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    tenant_id: uuid.UUID,
    scopes: frozenset[OperatorScope],
    label: str | None,
    secret: bytes,
    now: dt.datetime,
    ttl_days: int,
    max_issued_usd: Decimal | None = None,
) -> str:
    """Mint a scoped operator token for one server admin's account.

    Claims are ``{sub, jti, exp, kind: "operator"}``. The scopes live only on
    the row, which the verifier reads on every request, so they can be
    narrowed or the token revoked without re-minting. The caller checks that
    the account is a server admin; the verifier re-checks it on every call.
    """
    from daimon.core.stores.mcp_tokens import create_mcp_token_row

    jti = uuid.uuid4()
    expires_at = now + dt.timedelta(days=ttl_days)
    await create_mcp_token_row(
        session,
        jti=jti,
        account_id=account_id,
        tenant_id=tenant_id,
        agent_id=None,
        kind="operator",
        scopes=scopes,
        label=label,
        created_at=now,
        expires_at=expires_at,
        max_issued_usd=max_issued_usd,
    )
    return pyjwt.encode(
        {
            "sub": str(account_id),
            "jti": str(jti),
            "exp": int(expires_at.timestamp()),
            "kind": "operator",
        },
        secret,
        algorithm="HS256",
    )


async def mint_cli_mcp_token(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    tenant_id: uuid.UUID,
    secret: bytes,
    now: dt.datetime,
    ttl_days: int,
) -> str:
    """Mint the CLI's own token: ``{sub, iat, jti, exp, kind: "cli"}`` plus a row.

    Unlike the jti-less tokens `mint_jwt` signs, this one expires and
    `daimon mcp revoke-token` can revoke it.
    """
    return await _mint_account_mcp_token(
        session,
        account_id=account_id,
        tenant_id=tenant_id,
        secret=secret,
        now=now,
        expires_at=now + dt.timedelta(days=ttl_days),
        label=None,
    )


async def mint_panel_mcp_token(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    secret: bytes,
    now: dt.datetime,
) -> str:
    """Register a ten-minute non-admin account token for the checkout HTTP hop."""
    from daimon.core.stores.accounts import get_account_with_tenant

    identity = await get_account_with_tenant(session, account_id=account_id)
    if identity is None:
        raise ValueError("checkout requires a live account")
    return await _mint_account_mcp_token(
        session,
        account_id=account_id,
        tenant_id=identity.tenant_id,
        secret=secret,
        now=now,
        expires_at=now + dt.timedelta(minutes=10),
        label=PANEL_TOKEN_LABEL,
    )


async def _mint_account_mcp_token(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    tenant_id: uuid.UUID,
    secret: bytes,
    now: dt.datetime,
    expires_at: dt.datetime,
    label: str | None,
) -> str:
    from daimon.core.stores.mcp_tokens import create_mcp_token_row

    jti = uuid.uuid4()
    await create_mcp_token_row(
        session,
        jti=jti,
        account_id=account_id,
        tenant_id=tenant_id,
        agent_id=None,
        kind="cli",
        label=label,
        created_at=now,
        expires_at=expires_at,
    )
    return pyjwt.encode(
        {
            "sub": str(account_id),
            "iat": int(now.timestamp()),
            "jti": str(jti),
            "exp": int(expires_at.timestamp()),
            "kind": "cli",
        },
        secret,
        algorithm="HS256",
    )


def coding_token_channel(
    policy: TenantAccessPolicy, *, agent: AgentRef, channel_id: str | None
) -> str | None:
    """The channel a coding-tool token minted in `channel_id` is bound to, or None.

    `channel_id` is where the panel was opened (a thread's parent channel).
    The token is bound there when the channel's readers are limited, or when
    the agent has a rule and the channel is inside it: those are the places
    where an agent key from outside every channel could not reach what the
    channel's own turns do. Anywhere else the token stays unbound. The rule
    is decided as the key's own turns will be (`build_subject` with
    ``via_agent_key``, the channel as a `build_turn_place`).
    """
    if channel_id is None:
        return None
    if channel_rule(policy, channel_id).readers != "any":
        return channel_id
    ruled = bool(agent_permissions(policy, agent.names).runs_in)
    if ruled and authorize(
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
    then decides the mint at that place. A server admin may mint the token.
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
