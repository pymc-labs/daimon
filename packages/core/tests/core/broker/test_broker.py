"""Behavioral tests for the token broker package (Phase 19, GH-03).

These started life as Wave 0 RED stubs (ImportError-only) and are turned
green by Wave 3 plan 19-03 when the broker implementation lands. Plan 03
task 3.2 renames this file to ``test_broker.py``.

Per testing skill: real DB sessions, no ``model_construct``, no ``AsyncMock``
on SDK methods. ``google.oauth2.service_account.Credentials.refresh`` is
patched at the boundary because we cannot reach Google's OAuth endpoint
from CI; everything else uses real classes.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import pytest
from cryptography.fernet import Fernet
from daimon.core._models import AgentGoogleBinding
from daimon.core.broker import dispatch_mint_token
from daimon.core.broker.errors import NoBindingError, ProviderConfigError
from daimon.core.broker.providers import TokenProvider
from daimon.core.broker.providers.gcloud import GcloudTokenProvider
from daimon.core.broker.providers.github import GitHubTokenProvider
from daimon.core.config import (
    AnthropicSettings,
    CredentialsSettings,
    CryptoSettings,
    DatabaseSettings,
    Settings,
)
from daimon.core.github_credentials import build_multifernet, upsert_credential_encrypted
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _base_settings(
    *,
    crypto_keys: tuple[SecretStr, ...] = (),
    google_sa_json: SecretStr | None = None,
) -> Settings:
    """Construct a fully-populated Settings for broker tests."""
    return Settings(
        database=DatabaseSettings(
            url=PostgresDsn("postgresql+asyncpg://daimon:daimon@localhost:5432/daimon"),
        ),
        anthropic=AnthropicSettings(
            api_key=SecretStr("sk-test"),
            base_url=HttpUrl("https://api.anthropic.com"),
        ),
        crypto=CryptoSettings(keys=crypto_keys),
        credentials=CredentialsSettings(google_sa_json=google_sa_json),
    )


@pytest.fixture
def fernet_key() -> SecretStr:
    return SecretStr(Fernet.generate_key().decode("ascii"))


@pytest.fixture
def github_settings(fernet_key: SecretStr) -> Settings:
    return _base_settings(crypto_keys=(fernet_key,))


@pytest.fixture
def gcloud_settings(google_sa_info: dict[str, Any]) -> Settings:
    return _base_settings(
        google_sa_json=SecretStr(json.dumps(google_sa_info)),
    )


@pytest.fixture
def gcloud_settings_no_sa() -> Settings:
    return _base_settings(google_sa_json=None)


def test_protocol_satisfied() -> None:
    """``GitHubTokenProvider`` and ``GcloudTokenProvider`` must structurally
    satisfy the ``TokenProvider`` Protocol (runtime_checkable)."""
    assert isinstance(GitHubTokenProvider(), TokenProvider), (
        "GitHubTokenProvider must satisfy TokenProvider Protocol structurally"
    )
    assert isinstance(GcloudTokenProvider(), TokenProvider), (
        "GcloudTokenProvider must satisfy TokenProvider Protocol structurally"
    )


@pytest.mark.asyncio
async def test_github_passthrough_returns_stored_pat(
    db_session_factory: async_sessionmaker[AsyncSession],
    github_settings: Settings,
    fernet_key: SecretStr,
) -> None:
    """Dispatch for service='github' returns the decrypted PAT for the
    account's stored GitHub credential."""
    account_id = uuid.uuid4()
    fernet = build_multifernet((fernet_key.get_secret_value(),))
    await upsert_credential_encrypted(
        sessionmaker=db_session_factory,
        fernet=fernet,
        principal_id=account_id,
        github_login="octocat",
        plaintext_token="ghp_supersecret",
        scopes=("repo",),
    )

    token = await dispatch_mint_token(
        service="github",
        account_id=account_id,
        agent_id=None,
        sessionmaker=db_session_factory,
        settings=github_settings,
    )
    assert token == "ghp_supersecret", "github passthrough must return the decrypted PAT verbatim"


@pytest.mark.asyncio
async def test_github_passthrough_raises_no_binding_when_unbound(
    db_session_factory: async_sessionmaker[AsyncSession],
    github_settings: Settings,
) -> None:
    """Dispatch for service='github' with no credential row raises NoBindingError."""
    account_id = uuid.uuid4()
    with pytest.raises(NoBindingError):
        await dispatch_mint_token(
            service="github",
            account_id=account_id,
            agent_id=None,
            sessionmaker=db_session_factory,
            settings=github_settings,
        )


@pytest.mark.asyncio
async def test_github_raises_provider_config_when_crypto_keys_unset(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """github provider requires crypto keys to decrypt the at-rest PAT."""
    account_id = uuid.uuid4()
    settings = _base_settings(crypto_keys=())
    with pytest.raises(ProviderConfigError, match="crypto.keys"):
        await dispatch_mint_token(
            service="github",
            account_id=account_id,
            agent_id=None,
            sessionmaker=db_session_factory,
            settings=settings,
        )


async def _insert_google_binding(
    db_session: AsyncSession,
    *,
    agent_id: uuid.UUID,
    email: str = "user@example.com",
    scopes: tuple[str, ...] = ("https://www.googleapis.com/auth/gmail.readonly",),
) -> None:
    db_session.add(
        AgentGoogleBinding(
            agent_id=agent_id,
            email=email,
            scopes=list(scopes),
        )
    )
    await db_session.flush()


@pytest.mark.asyncio
async def test_gcloud_impersonation_returns_token(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    gcloud_settings: Settings,
) -> None:
    """Dispatch for service='gcloud' returns the access token from the
    impersonated Credentials.refresh()."""

    def _fake_refresh(self: Any, request: Any) -> None:
        self.token = "fake-access-token"

    monkeypatch.setattr(
        "google.oauth2.service_account.Credentials.refresh",
        _fake_refresh,
    )
    account_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    await _insert_google_binding(db_session, agent_id=agent_id)

    token = await dispatch_mint_token(
        service="gcloud",
        account_id=account_id,
        agent_id=agent_id,
        sessionmaker=db_session_factory,
        settings=gcloud_settings,
    )
    assert token == "fake-access-token", (
        "gcloud impersonation must return the SDK-refreshed access token verbatim"
    )


@pytest.mark.asyncio
async def test_gcloud_raises_no_binding_when_no_agent_google_binding(
    db_session_factory: async_sessionmaker[AsyncSession],
    gcloud_settings: Settings,
) -> None:
    """SA JSON configured but no agent_google_binding row → NoBindingError
    with message naming the agent."""
    account_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    with pytest.raises(NoBindingError, match="Agent not bound"):
        await dispatch_mint_token(
            service="gcloud",
            account_id=account_id,
            agent_id=agent_id,
            sessionmaker=db_session_factory,
            settings=gcloud_settings,
        )


@pytest.mark.asyncio
async def test_gcloud_raises_provider_config_error_when_sa_json_unset(
    db_session_factory: async_sessionmaker[AsyncSession],
    gcloud_settings_no_sa: Settings,
) -> None:
    """SA JSON unset → ProviderConfigError (BEFORE binding lookup, per
    Pitfall 2)."""
    account_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    with pytest.raises(ProviderConfigError, match="google_sa_json"):
        await dispatch_mint_token(
            service="gcloud",
            account_id=account_id,
            agent_id=agent_id,
            sessionmaker=db_session_factory,
            settings=gcloud_settings_no_sa,
        )


@pytest.mark.asyncio
async def test_gcloud_raises_no_binding_when_agent_id_is_none(
    db_session_factory: async_sessionmaker[AsyncSession],
    gcloud_settings: Settings,
) -> None:
    """SA JSON set but ``agent_id=None`` → NoBindingError (per Pitfall 7),
    not a silent fallthrough."""
    account_id = uuid.uuid4()
    with pytest.raises(NoBindingError, match="requires an agent_id"):
        await dispatch_mint_token(
            service="gcloud",
            account_id=account_id,
            agent_id=None,
            sessionmaker=db_session_factory,
            settings=gcloud_settings,
        )


@pytest.mark.asyncio
async def test_gcloud_raises_provider_config_when_sa_json_not_service_account(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """SA JSON missing 'type=service_account' → ProviderConfigError
    (Pitfall 3 — guards against passing an arbitrary JSON or path)."""
    account_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    await _insert_google_binding(db_session, agent_id=agent_id)
    settings = _base_settings(
        google_sa_json=SecretStr(json.dumps({"type": "user", "client_email": "x"})),
    )
    with pytest.raises(ProviderConfigError, match="type=service_account"):
        await dispatch_mint_token(
            service="gcloud",
            account_id=account_id,
            agent_id=agent_id,
            sessionmaker=db_session_factory,
            settings=settings,
        )


@pytest.mark.asyncio
async def test_dispatch_unknown_service_raises_provider_config_error(
    db_session_factory: async_sessionmaker[AsyncSession],
    github_settings: Settings,
) -> None:
    account_id = uuid.uuid4()
    with pytest.raises(ProviderConfigError, match="unknown service"):
        await dispatch_mint_token(
            service="not-a-real-service",
            account_id=account_id,
            agent_id=None,
            sessionmaker=db_session_factory,
            settings=github_settings,
        )


@pytest.mark.asyncio
async def test_audit_log_never_carries_token_value(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    gcloud_settings: Settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The token plaintext must never appear in any log record emitted by
    the broker (audit lines log service + account UUID only)."""

    def _fake_refresh(self: Any, request: Any) -> None:
        self.token = "fake-access-token"

    monkeypatch.setattr(
        "google.oauth2.service_account.Credentials.refresh",
        _fake_refresh,
    )
    account_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    await _insert_google_binding(db_session, agent_id=agent_id)

    caplog.set_level(logging.DEBUG, logger="daimon.core.broker")
    await dispatch_mint_token(
        service="gcloud",
        account_id=account_id,
        agent_id=agent_id,
        sessionmaker=db_session_factory,
        settings=gcloud_settings,
    )
    assert "fake-access-token" not in caplog.text, (
        "broker audit log must never contain the minted token plaintext"
    )
