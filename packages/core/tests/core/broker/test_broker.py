"""Behavioral tests for the token broker package.

These started life as Wave 0 RED stubs (ImportError-only) and are turned
green by the broker implementation when it lands.

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
from daimon.core._models import AgentGoogleBinding
from daimon.core.broker import dispatch_mint_token
from daimon.core.broker.errors import NoBindingError, ProviderConfigError
from daimon.core.broker.providers import TokenProvider
from daimon.core.broker.providers.gcloud import GcloudTokenProvider
from daimon.core.config import (
    AnthropicSettings,
    CredentialsSettings,
    DatabaseSettings,
    Settings,
)
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _base_settings(
    *,
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
        credentials=CredentialsSettings(google_sa_json=google_sa_json),
    )


@pytest.fixture
def gcloud_settings(google_sa_info: dict[str, Any]) -> Settings:
    return _base_settings(
        google_sa_json=SecretStr(json.dumps(google_sa_info)),
    )


@pytest.fixture
def gcloud_settings_no_sa() -> Settings:
    return _base_settings(google_sa_json=None)


def test_protocol_satisfied() -> None:
    """The gcloud provider satisfies the token provider protocol."""
    assert isinstance(GcloudTokenProvider(), TokenProvider), (
        "GcloudTokenProvider must satisfy TokenProvider Protocol structurally"
    )


@pytest.mark.asyncio
async def test_gcloud_opt_in_is_inert(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    gcloud_settings: Settings,
) -> None:
    """allow_service_default=True is a no-op for the gcloud provider — behavior
    is identical to the opt-out call."""

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
        allow_service_default=True,
    )
    assert token == "fake-access-token", (
        "allow_service_default must be inert for the gcloud provider"
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
    naming the operator and the bind-google command."""
    account_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    with pytest.raises(NoBindingError, match="bind-google"):
        await dispatch_mint_token(
            service="gcloud",
            account_id=account_id,
            agent_id=agent_id,
            sessionmaker=db_session_factory,
            settings=gcloud_settings,
        )


@pytest.mark.asyncio
async def test_gcloud_raises_provider_config_error_when_sa_json_unset(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    gcloud_settings_no_sa: Settings,
) -> None:
    """A bound agent with no usable credential gets a configuration error."""
    account_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    await _insert_google_binding(db_session, agent_id=agent_id)
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
    gcloud_settings: Settings,
) -> None:
    account_id = uuid.uuid4()
    with pytest.raises(ProviderConfigError, match="unknown service"):
        await dispatch_mint_token(
            service="not-a-real-service",
            account_id=account_id,
            agent_id=None,
            sessionmaker=db_session_factory,
            settings=gcloud_settings,
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
