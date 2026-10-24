"""Native projection and compilation must preserve the exact permission ceiling."""

import json
from pathlib import Path

import pytest
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ModelRef
from mux.contracts.resources import AgentSpec, ToolSpec
from mux.drivers.anthropic.resources.agents import agent_payload
from mux.drivers.anthropic.schemas import agent_tools


def project(configs, defaults):
    return agent_tools(
        ExtensionConfig(
            namespace="anthropic.agent_tools",
            version=1,
            value={
                "tools": [
                    {
                        "type": "agent_toolset_20260401",
                        "configs": configs,
                        "default_config": defaults,
                    }
                ]
            },
        )
    )


@pytest.mark.parametrize(
    "configs,defaults,expected,missing",
    [
        ([], {}, (), True),
        ([{"name": "read"}], {}, (), True),
        ([{"name": "bash", "enabled": False}], {"enabled": True}, (), True),
        ([{"name": "read"}], {"enabled": False}, (), False),
        ([{"name": "read", "enabled": True}], {"enabled": False}, (), True),
        (
            [{"name": "read", "enabled": True, "permission_policy": {"type": "always_ask"}}],
            {"enabled": False, "permission_policy": {"type": "always_allow"}},
            (("read", "ask"),),
            False,
        ),
        (
            [{"name": "read", "enabled": True, "permission_policy": {"type": "always_allow"}}],
            {"enabled": False, "permission_policy": {"type": "always_ask"}},
            (("read", "auto"),),
            False,
        ),
        (
            [{"name": "read", "enabled": True}, {"name": "bash", "enabled": False}],
            {"enabled": False, "permission_policy": {"type": "always_ask"}},
            (("read", "ask"),),
            False,
        ),
        (
            [{"name": "bash", "enabled": False}, {"name": "bash", "enabled": True}],
            {"enabled": False, "permission_policy": {"type": "always_allow"}},
            (),
            False,
        ),
        (
            [
                {"name": "read", "enabled": True, "permission_policy": {"type": "always_ask"}},
                {"name": "read", "enabled": True},
            ],
            {"enabled": False, "permission_policy": {"type": "always_allow"}},
            (("read", "ask"),),
            False,
        ),
    ],
)
def test_projection_requires_complete_controls_and_uses_strictest_duplicates(
    configs, defaults, expected, missing
):
    native = project(configs, defaults)
    assert tuple((t.name, t.permission) for t in native.intent()) == expected
    assert native.has_unrepresented_toolset() is missing


@pytest.mark.parametrize("permission,policy", [("auto", "always_allow"), ("ask", "always_ask")])
def test_read_only_neutral_grant_compiles_to_only_read(permission, policy):
    spec = AgentSpec(
        name="reader",
        model=ModelRef(provider="anthropic", id="claude-haiku-5-5"),
        tools=(ToolSpec(name="read", kind="builtin", permission=permission),),
    )
    tools = agent_payload(spec)["tools"]
    assert tools == [
        {
            "type": "agent_toolset_20260401",
            "default_config": {"enabled": False, "permission_policy": {"type": "always_ask"}},
            "configs": [{"name": "read", "enabled": True, "permission_policy": {"type": policy}}],
        }
    ]
    assert (
        agent_tools(
            ExtensionConfig(namespace="anthropic.agent_tools", version=1, value={"tools": tools})
        ).intent()
        == spec.tools
    )


@pytest.mark.parametrize(
    "fault", [None, "enabled", "permission", "unknown", "duplicates", "implicit"]
)
def test_native_readback_canonicalization_never_erases_a_widening(fault):
    from copy import deepcopy

    from mux.drivers.anthropic.resources.agents import native_tools_match

    expected = [
        {
            "type": "agent_toolset_20260401",
            "default_config": {"enabled": False, "permission_policy": {"type": "always_ask"}},
            "configs": [
                {"name": "read", "enabled": True, "permission_policy": {"type": "always_ask"}}
            ],
        }
    ]
    observed = deepcopy(expected)
    observed[0]["configs"][0]["type"] = "read"
    observed[0]["configs"].insert(
        0,
        {
            "name": "bash",
            "type": "bash",
            "enabled": False,
            "permission_policy": {"type": "always_allow"},
        },
    )
    if fault == "enabled":
        observed[0]["configs"][0]["enabled"] = True
    elif fault == "permission":
        observed[0]["configs"][1]["permission_policy"]["type"] = "always_allow"
    elif fault == "unknown":
        observed[0]["configs"][0]["scope"] = "unrestricted"
    elif fault == "duplicates":
        observed[0]["configs"].append(
            {"name": "bash", "enabled": True, "permission_policy": {"type": "always_allow"}}
        )
    elif fault == "implicit":
        observed[0]["default_config"]["enabled"] = True
    assert native_tools_match(expected, observed) is (fault is None)


@pytest.mark.parametrize("policy,permission", [("always_allow", "auto"), ("always_ask", "ask")])
def test_recorded_resolved_defaults_project_unlisted_builtins_exactly(policy, permission):
    tools = json.loads(
        (Path(__file__).parent / "fixtures/anthropic_retrieved_builtin_tools.json").read_text()
    )
    assert len(tools[0]["configs"]) == 6
    tools[0]["default_config"]["permission_policy"]["type"] = policy
    # An explicit disabled tool stays disabled; explicit policy overrides the default.
    tools[0]["configs"][0]["enabled"] = False
    native = agent_tools(
        ExtensionConfig(namespace="anthropic.agent_tools", version=1, value={"tools": tools})
    )
    projected = {tool.name: tool.permission for tool in native.intent()}
    assert projected == {
        "read": "auto",
        "edit": "auto",
        "grep": "auto",
        "glob": "auto",
        "write": "auto",
        "web_fetch": permission,
        "web_search": permission,
    }
    assert not native.has_unrepresented_toolset()


@pytest.mark.parametrize(
    "defaults", [{"enabled": True}, {"permission_policy": {"type": "always_allow"}}, {}]
)
def test_unresolved_defaults_do_not_grant_unlisted_builtins(defaults):
    native = project([], defaults)
    assert native.intent() == ()
    assert native.has_unrepresented_toolset()


@pytest.mark.parametrize(
    "identity,represented",
    [
        ({"type": "service_account"}, True),
        ({"type": "aws_role", "role_arn": "arn:aws:iam::000000000000:role/example"}, False),
        ({"type": "service_account", "scope": "all"}, False),
        ({"type": "future"}, False),
        ({"type": "user"}, False),
        (None, False),
    ],
)
def test_only_resolved_platform_identity_is_nonauthority_readback(identity, represented):
    from mux.contracts.ids import Scope
    from mux.drivers.anthropic.resources.agents import UNCLASSIFIED_SOURCE_NAMESPACE, agent_record
    from mux.testing.drivers.anthropic.ma_models import ma_agent

    native = ma_agent().model_copy(update={"execution_identity": identity})
    record = agent_record(
        native,
        "org",
        Scope(
            tenant_id="tenant", account_id="account", principal_id="host", authorization_id="auth"
        ),
    )
    extensions = {extension.namespace: extension for extension in record.spec.extensions}
    assert (UNCLASSIFIED_SOURCE_NAMESPACE not in extensions) is represented
    if not represented:
        assert extensions[UNCLASSIFIED_SOURCE_NAMESPACE].value == {"fields": ["execution_identity"]}
