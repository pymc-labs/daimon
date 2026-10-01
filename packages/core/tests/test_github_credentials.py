"""Resolver tests for github_credentials — Fernet round-trip + 2-tier cascade."""

from __future__ import annotations

import uuid

import pytest
from cryptography.fernet import Fernet, MultiFernet
from daimon.core._models import AgentGithubBinding
from daimon.core.github_credentials import (
    build_multifernet,
    decrypt_token,
    encrypt_token,
    get_pat,
    upsert_credential_encrypted,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.fixture
def fernet() -> MultiFernet:
    return build_multifernet((Fernet.generate_key().decode(),))


def test_encrypt_decrypt_roundtrips(fernet: MultiFernet) -> None:
    plaintext = "ghp_supersecrettoken"
    ciphertext = encrypt_token(fernet, plaintext)
    assert isinstance(ciphertext, bytes), "encrypt_token returns bytes"
    assert ciphertext != plaintext.encode(), (
        "ciphertext must not equal plaintext (Fernet must actually encrypt)"
    )
    assert decrypt_token(fernet, ciphertext) == plaintext, (
        "decrypt(encrypt(x)) == x — Fernet round-trip"
    )


def test_decrypt_token_handles_memoryview(fernet: MultiFernet) -> None:
    plaintext = "ghp_xxx"
    ciphertext = encrypt_token(fernet, plaintext)
    assert decrypt_token(fernet, memoryview(ciphertext)) == plaintext, (
        "decrypt_token must coerce memoryview → bytes (asyncpg BYTEA Pitfall 1)"
    )


def test_build_multifernet_rejects_empty_keys() -> None:
    with pytest.raises(ValueError, match="empty"):
        build_multifernet(())


async def test_get_pat_returns_principal_default_when_no_agent(
    db_session_factory: async_sessionmaker[AsyncSession],
    fernet: MultiFernet,
) -> None:
    principal_id = uuid.uuid4()

    await upsert_credential_encrypted(
        sessionmaker=db_session_factory,
        fernet=fernet,
        principal_id=principal_id,
        github_login="octocat",
        plaintext_token="ghp_default",
        scopes=("repo",),
    )

    result = await get_pat(
        principal_id=principal_id,
        agent_id=None,
        sessionmaker=db_session_factory,
        fernet=fernet,
    )
    assert result == "ghp_default", (
        "get_pat must return the principal-default token when agent_id is None"
    )


async def test_get_pat_returns_none_when_unbound(
    db_session_factory: async_sessionmaker[AsyncSession],
    fernet: MultiFernet,
) -> None:
    result = await get_pat(
        principal_id=uuid.uuid4(),
        agent_id=None,
        sessionmaker=db_session_factory,
        fernet=fernet,
    )
    assert result is None, "get_pat must return None (NOT raise) when no credential exists"


async def test_get_pat_returns_overlay_when_binding_present(
    db_session_factory: async_sessionmaker[AsyncSession],
    fernet: MultiFernet,
) -> None:
    overlay_principal_id = uuid.uuid4()
    caller_principal_id = uuid.uuid4()
    agent_id = uuid.uuid4()

    # Overlay credential (what we expect the resolver to return)
    await upsert_credential_encrypted(
        sessionmaker=db_session_factory,
        fernet=fernet,
        principal_id=overlay_principal_id,
        github_login="overlay-user",
        plaintext_token="ghp_overlay",
        scopes=("repo",),
    )
    # Caller's default credential (must NOT be returned when overlay is present)
    await upsert_credential_encrypted(
        sessionmaker=db_session_factory,
        fernet=fernet,
        principal_id=caller_principal_id,
        github_login="caller-user",
        plaintext_token="ghp_caller_default",
        scopes=("repo",),
    )
    # Binding row routes agent_id → overlay_principal_id
    async with db_session_factory.begin() as session:
        session.add(
            AgentGithubBinding(
                agent_id=agent_id,
                principal_id=overlay_principal_id,
            )
        )

    result = await get_pat(
        principal_id=caller_principal_id,
        agent_id=agent_id,
        sessionmaker=db_session_factory,
        fernet=fernet,
    )
    assert result == "ghp_overlay", (
        "get_pat must return the overlay (binding-resolved) token, "
        "NOT the caller's principal default"
    )


async def test_get_pat_agent_no_overlay_returns_none(
    db_session_factory: async_sessionmaker[AsyncSession],
    fernet: MultiFernet,
) -> None:
    """get_pat with agent_id given and no overlay row returns None,
    even when the principal has a default credential. No bleed."""
    principal_id = uuid.uuid4()
    agent_id = uuid.uuid4()  # no binding row for this agent

    # Principal has a default credential — the bleed was that this would be returned.
    await upsert_credential_encrypted(
        sessionmaker=db_session_factory,
        fernet=fernet,
        principal_id=principal_id,
        github_login="octocat",
        plaintext_token="ghp_principal_default",
        scopes=("repo",),
    )

    result = await get_pat(
        principal_id=principal_id,
        agent_id=agent_id,
        sessionmaker=db_session_factory,
        fernet=fernet,
    )
    assert result is None, (
        "get_pat(agent_id=X) with no overlay row must return None — "
        "NOT the principal-default credential (bleed fix)"
    )


# --- Opt-in operator service-default tier (allow_service_default + fallback_pat) ---


async def test_get_pat_agent_unbound_returns_fallback_when_opted_in(
    db_session_factory: async_sessionmaker[AsyncSession],
    fernet: MultiFernet,
) -> None:
    """agent_id given, no binding row at all, opted in with a fallback value
    -> the fallback wins (no bleed to principal-default; still no real credential)."""
    result = await get_pat(
        principal_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        sessionmaker=db_session_factory,
        fernet=fernet,
        allow_service_default=True,
        fallback_pat="ghp_service",
    )
    assert result == "ghp_service", (
        "get_pat must return the fallback when opted in and no binding resolves"
    )


async def test_get_pat_agent_bound_no_credential_returns_fallback_when_opted_in(
    db_session_factory: async_sessionmaker[AsyncSession],
    fernet: MultiFernet,
) -> None:
    """agent_id given, binding row exists but the bound principal has no
    credential row -> opted-in fallback still resolves."""
    agent_id = uuid.uuid4()
    unbound_principal_id = uuid.uuid4()  # no credential row for this principal
    async with db_session_factory.begin() as session:
        session.add(AgentGithubBinding(agent_id=agent_id, principal_id=unbound_principal_id))

    result = await get_pat(
        principal_id=uuid.uuid4(),
        agent_id=agent_id,
        sessionmaker=db_session_factory,
        fernet=fernet,
        allow_service_default=True,
        fallback_pat="ghp_service",
    )
    assert result == "ghp_service", (
        "get_pat must return the fallback when the bound principal has no credential row"
    )


async def test_get_pat_agent_overlay_credential_wins_over_fallback(
    db_session_factory: async_sessionmaker[AsyncSession],
    fernet: MultiFernet,
) -> None:
    """A real overlay credential must win over a configured fallback — the
    fallback can never shadow a resolved real credential."""
    overlay_principal_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    await upsert_credential_encrypted(
        sessionmaker=db_session_factory,
        fernet=fernet,
        principal_id=overlay_principal_id,
        github_login="overlay-user",
        plaintext_token="ghp_overlay",
        scopes=("repo",),
    )
    async with db_session_factory.begin() as session:
        session.add(AgentGithubBinding(agent_id=agent_id, principal_id=overlay_principal_id))

    result = await get_pat(
        principal_id=uuid.uuid4(),
        agent_id=agent_id,
        sessionmaker=db_session_factory,
        fernet=fernet,
        allow_service_default=True,
        fallback_pat="ghp_service",
    )
    assert result == "ghp_overlay", (
        "a resolved overlay credential must win over the fallback, never be shadowed by it"
    )


async def test_get_pat_agent_path_fallback_none_still_resolves_none(
    db_session_factory: async_sessionmaker[AsyncSession],
    fernet: MultiFernet,
) -> None:
    """allow_service_default=True alone (fallback_pat=None) resolves None —
    the flag is the authorization, the value is the payload; neither alone does anything."""
    result = await get_pat(
        principal_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        sessionmaker=db_session_factory,
        fernet=fernet,
        allow_service_default=True,
        fallback_pat=None,
    )
    assert result is None, (
        "allow_service_default=True with fallback_pat=None must still resolve None"
    )


async def test_get_pat_principal_default_path_returns_fallback_when_opted_in(
    db_session_factory: async_sessionmaker[AsyncSession],
    fernet: MultiFernet,
) -> None:
    """agent_id=None, no principal-default credential -> opted-in fallback wins."""
    result = await get_pat(
        principal_id=uuid.uuid4(),
        agent_id=None,
        sessionmaker=db_session_factory,
        fernet=fernet,
        allow_service_default=True,
        fallback_pat="ghp_service",
    )
    assert result == "ghp_service", (
        "get_pat(agent_id=None) must return the fallback when opted in and unbound"
    )


async def test_get_pat_principal_default_credential_wins_over_fallback(
    db_session_factory: async_sessionmaker[AsyncSession],
    fernet: MultiFernet,
) -> None:
    """agent_id=None with a real principal-default credential -> that credential
    wins over the fallback."""
    principal_id = uuid.uuid4()
    await upsert_credential_encrypted(
        sessionmaker=db_session_factory,
        fernet=fernet,
        principal_id=principal_id,
        github_login="octocat",
        plaintext_token="ghp_principal_default",
        scopes=("repo",),
    )

    result = await get_pat(
        principal_id=principal_id,
        agent_id=None,
        sessionmaker=db_session_factory,
        fernet=fernet,
        allow_service_default=True,
        fallback_pat="ghp_service",
    )
    assert result == "ghp_principal_default", (
        "the real principal-default credential must win over the fallback"
    )


async def test_get_pat_fallback_value_alone_without_flag_resolves_none(
    db_session_factory: async_sessionmaker[AsyncSession],
    fernet: MultiFernet,
) -> None:
    """fallback_pat set but allow_service_default left at its False default ->
    the value alone must never leak without the flag."""
    result = await get_pat(
        principal_id=uuid.uuid4(),
        agent_id=None,
        sessionmaker=db_session_factory,
        fernet=fernet,
        fallback_pat="ghp_service",
    )
    assert result is None, (
        "fallback_pat set without allow_service_default=True must never leak the value"
    )
