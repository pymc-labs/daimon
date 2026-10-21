"""Private HTTP requires explicit exact destination grants at each boundary."""

from uuid import uuid4

import pytest
from daimon.core.config import HarnessAuthBinding, McpCredentialBinding
from daimon.core.turn.selfhosted_tools import _mcp_url, build_turn_tools


def test_harness_http_is_opt_in():
    kwargs = dict(
        name="DAIMON_SELFHOSTED__HARNESS_TOKEN_TEST", origin="http://harness:8088", tenants=("*",)
    )
    with pytest.raises(ValueError):
        HarnessAuthBinding(**kwargs)
    assert HarnessAuthBinding(**kwargs, allow_insecure_http=True).origin == "http://harness:8088"
    with pytest.raises(ValueError):
        HarnessAuthBinding(
            **{**kwargs, "origin": "http://user:pass@harness:8088"}, allow_insecure_http=True
        )
    with pytest.raises(ValueError):
        McpCredentialBinding(
            name="HARNESS_MCP_CRED_TEST", origin="http://harness:8088", tenants=("*",)
        )


@pytest.mark.parametrize("host", ["mcp", "mcp.daimon.svc.cluster.local", "10.0.0.5"])
def test_mcp_credentials_require_tls_even_with_exact_grant(host):
    with pytest.raises(ValueError, match="HTTPS"):
        build_turn_tools(
            daimon_mcp_url=f"http://{host}:8765/mcp",
            tenant_id=uuid4(),
            platform="cli",
            channel_id="test",
            agent_names=(),
            skill_bundles={},
            mcp_allowed_http_hosts=(host,),
        )


@pytest.mark.parametrize("port,normalized", [(443, ":443"), (80, ""), (8088, ":8088")])
def test_http_origin_preserves_original_scheme_port(port, normalized):
    from daimon.core.config import SelfHostedSettings
    from daimon.core.turn.errors import AdmissionDenied
    from daimon.core.turn.selfhosted_deployment import validate_harness_auth
    from mux.drivers.selfhosted.config import HarnessConfig

    binding = HarnessAuthBinding(
        name="DAIMON_SELFHOSTED__HARNESS_TOKEN_TEST",
        origin=f"http://harness:{port}",
        tenants=("*",),
        allow_insecure_http=True,
    )
    assert binding.origin == f"http://harness{normalized}"
    settings = SelfHostedSettings(single_tenant=True, harness_auth_bindings=(binding,))
    validate_harness_auth(
        HarnessConfig(
            harness_url=f"http://harness:{port}",
            auth_ref=binding.name,
            allow_insecure_http=True,
        ),
        settings,
        "tenant",
    )
    if port != 80:
        with pytest.raises(AdmissionDenied):
            validate_harness_auth(
                HarnessConfig(
                    harness_url="http://harness",
                    auth_ref=binding.name,
                    allow_insecure_http=True,
                ),
                settings,
                "tenant",
            )


def test_chart_secret_name_is_not_a_bearer_reference():
    with pytest.raises(ValueError, match="operator token variable"):
        HarnessAuthBinding(name="DAIMON_HARNESS_TOKEN", origin="https://harness", tenants=("*",))


@pytest.mark.parametrize("host", ["public.example.com", "169.254.169.254", "8.8.8.8"])
def test_http_credential_grant_cannot_authorize_external_or_metadata_hosts(host):
    with pytest.raises(ValueError):
        build_turn_tools(
            daimon_mcp_url=f"http://{host}:8765/mcp",
            tenant_id=uuid4(),
            platform="cli",
            channel_id="test",
            agent_names=(),
            skill_bundles={},
            mcp_allowed_http_hosts=(host,),
        )


@pytest.mark.parametrize(
    "host",
    [
        "metadata.google.internal",
        "metadata",
        "metadata.azure.internal",
        "instance-data",
        "instance-data.ec2.internal",
        "169.254.169.254",
        "[fd00:ec2::254]",
        "[::ffff:169.254.169.254]",
        "[::169.254.169.254]",
        "0xa9fea9fe",
    ],
)
@pytest.mark.parametrize("scheme", ["http", "https"])
def test_metadata_mcp_destinations_refused_even_without_credentials(host, scheme):
    from daimon.core.turn.selfhosted_tools import _check_http_destination

    url = f"{scheme}://{host}/mcp"
    with pytest.raises(ValueError, match="metadata"):
        _mcp_url(url)
    with pytest.raises(ValueError, match="metadata"):
        _check_http_destination(url, allowed_hosts=(host,))
