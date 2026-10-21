"""Shared resource validation rejects credential carriers before persistence or I/O."""

import pytest
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ModelRef
from mux.contracts.resources import AgentSpec, EnvironmentSpec
from mux.drivers.selfhosted.config import HarnessConfig, validate_resource_spec
from mux.errors import ProviderError
from mux.profiles.selfhosted import LOOP


def spec_for(vector):
    agent = {"name": "agent", "model": ModelRef(provider=LOOP.provider, id="gpt-6-luna")}
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
    elif vector == "model_options":
        agent["model"] = ModelRef(
            provider=LOOP.provider, id="gpt-6-luna", options={"access_token": "secret-marker"}
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
            "model": {"provider": LOOP.provider, "id": "gpt-6-luna"},
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
            "model": {"provider": LOOP.provider, "id": "gpt-6-luna"},
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


@pytest.mark.parametrize(
    "name", [".", "..", "agent.name", "_agent", "-agent", "a" * 64, "a/b", "a%2fb", "àgent", ""]
)
def test_foundry_agent_names_reject_unsafe_routes_and_invalid_lengths(name):
    with pytest.raises(ValueError):
        HarnessConfig(
            harness_url="https://harness.example",
            auth_mode="entra",
            auth_ref="api://audience",
            transport="foundry_invocations",
            agent_name=name,
            project_endpoint="https://foundry.example/api/projects/test",
        )


@pytest.mark.parametrize("name", ["a", "0", "Agent_0-name", "a" * 63])
def test_foundry_agent_name_boundaries_are_valid(name):
    config = HarnessConfig(
        harness_url="https://harness.example",
        auth_mode="entra",
        auth_ref="api://audience",
        transport="foundry_invocations",
        agent_name=name,
        project_endpoint="https://foundry.example/api/projects/test",
    )
    assert config.agent_name == name


@pytest.mark.parametrize(
    "prose", ["Do not print Bearer tokens.", "Explain Authorization and Cookie headers."]
)
def test_free_text_fields_accept_harmless_credential_prose(prose):
    validate_resource_spec(
        AgentSpec(
            name=prose,
            description=prose,
            system=prose,
            model=ModelRef(provider=LOOP.provider, id="gpt-6-luna"),
        )
    )
    validate_resource_spec(
        EnvironmentSpec(
            name=prose,
            description=prose,
            execution="self_hosted",
            native_config=ExtensionConfig(
                namespace="selfhosted.environment",
                version=1,
                value={
                    "harness_url": "https://harness.example",
                    "auth_ref": "TOKEN",
                    "packages_note": prose,
                },
            ),
        )
    )


@pytest.mark.parametrize(
    "requirement",
    [
        "pkg @ https://user:opaque-value@packages.example/pkg.whl",
        "--index-url https://opaque-value@packages.example/simple",
        "--extra-index-url https://packages.example/simple?token=opaque-value",
        "--extra-index-url https://packages.example/simple?api_key=opaque-value",
        "pkg @ git+https://user:opaque-value@git.example/repo.git",
        "git+ssh://user:opaque-value@git.example/repo.git#egg=pkg",
        "pkg @ https://packages.example/pkg.whl#access_token=opaque-value",
        "--extra-index-url https://packages.example/sk-secret-marker",
    ],
)
def test_package_credentials_are_rejected(requirement):
    spec = EnvironmentSpec(
        name="environment",
        execution="self_hosted",
        packages={"python": (requirement,)},
        native_config=ExtensionConfig(
            namespace="selfhosted.environment",
            version=1,
            value={"harness_url": "https://harness.example", "auth_ref": "TOKEN"},
        ),
    )
    with pytest.raises(ProviderError) as error:
        validate_resource_spec(spec)
    assert error.value.category == "invalid_request"
    assert "opaque-value" not in str(error.value)
    assert "secret-marker" not in str(error.value)


def test_public_package_references_remain_valid():
    validate_resource_spec(
        EnvironmentSpec(
            name="environment",
            execution="self_hosted",
            packages={
                "python": (
                    "pymc>=5",
                    "pkg @ https://packages.example/pkg.whl#sha256=abc123",
                    "pkg @ git+https://git.example/repo.git#subdirectory=python",
                    "--extra-index-url https://packages.example/simple",
                )
            },
            native_config=ExtensionConfig(
                namespace="selfhosted.environment",
                version=1,
                value={"harness_url": "https://harness.example", "auth_ref": "TOKEN"},
            ),
        )
    )


@pytest.mark.parametrize(
    "host",
    [
        "8.8.8.8",
        "134744072",
        "010.010.010.010",
        "0x08080808",
        "8.526344",
        "[2606:4700:4700::1111]",
        "[::ffff:8.8.8.8]",
        "169.254.1.1",
        "[fe80::1]",
        "100.64.0.1",
        "192.0.2.1",
        "[2001:db8::1]",
        "0",
        "[::]",
        "[ff02::1]",
    ],
)
def test_internal_http_never_admits_public_reserved_or_link_local_addresses(host):
    with pytest.raises(ValueError):
        HarnessConfig(harness_url=f"http://{host}:8080", auth_ref="TOKEN", allow_insecure_http=True)


@pytest.mark.parametrize(
    "host",
    [
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.1",
        "167772161",
        "0x0a000001",
        "012.0.0.1",
        "[fd00::1]",
        "[fc00::1]",
        "[::ffff:10.0.0.1]",
        "harness",
        "harness.svc.cluster.local",
    ],
)
def test_explicit_internal_http_admits_rfc1918_ula_and_cluster_names(host):
    config = HarnessConfig(
        harness_url=f"http://{host}:8080", auth_ref="TOKEN", allow_insecure_http=True
    )
    assert config.allow_insecure_http
    with pytest.raises(ValueError):
        HarnessConfig(harness_url=f"http://{host}:8080", auth_ref="TOKEN")


@pytest.mark.parametrize("host", ["127.0.0.1", "127.1", "2130706433", "0x7f000001", "[::1]"])
def test_loopback_http_remains_available_without_internal_override(host):
    HarnessConfig(harness_url=f"http://{host}:8080", auth_ref="TOKEN")


@pytest.mark.parametrize(
    "policy, valid",
    [
        ({"allowed_tools": ["read"], "read": "ask"}, True),
        ({"tools/*": "deny", "tools/read": "ask"}, True),
        ({"other/read": "ask"}, False),
        ({"allowed_tools": ["read"], "write": "auto"}, False),
        ({"allowed_tools": [], "*": "auto"}, False),
        ({"allowed_tools": "read"}, False),
        ({"allowed_tools": ["../read"]}, False),
        ({"read": "sometimes"}, False),
        ({"headers": {"Authorization": "opaque-value"}}, False),
    ],
)
def test_persisted_mcp_policy_has_closed_permission_schema(policy, valid):
    from mux.contracts.resources import MCPConnection

    spec = AgentSpec(
        name="agent",
        model=ModelRef(provider=LOOP.provider, id="gpt-6-luna"),
        mcp_servers=(
            MCPConnection(name="tools", url="https://tools.example/mcp", tool_policy=policy),
        ),
    )
    if valid:
        validate_resource_spec(spec)
    else:
        with pytest.raises(ProviderError):
            validate_resource_spec(spec)
