"""Exercise the pinned SDK over HTTPX; every response is synthetic and offline."""

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pytest
from google import genai
from google.genai.types import HttpOptions, HttpRetryOptions
from mux.drivers.gemini.transport import Object, SDKTransport, close_iterator
from mux.errors import ProviderError
from pydantic import JsonValue


@pytest.mark.asyncio
async def test_sdk_submits_documented_antigravity_extra_body_and_reads_steps() -> None:
    captured: list[httpx.Request] = []
    stamp = datetime.now(UTC).isoformat()

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "id": "i1",
                "created": stamp,
                "updated": stamp,
                "status": "completed",
                "steps": [],
                "environment_id": "e1",
                "usage": {"total_output_tokens": 5},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = genai.Client(
            api_key="offline-fixture",
            http_options=HttpOptions(
                httpx_async_client=http, retry_options=HttpRetryOptions(attempts=1)
            ),
        )
        transport = SDKTransport(client)
        response = await transport.create(
            {
                "agent": "antigravity-preview-05-2026",
                "agent_config": {"type": "antigravity", "model": "gemini-3.8-flash"},
                "input": [{"type": "text", "text": "hello"}],
                "environment": "remote",
                "background": True,
                "store": True,
            }
        )
        assert response["id"] == "i1" and response["steps"] == []
        posted = json.loads(captured[0].content)
        assert posted["tools"] == []
        assert posted["agent_config"]["type"] == "antigravity"
        assert posted["input"][0]["text"] == "hello"
        assert captured[0].headers["Api-Revision"] == "2026-05-20"
        assert (await transport.get("i1"))["id"] == "i1"
        await transport.cancel("i1")
        assert captured[1].url.path.endswith("/interactions/i1")
        assert captured[2].url.path.endswith("/interactions/i1/cancel") or captured[
            2
        ].url.path.endswith("/interactions/i1:cancel")
        await client.aio.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "category"),
    [
        (400, "invalid_request"),
        (401, "auth"),
        (403, "permission"),
        (404, "not_found"),
        (409, "conflict"),
        (429, "rate_limited"),
        (503, "overloaded"),
    ],
)
async def test_sdk_errors_are_owned_and_do_not_echo_provider_secrets(
    status: int, category: str
) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            status, json={"error": {"message": "SECRET echoed upstream", "code": status}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = genai.Client(
            api_key="offline-fixture",
            http_options=HttpOptions(
                httpx_async_client=http, retry_options=HttpRetryOptions(attempts=1)
            ),
        )
        with pytest.raises(ProviderError) as exc:
            await SDKTransport(client).create(
                {"agent": "antigravity-preview-05-2026", "input": "hello"}
            )
        assert exc.value.category == category
        assert exc.value.native_code == ("503" if status == 503 else None)
        assert "SECRET" not in str(exc.value)
        assert exc.value.__cause__ is None
        assert len(calls) == 1
        await client.aio.aclose()


@pytest.mark.asyncio
async def test_sdk_open_stream_closes_http_response_without_iteration() -> None:
    class TrackedStream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b'data: {"event_type":"step.delta","index":0,"delta":{"type":"text","text":"hi"}}\n\n'

        async def aclose(self) -> None:
            self.closed = True

    body = TrackedStream()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = genai.Client(
            api_key="offline-fixture",
            http_options=HttpOptions(
                httpx_async_client=http, retry_options=HttpRetryOptions(attempts=1)
            ),
        )
        opened = await SDKTransport(client).open_stream("i1")
        assert not body.closed
        await close_iterator(opened)
        assert body.closed
        await client.aio.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 403, 404, 429, 503])
async def test_sdk_binary_workspace_snapshot_uses_documented_download(status: int) -> None:
    captured: list[httpx.Request] = []
    data = b"\x00\xffraw-snapshot\n"

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return (
            httpx.Response(status, content=data)
            if status == 200
            else httpx.Response(status, json={"error": {"message": "SECRET echo", "code": status}})
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = genai.Client(
            api_key="offline-fixture",
            http_options=HttpOptions(
                httpx_async_client=http, retry_options=HttpRetryOptions(attempts=1)
            ),
        )
        transport = SDKTransport(client)
        if status == 200:
            assert await transport.download_snapshot("e1") == data
        else:
            with pytest.raises(ProviderError) as exc:
                await transport.download_snapshot("e1")
            assert (
                exc.value.category
                == {403: "permission", 404: "not_found", 429: "rate_limited", 503: "overloaded"}[
                    status
                ]
            )
            assert "SECRET" not in str(exc.value) and exc.value.__cause__ is None
        assert len(captured) == 1
        assert captured[0].method == "GET"
        assert captured[0].url.path == "/v1beta/files/environment-e1:download"
        assert captured[0].url.params["alt"] == "media"
        await client.aio.aclose()


@pytest.mark.asyncio
async def test_sdk_interrupted_snapshot_body_is_typed_and_connection_is_closed() -> None:
    class BrokenBody(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b"\x00\xffpartial binary archive"
            raise httpx.ReadError("SECRET echoed in failed body")

        async def aclose(self) -> None:
            self.closed = True

    body = BrokenBody()
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, stream=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = genai.Client(
            api_key="offline-fixture",
            http_options=HttpOptions(
                httpx_async_client=http, retry_options=HttpRetryOptions(attempts=1)
            ),
        )
        with pytest.raises(ProviderError) as exc:
            await SDKTransport(client).download_snapshot("e1")
        assert exc.value.category == "transient_network"
        assert "SECRET" not in str(exc.value) and exc.value.__cause__ is None
        assert len(calls) == 1 and body.closed
        await client.aio.aclose()


@pytest.mark.parametrize("configured", [False, True])
async def test_every_create_wire_body_has_exact_tools_on_continuation_and_fallback(
    configured: bool,
) -> None:
    captured: list[httpx.Request] = []
    tools = [{"type": "code_execution"}] if configured else []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if len(captured) == 2:
            return httpx.Response(503, json={"error": {"code": 503, "message": "overloaded"}})
        return httpx.Response(200, json={"id": "next", "status": "completed", "steps": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = genai.Client(
            api_key="offline-fixture",
            http_options=HttpOptions(
                httpx_async_client=http, retry_options=HttpRetryOptions(attempts=1)
            ),
        )
        source = SDKTransport(client)
        base: Object = {
            "agent": "antigravity-preview-05-2026",
            "agent_config": {"type": "antigravity", "model": "gemini-3.8-flash"},
            "input": [{"type": "text", "text": "hello"}],
            "environment": "remote",
        }
        if configured:
            base["tools"] = [{"type": "code_execution"}]
        await source.create(base)
        continuation: Object = {**base, "previous_interaction_id": "next", "environment": "e1"}
        with pytest.raises(ProviderError, match="overloaded"):
            await source.create(continuation)
        # A caller's explicitly selected 503 fallback traverses the same SDK boundary.
        await source.create(
            {
                **continuation,
                "agent_config": {"type": "antigravity", "model": "gemini-flash-latest"},
            }
        )
        posted = [json.loads(request.content) for request in captured]
        assert len(posted) == 3  # No hidden SDK retry.
        assert [body["tools"] for body in posted] == [tools, tools, tools]
        assert [body.get("previous_interaction_id") for body in posted] == [None, "next", "next"]
        assert posted[-1]["agent_config"]["model"] == "gemini-flash-latest"
        assert ("tools" in base) == configured  # The caller's stored request is not mutated.
        await client.aio.aclose()


@pytest.mark.parametrize("tools", [None, "defaults", {"type": "google_search"}])
async def test_non_list_tools_are_refused_before_native_io(tools: JsonValue) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"id": "unexpected", "status": "completed"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = genai.Client(
            api_key="offline-fixture", http_options=HttpOptions(httpx_async_client=http)
        )
        with pytest.raises(ProviderError) as error:
            await SDKTransport(client).create(
                {"agent": "antigravity-preview-05-2026", "tools": tools}
            )
        assert error.value.native_code == "explicit_tools_required"
        assert captured == []
        await client.aio.aclose()


async def test_sdk_submits_the_cheap_named_worker_with_low_agent_budget() -> None:
    from contextlib import AsyncExitStack

    from mux.profiles.gemini import WORKER_MAX_TOTAL_TOKENS, WORKER_MODEL
    from mux.testing.isolated_sdk import build_gemini_client

    captured: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"id": "worker", "status": "completed", "steps": []})

    async with AsyncExitStack() as stack:
        client = build_gemini_client(transport=httpx.MockTransport(handle), api_key="offline")
        stack.callback(client.close)
        stack.push_async_callback(client.aio.aclose)
        await SDKTransport(client).create(
            {
                "agent": "owned-cheap-agent",
                "agent_config": {
                    "type": "antigravity",
                    "model": WORKER_MODEL,
                    "max_total_tokens": WORKER_MAX_TOTAL_TOKENS,
                },
                "input": [{"type": "text", "text": "one worker input"}],
                "tools": [],
                "background": True,
                "store": True,
            }
        )
    assert len(captured) == 1
    body = json.loads(captured[0].content)
    assert body["agent"] == "owned-cheap-agent"
    assert body["agent_config"] == {
        "type": "antigravity",
        "model": WORKER_MODEL,
        "max_total_tokens": WORKER_MAX_TOTAL_TOKENS,
    }
    assert "model" not in body and "generation_config" not in body and body["tools"] == []


@pytest.mark.parametrize("defect", (None, "missing", "null", "bool", "mixed_agent", "tools"))
async def test_sdk_model_output_cap_is_verified_in_actual_request_bytes(defect: str | None) -> None:
    from contextlib import AsyncExitStack

    from mux.contracts.ids import ModelRef
    from mux.contracts.resources import AgentSpec
    from mux.drivers.gemini.core import compile_agent
    from mux.errors import UnsupportedCapability
    from mux.testing.isolated_sdk import build_gemini_client

    captured: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"id": "capped", "status": "completed", "steps": []})

    body = compile_agent(
        AgentSpec(
            name="chat", model=ModelRef(provider="gemini", id="gemini-3.8-flash"), system="Kept"
        ),
        max_output_tokens=4096,
    )
    body.update(input=[{"type": "text", "text": "question"}], background=True, store=True)
    if defect == "missing":
        body.pop("generation_config")
    elif defect in ("null", "bool"):
        body["generation_config"] = {"max_output_tokens": None if defect == "null" else False}
    elif defect == "mixed_agent":
        body["agent"] = "managed"
    elif defect == "tools":
        body["tools"] = [{"type": "code_execution"}]
    async with AsyncExitStack() as stack:
        client = build_gemini_client(transport=httpx.MockTransport(handle), api_key="offline")
        stack.callback(client.close)
        stack.push_async_callback(client.aio.aclose)
        transport = SDKTransport(client)
        if defect is None:
            await transport.create(body)
        else:
            with pytest.raises(UnsupportedCapability, match="capped_model_request"):
                await transport.create(body)
    if defect is not None:
        assert captured == []
    else:
        assert len(captured) == 1
        assert captured[0].method == "POST" and captured[0].url.path == "/v1beta/interactions"
        assert json.loads(captured[0].content) == body
        assert b'"max_output_tokens"' in captured[0].content


@pytest.mark.parametrize("failure", (400, 503))
async def test_sdk_output_cap_never_downgrades_on_refusal_or_fallback(failure: int) -> None:
    from contextlib import AsyncExitStack

    from mux.drivers.gemini.flash import FLASH_MODELS, FlashTransport
    from mux.testing.drivers.gemini.accounting import Accounting
    from mux.testing.isolated_sdk import build_gemini_client

    captured: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if failure == 400 or len(captured) < 3:
            return httpx.Response(failure, json={"error": {"message": "refused"}})
        return httpx.Response(200, json={"id": "fallback", "status": "completed", "steps": []})

    accounting = Accounting()
    async with AsyncExitStack() as stack:
        client = build_gemini_client(transport=httpx.MockTransport(handle), api_key="offline")
        stack.callback(client.close)
        stack.push_async_callback(client.aio.aclose)
        transport = FlashTransport(SDKTransport(client), accounting)
        body: dict[str, JsonValue] = {
            "model": FLASH_MODELS[0],
            "generation_config": {"max_output_tokens": 4096},
            "tools": [],
            "input": [{"type": "text", "text": "question"}],
        }
        if failure == 400:
            with pytest.raises(ProviderError) as refused:
                await transport.create(body)
            assert refused.value.category == "invalid_request"
            assert len(captured) == len(accounting.held) == 1
        else:
            await transport.create(body)
            assert len(captured) == len(accounting.held) == 3
            assert [json.loads(call.content)["model"] for call in captured] == list(FLASH_MODELS)
    for call in captured:
        sent = json.loads(call.content)
        assert sent["generation_config"] == {"max_output_tokens": 4096}
        assert sent["tools"] == [] and not {"agent", "agent_config", "environment"} & sent.keys()


@pytest.mark.parametrize("count", (262144, None, 0))
async def test_sdk_counts_full_worker_prompt_including_system_and_cache(count):
    from contextlib import AsyncExitStack

    from mux.testing.isolated_sdk import build_gemini_client

    captured = []

    def handler(request):
        captured.append(request)
        assert request.url.path.endswith("/models/gemini-3.8-flash:countTokens")
        return httpx.Response(200, json={"totalTokens": count, "cachedContentTokenCount": 200000})

    async with AsyncExitStack() as stack:
        client = build_gemini_client(transport=httpx.MockTransport(handler), api_key="offline")
        stack.callback(client.close)
        stack.push_async_callback(client.aio.aclose)
        transport = SDKTransport(client)
        request = {
            "model": "gemini-3.8-flash",
            "tools": [],
            "generation_config": {"max_output_tokens": 65536},
            "system_instruction": "Protected source instructions",
            "input": [{"type": "text", "text": "Complete this task"}],
        }
        if count is None or count == 0:
            with pytest.raises(ProviderError, match="unknown_input_tokens"):
                await transport.count_input_tokens(request)
        else:
            assert await transport.count_input_tokens(request) == 262144
    body = json.loads(captured[0].content)
    assert body["generateContentRequest"]["systemInstruction"]["parts"] == [
        {"text": "Protected source instructions"}
    ]
    assert body["generateContentRequest"]["contents"] == [
        {"role": "user", "parts": [{"text": "Complete this task"}]}
    ]


HIGH_DEMAND = {
    "error": {
        "code": "api_error",
        "message": "The model is currently experiencing high demand. Please try again later.",
    }
}


@pytest.mark.parametrize(
    ("status", "body", "category", "native_code"),
    [
        (500, HIGH_DEMAND, "overloaded", "high_demand"),
        (500, {"error": {"code": 500, "message": "internal"}}, "upstream", None),
        (500, {"error": {"code": "api_error", "message": "internal"}}, "upstream", None),
        (503, HIGH_DEMAND, "overloaded", "503"),
        (400, HIGH_DEMAND, "invalid_request", None),
    ],
)
async def test_sdk_interaction_read_classifies_high_demand_without_retrying(
    status: int, body: dict[str, JsonValue], category: str, native_code: str | None
) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = genai.Client(
            api_key="offline-fixture",
            http_options=HttpOptions(
                httpx_async_client=http, retry_options=HttpRetryOptions(attempts=1)
            ),
        )
        with pytest.raises(ProviderError) as exc:
            await SDKTransport(client).get("i1")
        assert (exc.value.category, exc.value.native_code) == (category, native_code)
        assert exc.value.retryable == (status >= 500)
        assert len(calls) == 1 and calls[0].method == "GET"
        await client.aio.aclose()


async def test_sdk_high_demand_create_never_falls_back_to_another_model() -> None:
    from contextlib import AsyncExitStack

    from mux.drivers.gemini.flash import FLASH_MODELS, FlashTransport
    from mux.testing.isolated_sdk import build_gemini_client

    from .test_flash import Accounting

    captured: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(500, json=HIGH_DEMAND)

    async with AsyncExitStack() as stack:
        client = build_gemini_client(transport=httpx.MockTransport(handle), api_key="offline")
        stack.callback(client.close)
        stack.push_async_callback(client.aio.aclose)
        transport = FlashTransport(SDKTransport(client), Accounting())
        with pytest.raises(ProviderError) as exc:
            await transport.create(
                {
                    "model": FLASH_MODELS[0],
                    "generation_config": {"max_output_tokens": 4096},
                    "tools": [],
                    "input": [{"type": "text", "text": "question"}],
                }
            )
    assert (exc.value.category, exc.value.native_code) == ("overloaded", "high_demand")
    assert len(captured) == 1


@pytest.mark.parametrize(
    ("status", "category"),
    [(500, "upstream"), (408, "upstream"), (503, "overloaded"), (400, "invalid_request")],
)
async def test_sdk_errors_record_the_http_status_for_logs(status: int, category: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": "SECRET", "code": status}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = genai.Client(
            api_key="offline-fixture",
            http_options=HttpOptions(
                httpx_async_client=http, retry_options=HttpRetryOptions(attempts=1)
            ),
        )
        with pytest.raises(ProviderError) as exc:
            await SDKTransport(client).get("i1")
        assert exc.value.category == category
        assert exc.value.native_error_code == f"http_{status}"
        assert "SECRET" not in str(exc.value)
        await client.aio.aclose()


def _quota_body(
    limit: int,
    model: str = "gemini-4-argon",
    *,
    line_end: str = ", ",
    also: str | None = None,
    tier: str = "-FreeTier",
) -> dict[str, JsonValue]:
    """Google's 429 shape (smoke 2026-10-23), trimmed to the fields read.

    `line_end` ends the "limit: N" line; `also` names a second model's violation
    (one 429 can name several, and then no single model is named on the card).
    """
    quota = "free_tier_requests"
    models = [model, *([also] if also else [])]
    return {
        "error": {
            "code": 429,
            "status": "RESOURCE_EXHAUSTED",
            "message": (
                "You exceeded your current quota.\n* Quota exceeded for metric: "
                f"generativelanguage.googleapis.com/generate_content_{quota}, "
                f"limit: {limit}{line_end}model: {model}\nPlease retry in 17h26m16s."
            ),
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                    "violations": [
                        {
                            "quotaMetric": f"generativelanguage.googleapis.com/generate_content_{quota}",
                            "quotaId": f"GenerateRequestsPerDayPerProjectPerModel{tier}",
                            "quotaDimensions": {"location": "global", "model": named},
                        }
                        for named in models
                    ],
                },
                {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "62776s"},
            ],
        }
    }


@pytest.mark.parametrize(
    ("body", "native_code", "retryable", "model"),
    [
        (_quota_body(0), "quota_zero", False, "gemini-4-argon"),
        (_quota_body(10), "free_quota_exhausted", True, "gemini-4-argon"),
        (_quota_body(10, tier=""), "quota_exhausted", True, "gemini-4-argon"),
        (_quota_body(0, line_end="\n"), "quota_zero", False, "gemini-4-argon"),
        (_quota_body(0, line_end=".5, "), "free_quota_exhausted", True, "gemini-4-argon"),
        (_quota_body(100), "free_quota_exhausted", True, "gemini-4-argon"),
        (_quota_body(0, also="gemini-3.8-flash"), "quota_zero", False, None),
        ({"error": {"code": 429, "message": "slow down"}}, None, True, None),
    ],
)
async def test_a_429_names_a_zero_quota_apart_from_a_rate_limit(
    body: dict[str, JsonValue], native_code: str | None, retryable: bool, model: str | None
) -> None:
    """A model the plan gives no quota is never retried; a spent quota refills."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(429, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = genai.Client(
            api_key="offline-fixture",
            http_options=HttpOptions(
                httpx_async_client=http, retry_options=HttpRetryOptions(attempts=1)
            ),
        )
        with pytest.raises(ProviderError) as exc:
            await SDKTransport(client).get("i1")
        error = exc.value
        assert (error.category, error.native_code) == ("rate_limited", native_code)
        assert error.retryable is retryable
        assert error.native_param == model
        assert error.native_error_code == "http_429"
        assert len(calls) == 1
        await client.aio.aclose()
