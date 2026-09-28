import json
import runpy
import uuid
from pathlib import Path

import pytest
from daimon.core.config import AnthropicSettings, DatabaseSettings, DirectMessagePolicy, Settings
from pydantic import SecretStr, ValidationError


@pytest.mark.parametrize("key", [str(uuid.UUID(int=10)).upper(), uuid.UUID(int=10).hex])
def test_tenant_policy_map_loads_from_json_environment(monkeypatch, key):
    monkeypatch.setenv(
        "DAIMON_DIRECT_MESSAGE_POLICIES",
        json.dumps({key: {"mode": "allowlist", "recipient_ids": ["U123"]}}),
    )
    settings = Settings(
        database=DatabaseSettings(url="postgresql+asyncpg://x/y"),
        anthropic=AnthropicSettings(api_key=SecretStr("test")),
        _env_file=None,
    )
    assert settings.direct_message_policies[uuid.UUID(int=10)].allows("U123")
    assert not settings.direct_message_policies[uuid.UUID(int=10)].allows("U456")


def test_config_docs_treat_policy_maps_as_json_leaves():
    helpers = runpy.run_path(str(Path(__file__).resolve().parents[3] / "scripts/_settings_walk.py"))
    unwrap = helpers["unwrap_nested_model"]
    assert unwrap(dict[uuid.UUID, DirectMessagePolicy]) is None
    assert unwrap(DirectMessagePolicy | None) is DirectMessagePolicy


def test_misspelled_recipient_policy_fails_closed():
    with pytest.raises(ValidationError):
        DirectMessagePolicy.model_validate({"mod": "disabled"})


@pytest.mark.parametrize("key", ["tenant-id", "", "not-a-uuid"])
def test_invalid_tenant_policy_key_fails_settings_load(monkeypatch, key):
    monkeypatch.setenv("DAIMON_DIRECT_MESSAGE_POLICIES", json.dumps({key: {"mode": "disabled"}}))
    with pytest.raises(ValidationError, match="uuid_parsing"):
        Settings(
            database=DatabaseSettings(url="postgresql+asyncpg://x/y"),
            anthropic=AnthropicSettings(api_key=SecretStr("test")),
            _env_file=None,
        )
