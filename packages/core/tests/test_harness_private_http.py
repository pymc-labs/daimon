"""Private HTTP requires explicit exact destination grants at each boundary."""

import pytest
from daimon.core.config import HarnessAuthBinding, McpCredentialBinding
from daimon.core.turn.selfhosted_tools import _mcp_url


def test_harness_http_is_opt_in():
    kwargs = dict(name="DAIMON_HARNESS_TOKEN", origin="http://harness:8088", tenants=("*",))
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
