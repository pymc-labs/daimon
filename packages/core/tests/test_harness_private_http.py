"""Private HTTP requires explicit exact destination grants at each boundary."""

import pytest
from daimon.core.config import HarnessAuthBinding, McpCredentialBinding
from daimon.core.turn.selfhosted_tools import _mcp_url


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


def test_mcp_http_requires_exact_host():
    with pytest.raises(ValueError):
        _mcp_url("http://mcp:8765/mcp", credential=True)
    assert (
        _mcp_url("http://mcp:8765/mcp", credential=True, allowed_http_hosts=("mcp",))
        == "http://mcp:8765/mcp"
    )
    with pytest.raises(ValueError):
        _mcp_url("http://mcp.evil:8765/mcp", credential=True, allowed_http_hosts=("mcp",))


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
        _mcp_url(f"http://{host}:8765/mcp", credential=True, allowed_http_hosts=(host,))
