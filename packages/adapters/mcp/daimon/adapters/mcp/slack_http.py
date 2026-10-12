"""One aiohttp session for every Slack Web API call the MCP process makes.

Without an injected session slack_sdk opens and closes one per API call,
paying a fresh TCP and TLS handshake each time. The session holds no token, so
the per-call token clients built around it stay per call.
"""

from __future__ import annotations

import aiohttp

# An injected session's timeout replaces AsyncWebClient's own; keep its 30 s.
_SLACK_TIMEOUT_S = 30


class SlackHttpSession:
    """Opens the session on first use, inside the serving loop; the app
    factory runs before that loop exists."""

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None

    def get(self) -> aiohttp.ClientSession:
        """The open session, opening it if needed."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=_SLACK_TIMEOUT_S),
                # A cookie one workspace's call sets must not ride along on another's.
                cookie_jar=aiohttp.DummyCookieJar(),
            )
        return self._session

    async def close(self) -> None:
        """Close the session at app shutdown; a later `get` opens a new one."""
        if self._session is not None:
            await self._session.close()
            self._session = None
