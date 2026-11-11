"""Executable record of where an agent has no memory, and why.

Memory lives in Anthropic memory stores. OpenAI and Gemini agents reach them
through Daimon's own memory tools (`daimon.core.host_memory`), so both need an
Anthropic key: without one every memory feature shows as needing setup. Self-
hosted turns never call Anthropic, so their memory stays a declared drop.
"""

from __future__ import annotations

import uuid

import pytest
from daimon.core.config import AnthropicSettings, DatabaseSettings, Settings
from daimon.core.features.registry import FEATURES
from daimon.core.features.snapshot import FeaturePlace, unavailable_reason
from daimon.core.host_memory import HOST_MEMORY_TOOLS
from daimon.core.turn.runtime_manifest import host_memory_represented
from mux.contracts.ids import ModelRef, Provider
from mux.contracts.resources import AgentSpec, MCPConnection
from pydantic import PostgresDsn, SecretStr

MEMORY_FEATURES = ("memory_shared", "memory_personal", "memory_channel")


def _settings(key: str | None) -> Settings:
    return Settings.model_validate(
        {
            "database": DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@localhost/d")),
            "anthropic": AnthropicSettings(api_key=SecretStr(key) if key else None),
        }
    )


def _target(provider: Provider, allowed: tuple[str, ...] = HOST_MEMORY_TOOLS) -> AgentSpec:
    return AgentSpec(
        name="copy",
        model=ModelRef(provider=provider, id="m"),
        system="Source.",
        mcp_servers=(
            MCPConnection(
                name="daimon-mcp",
                url="https://a.invalid/mcp",
                credential_ref="turn",
                tool_policy={"allowed_tools": list(allowed)},
            ),
        ),
    )


@pytest.mark.parametrize("key", MEMORY_FEATURES)
def test_memory_feature_when_no_anthropic_key_then_needs_setup(key: str) -> None:
    place = FeaturePlace(uuid.uuid4(), "discord", "c1", agent_id="a", backend="openai")
    spec = FEATURES[key]
    assert unavailable_reason(spec, _settings(None), place) == "requires", f"{key} keyless"
    assert unavailable_reason(spec, _settings("k"), place) != "requires", f"{key} with a key"


@pytest.mark.parametrize("provider", ["openai", "gemini"])
def test_host_memory_when_hosted_target_lists_the_tools_then_represented(
    provider: Provider,
) -> None:
    assert host_memory_represented(_target(provider)), f"{provider} reaches memory"
    assert not host_memory_represented(_target(provider, HOST_MEMORY_TOOLS[:1])), "all four tools"


def test_host_memory_when_self_hosted_then_never_represented() -> None:
    assert not host_memory_represented(_target("selfhosted")), "self-hosted never calls Anthropic"
    assert not host_memory_represented(None), "no target, no memory"
