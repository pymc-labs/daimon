"""Private SDK boundary. Construction and importing this module perform no I/O."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Protocol, cast, runtime_checkable

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai._interactions import APIConnectionError, APIError, APIStatusError
from pydantic import JsonValue

from mux.contracts.errors import ProviderErrorCategory
from mux.drivers.gemini.mcp_auth import (
    RedactedCredential,
    contains_secret,
    model_request,
    refresh_bound_request,
)
from mux.errors import ProviderError

API_REVISION = "2026-05-20"
type Object = dict[str, JsonValue]


@runtime_checkable
class TokenCountingTransport(Protocol):
    async def count_input_tokens(self, request: Mapping[str, JsonValue]) -> int: ...


class Transport(Protocol):
    async def create(self, request: Mapping[str, JsonValue]) -> Object: ...
    async def get(self, interaction_id: str) -> Object: ...
    async def cancel(self, interaction_id: str) -> Object: ...
    async def download_snapshot(self, environment_id: str) -> bytes: ...
    async def open_stream(
        self, interaction_id: str, *, after: str | None = None
    ) -> AsyncIterator[Object]: ...


def object_value(value: JsonValue) -> Object:
    if not isinstance(value, dict):
        raise ValueError("expected a provider object")
    return value


def string(value: JsonValue) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("expected a provider identity")
    return value


def network_transform_present(value: JsonValue, *, in_network: bool = False) -> bool:
    """Presence-only inspection; never serialize a potentially echoed secret."""
    if isinstance(value, dict):
        return (in_network and "transform" in value) or any(
            network_transform_present(child, in_network=in_network or key == "network")
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(network_transform_present(child, in_network=in_network) for child in value)
    return False


def storage_request_token(request: Mapping[str, JsonValue]) -> RedactedCredential | None:
    """Remember only the dispatched mount credential, within the transport lifetime."""
    environment = request.get("environment")
    network = environment.get("network") if isinstance(environment, dict) else None
    rules = network.get("allowlist") if isinstance(network, dict) else None
    for rule in rules if isinstance(rules, list) else []:
        if not isinstance(rule, dict) or rule.get("domain") != "storage.googleapis.com":
            continue
        transform = rule.get("transform")
        header = transform.get("Authorization") if isinstance(transform, dict) else None
        if isinstance(header, str) and header.startswith("Bearer ") and header[7:]:
            return RedactedCredential(header[7:])
    return None


def checked_storage_response(
    value: Object, *, protected: bool, tokens: set[RedactedCredential] | None = None
) -> Object:
    if protected and network_transform_present(value):
        raise ProviderError("permission", retryable=False, native_code="gcs_transform_echo")
    if protected and tokens and any(contains_secret(value, token) for token in tokens):
        raise ProviderError("permission", retryable=False, native_code="gcs_token_echo")
    return value


def normalize_error(error: Exception) -> ProviderError:
    status = (
        error.status_code
        if isinstance(error, APIStatusError)
        else (error.code if isinstance(error, genai_errors.APIError) else None)
    )
    categories: dict[int, ProviderErrorCategory] = {
        400: "invalid_request",
        401: "auth",
        403: "permission",
        404: "not_found",
        409: "conflict",
        422: "invalid_request",
        429: "rate_limited",
        503: "overloaded",
    }
    if isinstance(error, APIConnectionError | httpx.HTTPError):
        return ProviderError("transient_network", retryable=True)
    category = categories.get(status or 0, "upstream")
    return ProviderError(
        category,
        retryable=status == 429 or (status is not None and status >= 500),
        native_code="503" if status == 503 else None,
    )


async def provider_call[T](call: Awaitable[T]) -> T:
    try:
        return await call
    except (APIError, genai_errors.APIError, httpx.HTTPError) as error:
        raise normalize_error(error) from None


class OwnedStream[T](AsyncIterator[T]):
    """Close the opened connection even if iteration never begins."""

    def __init__(self, source: AsyncIterator[T], close: Callable[[], Awaitable[None]]) -> None:
        self._source, self._close, self._closed = source, close, False

    def __aiter__(self) -> OwnedStream[T]:
        return self

    async def __anext__(self) -> T:
        if self._closed:
            raise StopAsyncIteration
        try:
            return await self._source.__anext__()
        except BaseException:
            await self.aclose()
            raise

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            close = getattr(self._source, "aclose", None)
            if close is not None:
                await close()
            await self._close()


async def close_iterator(source: AsyncIterator[object]) -> None:
    close = getattr(source, "aclose", None)
    if close is not None:
        await close()


class SDKTransport:
    """Caller supplies a no-retry SDK client; no keys or raw SDK values leave this boundary.

    The pinned SDK supports `extra_body` for newer Antigravity configuration.
    Interactions retries are disabled here; the host also configures the
    supplied base client's HttpRetryOptions(attempts=1).
    """

    def __init__(
        self,
        client: genai.Client,
        *,
        protect_storage_credentials: bool = False,
        storage_tokens: set[RedactedCredential] | None = None,
    ) -> None:
        self._protect_storage_credentials = protect_storage_credentials
        self._storage_tokens: set[RedactedCredential] = (
            storage_tokens if storage_tokens is not None else set()
        )
        # google-genai 2.7 forwards attempts as Stainless max_retries, so
        # attempts=1 still retries once. Set the Interactions edge to zero.
        nextgen = client.aio._nextgen_client  # pyright: ignore[reportPrivateUsage]
        self._interactions = nextgen.with_options(max_retries=0).interactions
        self._files = client.aio.files
        self._models = client.aio.models

    def _check_storage_response(self, response: httpx.Response) -> None:
        # Inspect bytes before JSON/SDK parsing can render an unexpected value.
        if any(token.encode() in response.content for token in self._storage_tokens):
            raise ProviderError("permission", retryable=False, native_code="gcs_token_echo")
        checked_storage_response(
            object_value(cast(JsonValue, response.json())),
            protected=True,
            tokens=self._storage_tokens,
        )

    async def count_input_tokens(self, request: Mapping[str, JsonValue]) -> int:
        from google.genai.types import CountTokensConfig, HttpOptions

        bounded = dict(request)
        if not model_request(bounded) or "previous_interaction_id" in bounded:
            raise ProviderError("invalid_request", retryable=False, native_code="unbounded_prompt")
        inputs = bounded.get("input")
        if (
            not isinstance(inputs, list)
            or len(inputs) != 1
            or not isinstance(inputs[0], dict)
            or set(inputs[0]) != {"type", "text"}
            or inputs[0].get("type") != "text"
            or not isinstance(inputs[0].get("text"), str)
        ):
            raise ProviderError("invalid_request", retryable=False, native_code="unbounded_prompt")
        system = bounded.get("system_instruction")
        if system is not None and not isinstance(system, str):
            raise ProviderError("invalid_request", retryable=False, native_code="unbounded_prompt")
        # The Developer API supports full request counting through
        # generateContentRequest; the SDK's system_instruction convenience
        # argument is Vertex-only. Use its supported extra_body escape hatch.
        prompt: Object = {
            "model": "models/" + string(bounded["model"]),
            "contents": [{"role": "user", "parts": [{"text": inputs[0]["text"]}]}],
        }
        if system is not None:
            prompt["systemInstruction"] = {"parts": [{"text": system}]}
        result = await provider_call(
            self._models.count_tokens(
                model=string(bounded["model"]),
                # The wire schema permits omitting contents for a full request.
                contents=None,  # pyright: ignore[reportArgumentType]
                config=CountTokensConfig(
                    http_options=HttpOptions(
                        extra_body={"generateContentRequest": prompt},
                    )
                ),
            )
        )
        if type(result.total_tokens) is not int or result.total_tokens <= 0:
            raise ProviderError("upstream", retryable=False, native_code="unknown_input_tokens")
        # total_tokens includes cached content; never subtract a cache discount
        # from the token envelope. Pricing can reserve every input at the full rate.
        return result.total_tokens

    async def download_snapshot(self, environment_id: str) -> bytes:
        return await provider_call(self._files.download(file=f"files/environment-{environment_id}"))

    async def create(self, request: Mapping[str, JsonValue]) -> Object:
        # Omission enables provider defaults, including billable web tools.
        # Keep older saved requests explicit as well as freshly compiled agents.
        bounded = dict(request)
        bounded.setdefault("tools", [])
        if not isinstance(bounded["tools"], list):
            raise ProviderError(
                "invalid_request", retryable=False, native_code="explicit_tools_required"
            )
        bounded = await refresh_bound_request(bounded)
        token = storage_request_token(bounded)
        if token is not None:
            self._storage_tokens.add(token)
        protected = (
            self._protect_storage_credentials
            or bool(self._storage_tokens)
            or network_transform_present(bounded)
        )
        if model_request(bounded):
            response = await provider_call(
                self._interactions.with_raw_response.create(
                    model=string(bounded["model"]),
                    input=[],
                    background=True,
                    store=True,
                    extra_body=bounded,
                    extra_headers={"Api-Revision": API_REVISION},
                )
            )
        else:
            response = await provider_call(
                self._interactions.with_raw_response.create(
                    agent=string(bounded["agent"]),
                    input=[],
                    background=True,
                    store=True,
                    extra_body=bounded,
                    extra_headers={"Api-Revision": API_REVISION},
                )
            )
        if protected:
            self._check_storage_response(response.http_response)
        parsed = await response.parse()
        return object_value(cast(JsonValue, parsed.model_dump(mode="json", exclude_none=True)))

    async def get(self, interaction_id: str) -> Object:
        response = await provider_call(
            self._interactions.with_raw_response.get(
                id=interaction_id,
                extra_headers={"Api-Revision": API_REVISION},
            )
        )
        if self._protect_storage_credentials or self._storage_tokens:
            self._check_storage_response(response.http_response)
        parsed = await response.parse()
        return object_value(cast(JsonValue, parsed.model_dump(mode="json", exclude_none=True)))

    async def cancel(self, interaction_id: str) -> Object:
        response = await provider_call(
            self._interactions.with_raw_response.cancel(
                id=interaction_id,
                extra_headers={"Api-Revision": API_REVISION},
            )
        )
        if self._protect_storage_credentials or self._storage_tokens:
            self._check_storage_response(response.http_response)
        parsed = await response.parse()
        return object_value(cast(JsonValue, parsed.model_dump(mode="json", exclude_none=True)))

    async def open_stream(
        self, interaction_id: str, *, after: str | None = None
    ) -> AsyncIterator[Object]:
        from google.genai._interactions import omit

        source = await provider_call(
            self._interactions.get(
                id=interaction_id,
                stream=True,
                last_event_id=after if after is not None else omit,
                extra_headers={"Api-Revision": API_REVISION},
            )
        )

        async def records() -> AsyncIterator[Object]:
            try:
                async for event in source:
                    if self._protect_storage_credentials or self._storage_tokens:
                        value = object_value(
                            cast(
                                JsonValue,
                                event.model_dump(mode="json", exclude_none=True, warnings=False),
                            )
                        )
                        yield checked_storage_response(
                            value, protected=True, tokens=self._storage_tokens
                        )
                    else:
                        yield object_value(
                            cast(JsonValue, event.model_dump(mode="json", exclude_none=True))
                        )
            except (APIError, httpx.HTTPError) as error:
                raise normalize_error(error) from None
            finally:
                await source.close()

        return OwnedStream(records(), source.close)
