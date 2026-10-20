"""Direct and Foundry transports share C3 JSON polling semantics."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Protocol
from urllib.parse import parse_qsl, quote, urlsplit

import httpx
from pydantic import JsonValue, TypeAdapter

from mux.contracts.errors import ProviderErrorCategory
from mux.errors import ProviderError

_JSON = TypeAdapter[JsonValue](JsonValue)


def sandbox_id(session_id: str) -> str:
    return "sh-" + hashlib.sha256(session_id.encode()).hexdigest()[:40]


class Transport(Protocol):
    async def request(
        self,
        method: str,
        path: str,
        *,
        session_id: str,
        body: Mapping[str, JsonValue] | None = None,
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


class DirectTransport:
    """One authorized endpoint; client lifetime belongs to the host."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        base_url: str,
        token: str,
        mcp_authorization: str | None = None,
    ) -> None:
        if not token.strip():
            raise ValueError("harness authentication is required")
        url = httpx.URL(base_url)
        if (
            url.scheme not in ("https", "http")
            or not url.host
            or url.userinfo
            or url.query
            or url.fragment
        ):
            raise ValueError("invalid harness URL")
        self._client, self._base_url = client, base_url.rstrip("/")
        self._headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}
        if mcp_authorization is not None and (
            not mcp_authorization.startswith("Bearer ")
            or not mcp_authorization.removeprefix("Bearer ").strip()
            or "\n" in mcp_authorization
            or "\r" in mcp_authorization
        ):
            raise ValueError("invalid per-turn MCP authorization")
        self._mcp_authorization = mcp_authorization

    async def _request(
        self, method: str, url: str, body: Mapping[str, JsonValue] | None, *, turn: bool = False
    ) -> JsonValue:
        headers = dict(self._headers)
        if turn and self._mcp_authorization is not None:
            headers["X-Daimon-MCP-Authorization"] = self._mcp_authorization
        try:
            response = await self._client.request(
                method,
                url,
                json=body,
                headers=headers,
                follow_redirects=False,
            )
            checked(response)
            return _JSON.validate_json(response.content)
        except httpx.HTTPError:
            raise ProviderError("transient_network", retryable=True) from None
        except ValueError:
            raise ProviderError(
                "upstream", retryable=False, native_code="malformed_response"
            ) from None

    async def request(
        self,
        method: str,
        path: str,
        *,
        session_id: str,
        body: Mapping[str, JsonValue] | None = None,
    ) -> JsonValue:
        return await self._request(
            method,
            self._base_url + path,
            body,
            turn=method == "POST" and urlsplit(path).path == "/v0/turns",
        )


class FoundryInvocationsTransport(DirectTransport):
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        project_endpoint: str,
        agent_name: str,
        token: str,
        mcp_authorization: str | None = None,
    ) -> None:
        super().__init__(
            client, base_url=project_endpoint, token=token, mcp_authorization=mcp_authorization
        )
        if not agent_name.strip():
            raise ValueError("Foundry agent name is required")
        self._agent_name = agent_name

    async def request(
        self,
        method: str,
        path: str,
        *,
        session_id: str,
        body: Mapping[str, JsonValue] | None = None,
    ) -> JsonValue:
        parsed = urlsplit(path)
        envelope: dict[str, JsonValue] = {
            "c3": {
                "method": method,
                "path": parsed.path,
                "query": dict(parse_qsl(parsed.query)),
                "body": dict(body) if body is not None else None,
            }
        }
        url = (
            f"{self._base_url}/agents/{quote(self._agent_name, safe='')}"
            "/endpoint/protocols/invocations"
            f"?agent_session_id={sandbox_id(session_id)}"
        )
        return await self._request(
            "POST", url, envelope, turn=method == "POST" and parsed.path == "/v0/turns"
        )
