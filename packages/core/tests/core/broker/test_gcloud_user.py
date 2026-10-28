"""Offline credential-selection and secret-redaction tests for Google tokens."""

from __future__ import annotations

import json
import logging
import traceback
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from daimon.core.broker import dispatch_mint_token
from daimon.core.broker.errors import NoBindingError, ProviderConfigError
from daimon.core.config import CredentialsSettings, Settings
from daimon.core.stores.domain import AgentGoogleBindingRow
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .test_broker import _base_settings

_USER_INFO = {
    "type": "authorized_user",
    "client_id": "test-client-id",
    "client_secret": "test-client-secret",
    "refresh_token": "test-refresh-token",
}
_AGENT_ID = uuid.uuid4()
_SCOPES = ("https://www.googleapis.com/auth/drive", "https://www.googleapis.com/auth/gmail.send")


@pytest.fixture
def mint(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> Any:
    """Fake only the store boundary; no database or network is used."""
    caplog.set_level(logging.DEBUG)

    @asynccontextmanager
    async def sessions() -> AsyncIterator[None]:
        yield None

    async def run(
        settings: Settings,
        email: str | None = "USER@example.com",
        *,
        agent_id: uuid.UUID | None = _AGENT_ID,
    ) -> str:
        async def binding(session: object, *, agent_id: uuid.UUID) -> AgentGoogleBindingRow | None:
            if email is None:
                return None
            return AgentGoogleBindingRow(
                agent_id=agent_id,
                email=email,
                scopes=_SCOPES,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )

        monkeypatch.setattr("daimon.core.broker.providers.gcloud.get_agent_google_binding", binding)
        return await dispatch_mint_token(
            service="gcloud",
            account_id=uuid.uuid4(),
            agent_id=agent_id,
            sessionmaker=cast(async_sessionmaker[AsyncSession], sessions),
            settings=settings,
        )

    return run


def _settings(
    raw: str = json.dumps(_USER_INFO), email: str | None = "user@EXAMPLE.com"
) -> Settings:
    settings = _base_settings()
    settings.credentials = CredentialsSettings(
        google_user_json=SecretStr(raw), google_user_email=email
    )
    return settings


@pytest.mark.asyncio
async def test_matching_user_scopes_and_secret_redaction(
    mint: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    settings = _settings(
        json.dumps(
            {
                **_USER_INFO,
                "scopes": ["https://www.googleapis.com/auth/cloud-platform"],
                "token": "unused-stored-access-token",
            }
        )
    )

    def refresh(self: Any, request: object) -> None:
        assert self.token is None
        assert self.scopes == list(_SCOPES)
        assert self.refresh_token == _USER_INFO["refresh_token"]
        assert self.client_id == _USER_INFO["client_id"]
        assert self.client_secret == _USER_INFO["client_secret"]
        self.token = "test-access-token"

    monkeypatch.setattr("google.oauth2.credentials.Credentials.refresh", refresh)
    # User credentials take priority even if the configured SA is invalid.
    settings.credentials.google_sa_json = SecretStr("invalid-unused-sa")
    assert await mint(settings) == "test-access-token"
    for secret in (*_USER_INFO.values(), "test-access-token", "unused-stored-access-token"):
        assert secret not in repr(settings.credentials)
        assert secret not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("email", ["other@example.com", None])
async def test_user_credential_requires_matching_configured_email(
    mint: Any, email: str | None
) -> None:
    settings = _settings("invalid-unused-user-secret", email=email)
    with pytest.raises(ProviderConfigError, match="google_sa_json"):
        await mint(settings)


@pytest.mark.asyncio
async def test_other_binding_uses_sa_without_reading_user_json(
    mint: Any, monkeypatch: pytest.MonkeyPatch, google_sa_info: dict[str, Any]
) -> None:
    settings = _settings("invalid-unused-user-secret")
    settings.credentials.google_sa_json = SecretStr(json.dumps(google_sa_info))

    def refresh(self: Any, request: object) -> None:
        assert self.scopes == list(_SCOPES)
        assert self._subject == "other@example.com"
        self.token = "test-sa-token"

    def forbidden(self: SecretStr) -> str:
        raise AssertionError("unrelated binding touched the user credential")

    class UnreadableSecret(SecretStr):
        get_secret_value = forbidden

    settings.credentials.google_user_json = UnreadableSecret("unused")
    monkeypatch.setattr("google.oauth2.service_account.Credentials.refresh", refresh)
    assert await mint(settings, "other@example.com") == "test-sa-token"


_BAD_JSON = [
    "test-refresh-token{",
    json.dumps(["test-refresh-token"]),
    json.dumps({**_USER_INFO, "type": "test-refresh-token"}),
    *(
        json.dumps({key: value for key, value in _USER_INFO.items() if key != missing})
        for missing in ("client_id", "client_secret", "refresh_token")
    ),
    json.dumps({**_USER_INFO, "refresh_token": 123}),
    json.dumps({**_USER_INFO, "client_secret": " "}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", _BAD_JSON)
async def test_invalid_user_json_is_redacted_config_error(
    mint: Any, raw: str, caplog: pytest.LogCaptureFixture
) -> None:
    with pytest.raises(ProviderConfigError, match="google_user_json") as exc:
        await mint(_settings(raw))
    output = "".join(traceback.format_exception(exc.value)) + caplog.text + repr(exc.value)
    for secret in (_USER_INFO["refresh_token"], _USER_INFO["client_secret"]):
        assert secret not in output
    assert exc.value.__context__ is None


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["construct", "refresh", "empty-token"])
async def test_sdk_errors_are_redacted(
    mint: Any, monkeypatch: pytest.MonkeyPatch, stage: str, caplog: pytest.LogCaptureFixture
) -> None:
    def fail(*args: Any, **kwargs: Any) -> None:
        raise ValueError("test-refresh-token test-client-secret test-access-token")

    if stage == "construct":
        monkeypatch.setattr("google.oauth2.credentials.Credentials.from_authorized_user_info", fail)
    elif stage == "refresh":
        monkeypatch.setattr("google.oauth2.credentials.Credentials.refresh", fail)
    else:
        monkeypatch.setattr("google.oauth2.credentials.Credentials.refresh", lambda *args: None)
    with pytest.raises(ProviderConfigError, match="google_user_json") as exc:
        await mint(_settings())
    assert exc.value.__context__ is None
    output = "".join(traceback.format_exception(exc.value)) + caplog.text
    for secret in ("test-refresh-token", "test-client-secret", "test-access-token"):
        assert secret not in output


@pytest.mark.asyncio
async def test_no_credentials_and_binding_gates(mint: Any) -> None:
    with pytest.raises(ProviderConfigError, match="google_sa_json"):
        await mint(_base_settings())
    with pytest.raises(NoBindingError, match="bind-google"):
        await mint(_settings(), None)
    with pytest.raises(NoBindingError, match="requires an agent_id"):
        await mint(_settings(), agent_id=None)
