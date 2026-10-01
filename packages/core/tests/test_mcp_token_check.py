"""`is_token_rejected`: only a definite 401/403 is a verdict."""

from __future__ import annotations

from daimon.core.mcp_oauth.discovery import McpProbe, probe_bearer_token
from daimon.core.mcp_token_check import is_token_rejected


async def test_a_url_the_probe_will_not_contact_is_not_a_rejection() -> None:
    assert (
        await is_token_rejected(
            probe_bearer_token, mcp_server_url="http://10.0.0.5:6379/", token="t"
        )
        is False
    ), "http and private addresses are left to MA, as before the probe existed"


async def test_only_401_and_403_reject() -> None:
    async def probe(_url: str, _token: str) -> McpProbe:
        return McpProbe(status_code=500, resource_metadata_url=None)

    assert (
        await is_token_rejected(probe, mcp_server_url="https://x.example/mcp", token="t") is False
    )
