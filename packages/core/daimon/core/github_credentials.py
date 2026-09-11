"""GitHub PAT encryption + three-tier resolver.

Encryption: MultiFernet wrapping a list of Fernet keys. v1 ships
a length-1 tuple; rotation = prepend a new key.

Resolver: get_pat(principal_id, agent_id=None) -> str | None. Cascade:
- agent_id given: tier-1 overlay ONLY. If no overlay row -> tier-3 (below).
  get_pat(agent_id=X) never falls back to the principal-default credential.
- agent_id=None: tier-2 principal-default credential -> tier-3 (below).
- tier-3 (opt-in, both tiers above exhausted): the operator-wide service
  default. Gated by two keyword-only parameters that both must be set —
  `allow_service_default` is the authorization, `fallback_pat` is the
  payload; neither alone does anything. This keeps the default behavior
  (both params at their default) identical to the old two-tier cascade, so
  no future caller can silently inherit the shared operator secret by
  omission. A resolved real credential (tier-1 or tier-2) always returns
  before this tier is reached, so the fallback can never shadow one.

  Three callers must NEVER opt into this tier: the agent-fork copy path
  (would persist the shared token as a per-agent DB row plus a binding), the
  session Copilot vault mirror and clone-token resolver (the shared token
  must never beat a short-lived per-repo GitHub App installation token, nor
  land in a per-agent MA vault credential), and the MCP self-edit
  repo-binding write (uploads the minted token as a durable MA vault
  credential).

No try/except — exceptions propagate. None means 'not found', NEVER
'something broke'.
"""

from __future__ import annotations

import uuid

from cryptography.fernet import Fernet, MultiFernet
from daimon.core.stores import agent_github_binding as binding_store
from daimon.core.stores import github_credentials as cred_store
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def build_multifernet(keys: tuple[str, ...]) -> MultiFernet:
    """Construct MultiFernet from a tuple of base64-urlsafe 32-byte keys.

    First key is used for encryption; all keys are tried for decryption (forward
    compatible with rotation). Raises ValueError at construction if any key is
    not a valid Fernet key — surfaces config errors at boot rather than on first
    OAuth callback (Pitfall 2 in RESEARCH.md).
    """
    if not keys:
        raise ValueError(
            "settings.crypto.keys is empty — at least one Fernet key is required "
            "for GitHub OAuth. Generate one with `cryptography.fernet.Fernet.generate_key()`."
        )
    return MultiFernet([Fernet(k.encode("utf-8")) for k in keys])


def encrypt_token(fernet: MultiFernet, plaintext: str) -> bytes:
    return fernet.encrypt(plaintext.encode("utf-8"))


def decrypt_token(fernet: MultiFernet, ciphertext: bytes | memoryview) -> str:
    # asyncpg may return BYTEA as memoryview; Fernet wants bytes (Pitfall 1).
    return fernet.decrypt(bytes(ciphertext)).decode("utf-8")


async def upsert_credential_encrypted(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    fernet: MultiFernet,
    principal_id: uuid.UUID,
    github_login: str,
    plaintext_token: str,
    scopes: tuple[str, ...],
) -> None:
    """Encrypt and UPSERT in one shot. Used by the OAuth callback (Plan 05)."""
    encrypted = encrypt_token(fernet, plaintext_token)
    async with sessionmaker.begin() as session:
        await cred_store.upsert_credential(
            session,
            principal_id=principal_id,
            github_login=github_login,
            encrypted_token=encrypted,
            scopes=scopes,
        )


async def get_pat(
    *,
    principal_id: uuid.UUID,
    agent_id: uuid.UUID | None = None,
    sessionmaker: async_sessionmaker[AsyncSession],
    fernet: MultiFernet,
    allow_service_default: bool = False,
    fallback_pat: str | None = None,
) -> str | None:
    """Per-agent credential resolver.

    agent_id given -> overlay-only -> opt-in service default -> None (no
    principal-default bleed).
    agent_id=None -> principal-default -> opt-in service default -> None
    (OAuth-callback / CLI path).

    The opt-in tier only engages when `allow_service_default` is set to True
    AND `fallback_pat` is not None — see the module docstring for the full
    contract and the callers that must never opt in.
    """
    async with sessionmaker() as session:
        if agent_id is not None:
            # Agent path is overlay-only. No fallback to principal default.
            binding = await binding_store.get_agent_github_binding(session, agent_id=agent_id)
            if binding is not None:
                overlay_cred = await cred_store.get_credential_by_principal(
                    session, principal_id=binding.principal_id
                )
                if overlay_cred is not None:
                    return decrypt_token(fernet, overlay_cred.encrypted_token)
            return fallback_pat if allow_service_default and fallback_pat is not None else None

        # agent_id=None: principal-default path (OAuth-callback / CLI flows).
        cred = await cred_store.get_credential_by_principal(session, principal_id=principal_id)
        if cred is not None:
            return decrypt_token(fernet, cred.encrypted_token)

        return fallback_pat if allow_service_default and fallback_pat is not None else None


async def get_github_login(
    *,
    principal_id: uuid.UUID,
    agent_id: uuid.UUID | None = None,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> str | None:
    """Display-only login resolver — the non-secret peer of `get_pat`.

    Returns `github_login` for the resolved credential without decrypting (or
    even reading) the token, so a Working repo view can show GitHub linkage
    for the selected agent. Same cascade shape as `get_pat`:

    agent_id given -> overlay-only -> None (no principal-default bleed).
    agent_id=None -> principal-default -> None.
    """
    async with sessionmaker() as session:
        if agent_id is not None:
            binding = await binding_store.get_agent_github_binding(session, agent_id=agent_id)
            if binding is None:
                return None
            return await cred_store.get_credential_login_by_principal(
                session, principal_id=binding.principal_id
            )

        return await cred_store.get_credential_login_by_principal(
            session, principal_id=principal_id
        )
