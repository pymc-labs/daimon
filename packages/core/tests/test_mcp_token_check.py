"""`check_token`: only a definite 401/403 is a verdict, and it says whether sign-in works."""

from __future__ import annotations

import pytest
from daimon.core.mcp_oauth.discovery import McpProbe, probe_bearer_token
from daimon.core.mcp_token_check import (
    TokenRejection,
    check_token,
    is_token_rejected,
    rejected_token_message,
)


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


@pytest.mark.parametrize(
    ("metadata_url", "supports_sign_in"),
    [("https://x.example/.well-known/oauth-protected-resource", True), (None, False)],
)
async def test_a_rejection_says_whether_the_server_offers_sign_in(
    metadata_url: str | None, supports_sign_in: bool
) -> None:
    async def probe(_url: str, _token: str) -> McpProbe:
        return McpProbe(status_code=401, resource_metadata_url=metadata_url)

    rejection = await check_token(probe, mcp_server_url="https://x.example/mcp", token="t")
    assert rejection == TokenRejection(supports_sign_in=supports_sign_in), (
        "advertised OAuth resource metadata is how the server says it signs people in"
    )


def test_the_rejection_message_offers_sign_in_only_where_it_works() -> None:
    plain = rejected_token_message(TokenRejection(supports_sign_in=False))
    assert plain == (
        "That token didn't work. Nothing was saved.\n\nCheck the token and try the form again."
    ), "two lines, a blank line apart"
    with_sign_in = rejected_token_message(TokenRejection(supports_sign_in=True))
    assert with_sign_in.startswith(plain + "\n\n")
    assert "sign in" in with_sign_in.split("\n\n")[-1], "the hint is its own line"
