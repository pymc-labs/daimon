"""An embedded app's token exchange needs a known identity server, a key and the web provider."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from daimon.core.config import Settings
from pydantic import ValidationError

_ISSUER = "https://id.example.com"
_SERVER = {"name": "hub", "url": "https://hub.example.com/mcp", "issuer": "d", "audience": "a"}


def _settings(*, server: str = "hub", key: str | None = "k" * 43, issuer: str = _ISSUER) -> Any:
    return Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        database={"url": "postgresql+asyncpg://u:p@h/db"},  # pyright: ignore[reportArgumentType]
        mcp_identity={"signing_key": key, "servers": [_SERVER]},  # pyright: ignore[reportArgumentType]
        web={  # pyright: ignore[reportArgumentType]
            "public_url": "https://web.example.com",
            "oidc": {
                "issuer": _ISSUER,
                "client_id": "c",
                "client_secret": "s",
                "tenant_id": str(uuid.uuid4()),
            },
            "apps": [
                {
                    "key": "data",
                    "name": "Data",
                    "icon": "grid",
                    "origin": "https://app.example.com",
                    "oidc_issuer": issuer,
                    "identity_server": server,
                }
            ],
        },
    )


def test_a_complete_exchange_setup_loads() -> None:
    assert _settings().web.apps[0].identity_server == "hub"


@pytest.mark.parametrize(
    ("overrides", "names"),
    [
        ({"server": "nope"}, "DAIMON_MCP_IDENTITY__SERVERS"),
        ({"key": None}, "DAIMON_MCP_IDENTITY__SIGNING_KEY"),
        ({"issuer": "https://other.example.com"}, "DAIMON_WEB__OIDC__ISSUER"),
    ],
)
def test_an_incomplete_exchange_setup_is_refused(overrides: dict[str, Any], names: str) -> None:
    with pytest.raises(ValidationError, match=names):
        _settings(**overrides)
