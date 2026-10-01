"""`daimon.core.mcp_oauth.urls`: which URLs daimon will contact at all."""

from __future__ import annotations

import pytest
from daimon.core.mcp_oauth.urls import McpUrlError, assert_public_host, assert_public_https_url


@pytest.mark.parametrize(
    "url",
    [
        "https://10.0.0.5:6379/",
        "https://169.254.169.254/computeMetadata/v1/",
        "https://127.0.0.1/mcp",
        "https://[::1]/mcp",
        "https://[fd00::1]/mcp",
        "https://localhost/mcp",
        "https://db.internal/mcp",
        "https://printer.local/mcp",
        "https://user:pw@mcp.notion.com/mcp",
        "https:///mcp",
        "https://2130706433/",
        "https://0x7f000001/",
        "https://127.1/",
        "https://localhost./",
        "https://db.internal./",
    ],
)
def test_non_public_or_credentialed_urls_are_refused(url: str) -> None:
    with pytest.raises(McpUrlError):
        assert_public_host(url, what="mcp server url")


def test_a_public_https_url_passes_and_plain_http_does_not() -> None:
    assert assert_public_https_url("https://mcp.notion.com/mcp") == "https://mcp.notion.com/mcp"
    assert assert_public_host("http://mcp.notion.com/mcp"), "the host check alone allows http"
    with pytest.raises(McpUrlError, match="https"):
        assert_public_https_url("http://mcp.notion.com/mcp")
