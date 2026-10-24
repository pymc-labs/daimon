import pytest
from mux.contracts.extensions import ExtensionConfig
from mux.drivers.anthropic.schemas import AgentToolsConfig, MultiagentConfig, agent_tools
from pydantic import ValidationError


@pytest.mark.parametrize("level", ["low", "medium", "high", "xhigh", "max"])
@pytest.mark.parametrize("object_form", [False, True])
def test_native_model_effort_round_trips_without_changing_its_form(level, object_form):
    from mux.drivers.anthropic.schemas import AgentModelConfig

    effort = {"type": level} if object_form else level
    payload = {"model": {"id": "claude-haiku-5-5", "effort": effort}}
    assert (
        AgentModelConfig.model_validate(payload).model_dump(mode="json", exclude_unset=True)
        == payload
    )


@pytest.mark.parametrize("options", [{}, {"effort": None}, {"speed": None}])
def test_native_model_preserves_omitted_and_null_options(options):
    from mux.drivers.anthropic.schemas import AgentModelConfig

    payload = {"model": {"id": "claude-haiku-5-5", **options}}
    assert (
        AgentModelConfig.model_validate(payload).model_dump(mode="json", exclude_unset=True)
        == payload
    )


@pytest.mark.parametrize(
    "options",
    [
        {"effort": "unrecognized"},
        {"effort": True},
        {"effort": 1},
        {"effort": []},
        {"effort": {}},
        {"effort": {"type": "unrecognized"}},
        {"effort": {"type": "high", "budget": 100}},
        {"unknown_control": "high"},
    ],
)
def test_native_model_rejects_unknown_or_invalid_controls(options):
    from mux.drivers.anthropic.schemas import AgentModelConfig

    with pytest.raises(ValidationError):
        AgentModelConfig.model_validate({"model": {"id": "claude-haiku-5-5", **options}})


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


def test_legacy_create_nulls_are_a_closed_field_allowlist():
    from mux.drivers.anthropic.schemas import AgentCreateNulls

    assert AgentCreateNulls(fields=["description"]).model_dump() == {"fields": ["description"]}
    with pytest.raises(ValidationError):
        AgentCreateNulls.model_validate({"fields": ["name"]})
    with pytest.raises(ValidationError):
        AgentCreateNulls.model_validate({"fields": ["description"], "arbitrary": None})


async def test_duplicate_native_config_namespaces_fail_before_io():
    from mux.contracts.extensions import ExtensionConfig
    from mux.contracts.ids import ModelRef, Scope
    from mux.contracts.resources import AgentSpec
    from mux.drivers.anthropic import AnthropicManagedAgents
    from mux.testing.drivers.anthropic.ma_transport import ScriptedTransport

    extension = ExtensionConfig(namespace="anthropic.agent_tools", version=1, value={"tools": []})
    transport = ScriptedTransport()
    async with transport.client() as sdk:
        backend = AnthropicManagedAgents(sdk)
        spec = AgentSpec(
            name="one",
            model=ModelRef(provider="anthropic", id="claude-sonnet-4-6"),
            extensions=(extension, extension),
        )
        with pytest.raises(ValueError, match="duplicate agent extension"):
            await backend.agents.create(
                Scope.platform(reason="test duplicate namespace"),
                spec,
                key="key",
            )
    assert transport.requests == []


@pytest.mark.parametrize("defect", [None, "disabled", "ask", "unknown", "unresolved", "duplicate"])
def test_exact_mcp_toolset_representation_requires_resolved_catalogue_authority(defect):
    catalogue = frozenset({"describe_agent", "list_my_sessions"})
    defaults = {"enabled": True, "permission_policy": {"type": "always_allow"}}
    configs = []
    if defect == "disabled":
        configs = [{"name": "describe_agent", "enabled": False}]
    elif defect == "ask":
        configs = [{"name": "describe_agent", "permission_policy": {"type": "always_ask"}}]
    elif defect == "unknown":
        configs = [{"name": "not_on_the_server", "enabled": True}]
    elif defect == "unresolved":
        defaults = {}
    elif defect == "duplicate":
        configs = [{"name": "describe_agent"}, {"name": "describe_agent"}]
    native = AgentToolsConfig.model_validate(
        {
            "tools": [
                {
                    "type": "mcp_toolset",
                    "mcp_server_name": "daimon-mcp",
                    "configs": configs,
                    "default_config": defaults,
                }
            ]
        }
    )
    # Legacy/worker projection remains unchanged, even when a full-mode proof exists.
    assert native.intent() == () and native.has_unrepresented_toolset()
    if defect:
        with pytest.raises(ValueError):
            native.intent(exact=True, mcp_catalogues={"daimon-mcp": catalogue})
    else:
        tools = native.intent(exact=True, mcp_catalogues={"daimon-mcp": catalogue})
        assert len(tools) == 1 and tools[0].kind == "mcp_toolset" and tools[0].permission == "auto"
        assert not native.has_unrepresented_toolset(mcp_catalogues={"daimon-mcp": catalogue})


@pytest.mark.parametrize("policy", ["always_allow", "always_ask"])
def test_per_tool_mcp_permissions_keep_siblings_and_never_relax_ask(policy):
    from mux.drivers.anthropic.schemas import MCPToolset, mcp_permissions

    tool = MCPToolset.model_validate(
        {
            "type": "mcp_toolset",
            "mcp_server_name": "daimon-mcp",
            "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
            "configs": [{"name": "add_skill", "permission_policy": {"type": policy}}],
        }
    )
    assert mcp_permissions(tool, frozenset({"add_skill", "call_tool"})) == {
        "add_skill": "ask" if policy == "always_ask" else "auto",
        "call_tool": "auto",
    }
    # Ordinary projection still refuses to erase an approval requirement.
    config = AgentToolsConfig(tools=[tool])
    if policy == "always_ask":
        with pytest.raises(ValueError):
            config.intent(
                exact=True, mcp_catalogues={"daimon-mcp": frozenset({"add_skill", "call_tool"})}
            )


@pytest.mark.parametrize("tool", ["web_fetch", "web_search"])
@pytest.mark.parametrize(
    "sources", [{"type": "any"}, ["https://docs.example.com"], {"allowed": [], "blocked": None}]
)
def test_a_url_tool_echo_with_url_sources_round_trips_exactly(tool, sources):
    """SMOKE d75 J4: MA echoes url_sources on a URL tool's config (probe2)."""
    from mux.drivers.anthropic.schemas import AgentToolsConfig

    echoed = {
        "tools": [
            {
                "type": "agent_toolset_20260401",
                "configs": [
                    {"name": "read", "enabled": True, "type": "read"},
                    {"name": tool, "enabled": False, "type": tool, "url_sources": sources},
                ],
            }
        ]
    }
    config = AgentToolsConfig.model_validate(echoed)
    assert config.model_dump(mode="json", exclude_unset=True) == echoed
    # Never invented: a config without it dumps without it.
    bare = AgentToolsConfig.model_validate(
        {"tools": [{"type": "agent_toolset_20260401", "configs": [{"name": tool}]}]}
    )
    assert (
        "url_sources"
        not in bare.model_dump(mode="json", exclude_unset=True)["tools"][0]["configs"][0]
    )


def test_url_sources_on_a_non_url_tool_and_other_unknown_keys_stay_refused():
    from mux.drivers.anthropic.schemas import AgentToolsConfig
    from pydantic import ValidationError

    for config in ({"name": "bash", "url_sources": {"type": "any"}}, {"name": "web_fetch", "x": 1}):
        with pytest.raises(ValidationError):
            AgentToolsConfig.model_validate(
                {"tools": [{"type": "agent_toolset_20260401", "configs": [config]}]}
            )
