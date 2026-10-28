"""Only explicitly configured bridge memory readers disappear from session tools."""

from daimon.core.repo_memory_tools import suppress_legacy_readers


def test_suppression_preserves_other_readers_and_native_family() -> None:
    source = [
        {
            "type": "mcp_toolset",
            "mcp_server_name": "legacy",
            "default_config": {"enabled": True},
            "configs": [
                {"name": "repo_memory_view", "enabled": True},
                {"name": "search", "enabled": True},
            ],
        },
        {"type": "mcp_toolset", "mcp_server_name": "daimon-mcp"},
    ]
    result = suppress_legacy_readers(source, ("legacy",))
    configs = {c["name"]: c for c in result[0]["configs"]}
    assert configs["repo_memory_view"]["enabled"] is False
    assert configs["repo_memory_list"]["enabled"] is False
    assert configs["search"]["enabled"] is True
    assert result[1] == source[1]
    assert source[0]["configs"][0]["enabled"] is True
    assert suppress_legacy_readers(source, ()) == source


def test_suppression_survives_native_and_mux_permission_projection() -> None:
    from anthropic.types.beta.beta_managed_agents_mcp_toolset import BetaManagedAgentsMCPToolset
    from mux.drivers.anthropic.schemas import MCPToolset, mcp_permissions
    from pydantic import TypeAdapter

    payload = suppress_legacy_readers(
        [
            {
                "type": "mcp_toolset",
                "mcp_server_name": "legacy",
                "configs": None,
                "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
            }
        ],
        ("legacy",),
    )[0]
    native = TypeAdapter(BetaManagedAgentsMCPToolset).validate_python(payload)
    assert {c.name for c in native.configs if c.enabled is False} == {
        "repo_memory_list",
        "repo_memory_view",
    }
    projected = mcp_permissions(
        MCPToolset.model_validate(payload),
        frozenset({"repo_memory_list", "repo_memory_view", "search"}),
    )
    assert projected == {
        "repo_memory_list": "disabled",
        "repo_memory_view": "disabled",
        "search": "auto",
    }
