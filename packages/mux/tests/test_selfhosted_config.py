"""Shared resource validation rejects credential carriers before persistence or I/O."""

import pytest
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ModelRef
from mux.contracts.resources import AgentSpec, EnvironmentSpec
from mux.drivers.selfhosted.config import HarnessConfig, validate_resource_spec
from mux.errors import ProviderError
from mux.profiles.selfhosted import LOOP


def spec_for(vector):
    agent = {"name": "agent", "model": ModelRef(provider=LOOP.provider, id="gpt-5")}
    environment = {
        "name": "environment",
        "execution": "self_hosted",
        "native_config": {
            "namespace": "selfhosted.environment",
            "version": 1,
            "value": {"harness_url": "https://harness.example", "auth_ref": "HARNESS_TOKEN"},
        },
    }
    if vector == "repository_userinfo":
        environment["sources"] = [
            {
                "kind": "repository",
                "target_path": "work",
                "repository_url": "https://x-access-token:secret-marker@github.com/o/r",
            }
        ]
    elif vector == "repository_query":
        environment["sources"] = [
            {
                "kind": "repository",
                "target_path": "work",
                "repository_url": "https://github.com/o/r?token=secret-marker",
            }
        ]
    elif vector == "repository_fragment":
        environment["sources"] = [
            {
                "kind": "repository",
                "target_path": "work",
                "repository_url": "https://github.com/o/r#secret-marker",
            }
        ]
    elif vector == "source_bearer":
        environment["sources"] = [
            {"kind": "repository", "target_path": "work", "credential_ref": "Bearer secret-marker"}
        ]
    elif vector == "entra_token":
        environment["native_config"]["value"].update(
            auth_mode="entra", auth_ref="eyJhbGciOiJub25lIn0.eyJzdWIiOiIxIn0.signature"
        )
    elif vector == "bearer_token":
        environment["native_config"]["value"]["auth_ref"] = "Bearer secret-marker"
    elif vector in ("authorization", "cookie", "mcp_bearer", "mcp_url"):
        server = {"name": "tools", "url": "https://tools.example/mcp"}
        if vector == "mcp_bearer":
            server["credential_ref"] = "Bearer secret-marker"
        elif vector == "mcp_url":
            server["url"] = "https://secret-marker@tools.example/mcp"
        else:
            server["tool_policy"] = {"headers": {vector.title(): "opaque-value"}}
        agent["mcp_servers"] = [server]
    elif vector == "extensions":
        agent["extensions"] = [
            ExtensionConfig(
                namespace="anthropic.agent_tools", version=1, value={"opaque": "secret-marker"}
            )
        ]
    elif vector == "metadata":
        agent["metadata"] = {"api_key": "secret-marker"}
    elif vector == "instructions":
        agent["system"] = "Bearer secret-marker"
    elif vector == "model_options":
        agent["model"] = ModelRef(
            provider=LOOP.provider, id="gpt-5", options={"access_token": "secret-marker"}
        )
    else:
        raise AssertionError(vector)
    if vector.startswith(("repository", "source", "entra", "bearer")):
        return EnvironmentSpec.model_validate(environment)
    return AgentSpec.model_validate(agent)


@pytest.mark.parametrize(
    "vector",
    [
        "repository_userinfo",
        "repository_query",
        "repository_fragment",
        "source_bearer",
        "entra_token",
        "bearer_token",
        "authorization",
        "cookie",
        "mcp_bearer",
        "mcp_url",
        "extensions",
        "metadata",
        "instructions",
        "model_options",
    ],
)
def test_each_credential_carrier_is_refused_without_echoing_input(vector):
    with pytest.raises(ProviderError) as error:
        validate_resource_spec(spec_for(vector))
    assert error.value.category == "invalid_request"
    assert "secret-marker" not in str(error.value)


@pytest.mark.parametrize(
    "extra",
    [
        {"harness_url": "http://public.example"},
        {"harness_url": "http://public.example", "allow_insecure_http": True},
        {"harness_url": "http://harness"},
        {"auth_mode": "entra", "auth_ref": "not-an-audience"},
        {"agent_name": "ignored"},
        {"project_endpoint": "https://foundry.example"},
        {
            "transport": "foundry_invocations",
            "agent_name": "..",
            "project_endpoint": "https://foundry.example",
            "auth_mode": "entra",
            "auth_ref": "api://audience",
        },
    ],
)
def test_transport_requires_secure_explicit_routing(extra):
    with pytest.raises(ValueError):
        HarnessConfig.model_validate(
            {"harness_url": "https://harness.example", "auth_ref": "HARNESS_TOKEN", **extra}
        )


def test_internal_http_requires_explicit_permission_and_foundry_uses_one_schema():
    local = HarnessConfig(harness_url="http://127.0.0.1:8080", auth_ref="TOKEN")
    internal = HarnessConfig(
        harness_url="http://harness:8080", auth_ref="TOKEN", allow_insecure_http=True
    )
    foundry = HarnessConfig(
        harness_url="https://harness.example",
        auth_mode="entra",
        auth_ref="api://audience",
        transport="foundry_invocations",
        agent_name="daimon",
        project_endpoint="https://foundry.example/api/projects/test",
    )
    assert local.transport == internal.transport == "direct"
    assert HarnessConfig.model_validate(foundry.model_dump()) == foundry


@pytest.mark.parametrize(
    "credential_ref", ["MCP_TOKEN", "PATH", "DAIMON_MCP_AUTHORIZATION", "HARNESS_MCP_CRED_"]
)
def test_mcp_credentials_require_operator_scoped_environment_names(credential_ref):
    spec = AgentSpec.model_validate(
        {
            "name": "agent",
            "model": {"provider": LOOP.provider, "id": "gpt-5"},
            "mcp_servers": [
                {
                    "name": "tools",
                    "url": "https://tools.example/mcp",
                    "credential_ref": credential_ref,
                }
            ],
        }
    )
    with pytest.raises(ProviderError) as error:
        validate_resource_spec(spec)
    assert error.value.category == "invalid_request"


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("http://tools.internal/mcp", False),
        ("https://tools.example/mcp", True),
        ("http://127.0.0.1:8080/mcp", True),
    ],
)
def test_credentialed_mcp_requires_https_except_loopback(url, allowed):
    spec = AgentSpec.model_validate(
        {
            "name": "agent",
            "model": {"provider": LOOP.provider, "id": "gpt-5"},
            "mcp_servers": [
                {"name": "tools", "url": url, "credential_ref": "HARNESS_MCP_CRED_TOOLS"}
            ],
        }
    )
    if allowed:
        validate_resource_spec(spec)
    else:
        with pytest.raises(ProviderError) as error:
            validate_resource_spec(spec)
        assert error.value.category == "invalid_request"
