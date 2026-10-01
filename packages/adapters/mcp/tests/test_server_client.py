"""create_mcp_app's fallback AsyncAnthropic client: shared retry budget (D-05).

The factory's `anthropic` kwarg is boundary-stubbed by every other test in
this package (`build_stub_anthropic()`), so the fallback construction at
`server.py`'s `effective_anthropic = AsyncAnthropic(...)` has no reachable
seam from the returned `app` object itself -- `create_mcp_app` deliberately
does not expose the constructed client or the `McpRuntime` it feeds. This
test observes the fallback by patching the `AsyncAnthropic` name imported
into `server.py` with a subclass that records its constructor kwargs, then
constructs a real client via `super().__init__` so the rest of the factory
(which uses the client normally) is unaffected.
"""

from __future__ import annotations

from unittest.mock import patch

from anthropic import AsyncAnthropic
from daimon.adapters.mcp.server import create_mcp_app
from daimon.core.config import AnthropicSettings, DatabaseSettings, McpSettings, Settings
from daimon.core.constants import MA_MAX_RETRIES
from pydantic import HttpUrl, PostgresDsn, SecretStr


def _settings() -> Settings:
    return Settings(
        database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
        anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
        mcp=McpSettings(
            jwt_secret=SecretStr("a" * 32),
            public_url=HttpUrl("https://x/mcp"),
        ),
        discord=None,
    )


def test_fallback_anthropic_client_gets_the_shared_retry_budget() -> None:
    captured_kwargs: dict[str, object] = {}

    class _SpyAsyncAnthropic(AsyncAnthropic):
        def __init__(self, **kwargs: object) -> None:
            captured_kwargs.update(kwargs)
            super().__init__(**kwargs)  # pyright: ignore[reportArgumentType]  # spy forwards whatever the factory passed

    with patch("daimon.adapters.mcp.server.AsyncAnthropic", _SpyAsyncAnthropic):
        create_mcp_app(settings=_settings())

    assert captured_kwargs.get("max_retries") == MA_MAX_RETRIES, (
        "the MCP server's fallback client must carry the same retry budget "
        "every other adapter runtime passes"
    )
