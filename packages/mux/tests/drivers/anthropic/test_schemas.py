import pytest
from mux.contracts.extensions import ExtensionConfig
from mux.drivers.anthropic.schemas import AgentToolsConfig, MultiagentConfig, agent_tools
from pydantic import ValidationError


@pytest.mark.parametrize(
    "tools",
    [
        [],
        [{"type": "agent_toolset_20260401"}],
        [{"type": "agent_toolset_20260401", "configs": [], "default_config": None}],
        [
            {
                "type": "agent_toolset_20260401",
                "configs": [
                    {"name": "bash", "enabled": False, "permission_policy": {"type": "always_ask"}},
                    {"name": "read"},
                ],
            }
        ],
        [
            {
                "type": "mcp_toolset",
                "mcp_server_name": "daimon-mcp",
                "configs": [{"name": "save_file", "permission_policy": None}],
            }
        ],
        [
            {
                "type": "custom",
                "name": "weather",
                "description": "Weather",
                "input_schema": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            }
        ],
    ],
)
def test_tools_round_trip_preserves_omission_and_explicit_empty(tools):
    payload = {"tools": tools}
    extension = ExtensionConfig(namespace="anthropic.agent_tools", version=1, value=payload)
    parsed = agent_tools(extension)
    assert parsed.model_dump(mode="json", exclude_unset=True) == payload
    assert parsed.model_dump(mode="json", exclude_unset=True) == payload


@pytest.mark.parametrize(
    "payload",
    [
        {"tools": [], "unexpected": True},
        {"tools": [{"type": "mcp_toolset", "mcp_server_name": "s", "arbitrary_kwarg": 1}]},
        {"tools": [{"type": "agent_toolset_20260401", "configs": [{"name": "shell"}]}]},
        {"tools": [{"type": "agent_toolset_20260401", "default_config": {"enabled": "false"}}]},
    ],
)
def test_tools_reject_unknown_fields_and_coercion(payload):
    with pytest.raises(ValidationError):
        AgentToolsConfig.model_validate(payload)


def test_roster_keeps_unversioned_versioned_and_self_references():
    payload = {
        "type": "coordinator",
        "agents": ["agent-a", {"type": "agent", "id": "agent-b", "version": 3}, {"type": "self"}],
    }
    assert (
        MultiagentConfig.model_validate(payload).model_dump(mode="json", exclude_unset=True)
        == payload
    )


def test_wrong_extension_version_is_rejected():
    with pytest.raises(ValueError, match="expected anthropic.agent_tools@1"):
        agent_tools(
            ExtensionConfig(namespace="anthropic.agent_tools", version=2, value={"tools": []})
        )
