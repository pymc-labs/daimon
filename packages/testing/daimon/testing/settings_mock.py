"""`Settings` stand-ins: a `MagicMock` with a real `features` mapping so feature resolution
reads deployment values, not mocks, and a real one with nothing set up."""

from __future__ import annotations

from unittest.mock import MagicMock

from daimon.core.config import AnthropicSettings, DatabaseSettings, Settings
from pydantic import PostgresDsn, SecretStr


def mock_settings(**features: str) -> MagicMock:
    """`Settings` stand-in; `features` are the deployment tier's values (`DAIMON_FEATURES__*`)."""
    settings = MagicMock()
    settings.features = dict(features)
    return settings


def bare_settings() -> Settings:
    """A real `Settings` with only what validation needs: no integration is set up."""
    return Settings.model_validate(
        {
            "database": DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@localhost/d")),
            "anthropic": AnthropicSettings(api_key=SecretStr("test")),
        }
    )
