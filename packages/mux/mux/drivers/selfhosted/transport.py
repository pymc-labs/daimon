"""Authenticated direct/Foundry calls with bounded, cursor-safe replay retries."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import math
import random
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Protocol, cast
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

import httpx
from pydantic import JsonValue, TypeAdapter

from mux.contracts.errors import ProviderErrorCategory
from mux.drivers.selfhosted.config import HarnessConfig
from mux.errors import ProviderError

_JSON = TypeAdapter[JsonValue](JsonValue)
TokenProvider = Callable[[], Awaitable[str]]


def sandbox_id(session_id: str) -> str:
    return "sh-" + hashlib.sha256(session_id.encode()).hexdigest()[:40]


def secure_url(value: str) -> bool:
    url = urlsplit(value)
    if url.scheme == "https":
        return True
    if url.scheme != "http":
        return False
    if url.hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(url.hostname or "").is_loopback
    except ValueError:
        return False


class Transport(Protocol):
    async def request(
        self,
        method: str,
        path: str,
        *,
        session_id: str,
        after_seq: int,
        body: Mapping[str, JsonValue] | None = None,
        mcp_authorization: str | None = None,
        deadline: datetime | None = None,
    ) -> JsonValue: ...


def checked(response: httpx.Response) -> None:
    if response.is_success:
        return
    categories: dict[int, ProviderErrorCategory] = {
        400: "invalid_request",
        401: "auth",
        403: "permission",
        404: "not_found",
        409: "conflict",
        422: "invalid_request",
        429: "rate_limited",
    }
    raise ProviderError(
        categories.get(response.status_code, "upstream"),
        retryable=response.status_code in (429, 502, 503, 504),
        native_code=str(response.status_code),
    )


def retry_delay(response: httpx.Response, attempt: int) -> float:
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            value = float(retry_after)
            if math.isfinite(value):
                return min(30, max(0, value))
        except ValueError:
            try:
                return min(
                    30,
                    max(
                        0,
                        cast(datetime, parsedate_to_datetime(retry_after)).timestamp()
                        - datetime.now(UTC).timestamp(),
                    ),
                )
            except (ValueError, TypeError):
                pass
    return random.uniform(0.1, min(2.0, 0.1 * 2**attempt))


class DirectTransport:
    """Token providers refresh credentials per call; MCP authorization is never retained."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        base_url: str,
        token: str | None = None,
        token_provider: TokenProvider | None = None,
        session_pool: bool = False,
        allow_insecure_http: bool = False,
    ) -> None:
        if (token is None) == (token_provider is None) or (token is not None and not token.strip()):
            raise ValueError("exactly one harness token or token provider is required")
        url = httpx.URL(base_url)
        try:
            HarnessConfig(
                harness_url=base_url,
                auth_ref="TRANSPORT_TOKEN",
                allow_insecure_http=allow_insecure_http,
            )
        except ValueError:
            raise ValueError(
                "harness URL must use HTTPS except on loopback or opted-in private endpoints"
            ) from None
        if session_pool and (url.scheme != "https" or token_provider is None):
            raise ValueError("session pools require HTTPS and a refreshing Entra token provider")
        self._client, self._base_url = client, base_url.rstrip("/")
        self._token, self._token_provider = token, token_provider
        self._session_pool = session_pool

    async def _request(
        self,
        method: str,
        url: str,
        body: Mapping[str, JsonValue] | None,
        *,
        mcp_authorization: str | None = None,
        replay: bool = False,
        deadline: datetime | None = None,
    ) -> JsonValue:
        # C3.9a covers the credential carrier hop as well as harness→MCP.
        # Refuse before refreshing the harness token or invoking the HTTP client.
        if mcp_authorization is not None and not secure_url(url):
            raise ProviderError(
                "permission",
                retryable=False,
                native_code="C3.9a_mcp_authorization_requires_https_or_loopback",
            )
        if mcp_authorization is not None and (
            not mcp_authorization.startswith("Bearer ")
            or not mcp_authorization.removeprefix("Bearer ").strip()
            or "\n" in mcp_authorization
            or "\r" in mcp_authorization
        ):
            raise ValueError("invalid per-turn MCP authorization")
        for attempt in range(6):
            token = await self._token_provider() if self._token_provider else self._token
            if not token or not token.strip():
                raise ValueError("harness authentication is required")
            headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}
            if mcp_authorization is not None:
                headers["X-Daimon-MCP-Authorization"] = mcp_authorization
            remaining = (
                (deadline - datetime.now(UTC)).total_seconds() if deadline is not None else None
            )
            if remaining is not None and remaining <= 0:
                raise ProviderError(
                    "transient_network", retryable=True, native_code="polling_deadline"
                )
            try:
                async with asyncio.timeout(remaining):
                    response = await self._client.request(
                        method,
                        url,
                        json=body,
                        headers=headers,
                        follow_redirects=False,
                    )
                if replay and response.status_code in (429, 502, 503, 504) and attempt < 5:
                    delay = retry_delay(response, attempt)
                    remaining = (
                        (deadline - datetime.now(UTC)).total_seconds()
                        if deadline is not None
                        else delay
                    )
                    await asyncio.sleep(max(0, min(delay, remaining)))
                    continue
                checked(response)
                return _JSON.validate_json(response.content)
            except (httpx.HTTPError, TimeoutError):
                if replay and attempt < 5:
                    delay = random.uniform(0.1, min(2.0, 0.1 * 2**attempt))
                    remaining = (
                        (deadline - datetime.now(UTC)).total_seconds()
                        if deadline is not None
                        else delay
                    )
                    await asyncio.sleep(max(0, min(delay, remaining)))
                    continue
                raise ProviderError("transient_network", retryable=True) from None
            except ValueError:
                raise ProviderError(
                    "upstream", retryable=True, native_code="malformed_response"
                ) from None
        raise AssertionError("retry loop exhausted")

    async def request(
        self,
        method: str,
        path: str,
        *,
        session_id: str,
        after_seq: int,
        body: Mapping[str, JsonValue] | None = None,
        mcp_authorization: str | None = None,
        deadline: datetime | None = None,
    ) -> JsonValue:
        if not session_id.strip():
            raise ValueError("session id is required")
        if not 0 <= after_seq <= 2**53:
            raise ValueError("invalid persisted high-water")
        parsed = urlsplit(path)
        if parsed.scheme or parsed.netloc or parsed.fragment or not parsed.path.startswith("/v0/"):
            raise ValueError("direct transport requires an absolute C3 path")
        query = parse_qsl(parsed.query, keep_blank_values=True)
        if any(key.lower() == "session_id" for key, _ in query):
            raise ValueError("session context is derived from the mux session")
        fence = max([after_seq, *(int(value) for key, value in query if key == "after_seq")])
        query = [(key, value) for key, value in query if key != "after_seq"]
        query.insert(0, ("after_seq", str(fence)))
        query.append(("session_id", session_id))
        path = parsed.path + "?" + urlencode(query)
        turn = method == "POST" and parsed.path == "/v0/turns"
        if mcp_authorization is not None and not turn:
            raise ValueError("MCP authorization is only valid for turn submission")
        if self._session_pool:
            if any(key.lower() == "identifier" for key, _ in query):
                raise ValueError("session pool identifier is derived from the mux session")
            query.append(("identifier", sandbox_id(session_id)))
            path = parsed.path + "?" + urlencode(query)
        return await self._request(
            method,
            self._base_url + path,
            body,
            mcp_authorization=mcp_authorization,
            replay=method == "GET",
            deadline=deadline,
        )


class FoundryInvocationsTransport(DirectTransport):
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        project_endpoint: str,
        agent_name: str,
        token: str | None = None,
        token_provider: TokenProvider | None = None,
        api_version: str = "v1",
    ) -> None:
        super().__init__(
            client, base_url=project_endpoint, token=token, token_provider=token_provider
        )
        if not agent_name.strip() or not api_version.strip():
            raise ValueError("Foundry agent name and API version are required")
        self._agent_name, self._api_version = agent_name, api_version

    async def request(
        self,
        method: str,
        path: str,
        *,
        session_id: str,
        after_seq: int,
        body: Mapping[str, JsonValue] | None = None,
        mcp_authorization: str | None = None,
        deadline: datetime | None = None,
    ) -> JsonValue:
        if not session_id.strip():
            raise ValueError("session id is required")
        if not 0 <= after_seq <= 2**53:
            raise ValueError("invalid persisted high-water")
        parsed = urlsplit(path)
        query_params = parse_qsl(parsed.query, keep_blank_values=True)
        if any(key.lower() == "session_id" for key, _ in query_params):
            raise ValueError("session context is derived from the mux session")
        fence = max([after_seq, *(int(value) for key, value in query_params if key == "after_seq")])
        query_params = [(key, value) for key, value in query_params if key != "after_seq"]
        query_params.insert(0, ("after_seq", str(fence)))
        query_params.append(("session_id", session_id))
        turn = method == "POST" and parsed.path == "/v0/turns"
        if mcp_authorization is not None and not turn:
            raise ValueError("MCP authorization is only valid for turn submission")
        envelope: dict[str, JsonValue] = {
            "c3": {
                "method": method,
                "path": parsed.path,
                "query": dict(query_params),
                "body": dict(body) if body is not None else None,
            }
        }
        query = urlencode(
            {
                "api-version": self._api_version,
                "agent_session_id": sandbox_id(session_id),
                "session_id": session_id,
            }
        )
        url = (
            f"{self._base_url}/agents/{quote(self._agent_name, safe='')}"
            f"/endpoint/protocols/invocations?{query}"
        )
        return await self._request(
            "POST",
            url,
            envelope,
            mcp_authorization=mcp_authorization,
            replay=method == "GET",
            deadline=deadline,
        )
