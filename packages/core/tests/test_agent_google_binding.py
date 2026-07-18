"""Tests for AgentGoogleBinding ORM, Row, and CredentialsSettings."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError


def test_agent_google_binding_orm_has_expected_tablename() -> None:
    from daimon.core._models import AgentGoogleBinding

    assert AgentGoogleBinding.__tablename__ == "agent_google_binding", (
        "AgentGoogleBinding ORM should map to agent_google_binding table"
    )


def test_agent_google_binding_row_validates_construction() -> None:
    from daimon.core.stores.domain import AgentGoogleBindingRow

    now = datetime.now(UTC)
    row = AgentGoogleBindingRow.model_validate(
        {
            "agent_id": uuid.uuid4(),
            "email": "u@example.com",
            "scopes": ("a", "b"),
            "created_at": now,
            "updated_at": now,
        }
    )

    assert row.email == "u@example.com", "row should preserve email"
    assert row.scopes == ("a", "b"), "scopes should be a tuple of strings"


def test_agent_google_binding_row_is_frozen() -> None:
    from daimon.core.stores.domain import AgentGoogleBindingRow

    now = datetime.now(UTC)
    row = AgentGoogleBindingRow(
        agent_id=uuid.uuid4(),
        email="u@example.com",
        scopes=("a",),
        created_at=now,
        updated_at=now,
    )
    with pytest.raises(ValidationError):
        row.email = "x@example.com"  # pyright: ignore[reportAttributeAccessIssue]


def test_credentials_settings_default_google_sa_json_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from daimon.core.config import CredentialsSettings

    monkeypatch.delenv("DAIMON_CREDENTIALS__GOOGLE_SA_JSON", raising=False)
    settings = CredentialsSettings()

    assert settings.google_sa_json is None, (
        "credentials.google_sa_json should default to None when env unset"
    )


def test_settings_loads_google_sa_json_from_env_when_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from daimon.core.config import load_settings

    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h:5432/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")
    monkeypatch.setenv(
        "DAIMON_CREDENTIALS__GOOGLE_SA_JSON",
        '{"type":"service_account","project_id":"x"}',
    )

    settings = load_settings(_env_file=None)

    assert settings.credentials.google_sa_json is not None, (
        "google_sa_json should load from DAIMON_CREDENTIALS__GOOGLE_SA_JSON"
    )
    assert (
        settings.credentials.google_sa_json.get_secret_value()
        == '{"type":"service_account","project_id":"x"}'
    ), "secret value should round-trip the env string"
