"""An interaction may echo our MCP server's Authorization header: scrub it, refuse the rest.

Whether Gemini echoes request tool headers is unverified (F2-G r12 evidence
dropped all headers before saving), so the transport assumes it may, exactly
as it does for the storage-mount transform: only our own header, at
`tools[i].headers` of an `mcp_server` tool, is removed; the token anywhere
else, or any other header, fails closed without quoting it.
"""

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import httpx
import pytest
from google import genai
from google.genai.types import HttpOptions, HttpRetryOptions
from mux.drivers.gemini import transport as transport_module
from mux.drivers.gemini.mcp_auth import RedactedCredential
from mux.drivers.gemini.transport import (
    Object,
    SDKTransport,
    checked_storage_response,
    mcp_request_tokens,
    scrub_mcp_echo,
)
from mux.errors import ProviderError

TOKEN = RedactedCredential("offline-mcp-token-never-escapes")
OURS = {"Authorization": "Bearer " + TOKEN}
URL = "https://mcp.invalid/mcp"


def mcp_tool(headers: object = OURS) -> Object:
    tool: Object = {
        "type": "mcp_server",
        "name": "daimon_mcp",
        "url": URL,
        "allowed_tools": ["get_agent"],
    }
    if headers is not None:
        tool["headers"] = headers  # pyright: ignore[reportArgumentType]
    return tool


def completed(*, tools: list[Object] | None = None, text: str = "final answer") -> Object:
    return {
        "id": "native",
        "status": "completed",
        "created": "2026-10-22T15:02:25Z",
        "updated": "2026-10-22T15:08:12Z",
        "tools": tools if tools is not None else [mcp_tool()],
        "steps": [{"type": "model_output", "content": [{"type": "text", "text": text}]}],
        "usage": {"total_input_tokens": 10, "total_output_tokens": 2},
    }


def test_only_dispatched_mcp_bearer_headers_are_remembered() -> None:
    request: Object = {
        "tools": [
            mcp_tool(),
            {"type": "google_search"},
            {
                "type": "mcp_server",
                "name": "x",
                "url": URL,
                "headers": {"Authorization": "Basic x"},
            },
        ]
    }
    assert mcp_request_tokens(request) == {TOKEN}
    assert mcp_request_tokens({"tools": "not-a-list"}) == set()


def test_our_echoed_header_is_scrubbed_and_the_answer_survives() -> None:
    clean = checked_storage_response(completed(), protected=False, mcp_tokens={TOKEN})
    assert "headers" not in clean["tools"][0]  # pyright: ignore[reportIndexIssue,reportArgumentType]
    assert clean["steps"][0]["content"][0]["text"] == "final answer"  # pyright: ignore[reportIndexIssue,reportArgumentType]
    assert TOKEN not in json.dumps(clean)
    nested = scrub_mcp_echo({"event_type": "x", "interaction": completed()}, {TOKEN})
    assert "headers" not in nested["interaction"]["tools"][0]  # pyright: ignore[reportIndexIssue,reportArgumentType]


@pytest.mark.parametrize(
    "tools",
    [
        [mcp_tool({"Authorization": "Bearer someone-else"})],
        [mcp_tool({"Authorization": "Bearer " + TOKEN, "X-Extra": "1"})],
        [{"type": "google_search", "headers": OURS}],
    ],
)
def test_a_header_that_is_not_our_exact_echo_refuses(tools: list[Object]) -> None:
    with pytest.raises(ProviderError) as refused:
        checked_storage_response(completed(tools=tools), protected=False, mcp_tokens={TOKEN})
    assert refused.value.native_code == "mcp_header_echo"
    assert TOKEN not in str(refused.value)


def test_the_token_anywhere_else_refuses_without_quoting_it() -> None:
    with pytest.raises(ProviderError) as refused:
        checked_storage_response(
            completed(text="leaked Bearer " + TOKEN), protected=False, mcp_tokens={TOKEN}
        )
    assert refused.value.native_code == "mcp_token_echo"
    assert TOKEN not in str(refused.value) and TOKEN not in repr(refused.value)


def test_without_mcp_tokens_an_unprotected_response_is_untouched() -> None:
    body = completed(tools=[mcp_tool(None)])
    assert checked_storage_response(body, protected=False) is body


@asynccontextmanager
async def transport(
    respond: Callable[[httpx.Request], httpx.Response],
) -> AsyncIterator[SDKTransport]:
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = genai.Client(
            api_key="offline-fixture",
            http_options=HttpOptions(
                httpx_async_client=http, retry_options=HttpRetryOptions(attempts=1)
            ),
        )
        try:
            yield SDKTransport(client)
        finally:
            await client.aio.aclose()


async def test_a_dispatched_mcp_credential_protects_create_and_later_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def already_bound(request: Object) -> Object:
        return request

    monkeypatch.setattr(transport_module, "refresh_bound_request", already_bound)
    sent: list[bytes] = []

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(request.content)
        return httpx.Response(200, json=completed())

    async with transport(respond) as gemini:
        created = await gemini.create(
            {"agent": "agents/base", "agent_config": {"type": "antigravity"}, "tools": [mcp_tool()]}
        )
        assert "headers" not in created["tools"][0]  # pyright: ignore[reportIndexIssue,reportArgumentType]
        read = await gemini.get("native")
        assert "headers" not in read["tools"][0]  # pyright: ignore[reportIndexIssue,reportArgumentType]
    assert TOKEN.encode() in sent[0], "the credential is sent to the provider"


def test_a_single_element_header_list_is_our_echo_too() -> None:
    body = completed(tools=[mcp_tool([OURS])])
    clean = checked_storage_response(body, protected=False, mcp_tokens={TOKEN})
    assert "headers" not in clean["tools"][0]  # pyright: ignore[reportIndexIssue,reportArgumentType]


@pytest.mark.parametrize(
    ("headers", "reason"),
    [
        ([OURS, OURS], "header_list_not_single"),
        ([], "header_list_not_single"),
        ({"authorization": "Bearer " + TOKEN}, "header_keys"),
        ([{"Authorization": "Bearer " + TOKEN, "X": "1"}], "header_keys"),
        ("Bearer " + TOKEN, "header_shape"),
        ({"Authorization": "Bearer someone-else"}, "header_value_not_dispatched"),
    ],
)
def test_every_other_shape_refuses_with_its_path_and_never_the_value(headers, reason) -> None:
    event = {
        "event_type": "interaction.completed",
        "interaction": completed(tools=[mcp_tool(headers)]),
    }
    with pytest.raises(ProviderError) as refused:
        checked_storage_response(event, protected=False, mcp_tokens={TOKEN})
    error = refused.value
    assert (error.native_code, error.native_message) == ("mcp_header_echo", reason)
    assert error.native_param == "$.interaction.tools[0].headers"
    assert all(TOKEN not in str(part) for part in (error, error.native_param, error.native_message))


def test_a_token_outside_the_header_names_its_path_only() -> None:
    with pytest.raises(ProviderError) as refused:
        checked_storage_response(
            completed(text="leaked Bearer " + TOKEN), protected=False, mcp_tokens={TOKEN}
        )
    assert refused.value.native_code == "mcp_token_echo"
    assert refused.value.native_param == "$.steps[0].content[0].<key>"  # non-schema keys redacted
    assert refused.value.native_message == "token_outside_header"


def test_a_pinned_full_source_echo_scrubs_mount_and_mcp_together() -> None:
    """Operator/pinned (F2-G) requests carry both credentials through the same transport."""
    mount = RedactedCredential("offline-mount-token")
    body = completed(tools=[mcp_tool([OURS])])
    body["environment"] = {
        "type": "remote",
        "network": {
            "allowlist": [
                {
                    "domain": "storage.googleapis.com",
                    "transform": {"Authorization": "Bearer " + mount},
                }
            ]
        },
    }
    clean = checked_storage_response(body, protected=True, tokens={mount}, mcp_tokens={TOKEN})
    encoded = json.dumps(clean)
    assert TOKEN not in encoded and mount not in encoded


@pytest.mark.parametrize("leak", [False, True])
async def test_the_configured_transport_scrubs_every_later_read_after_one_create(
    monkeypatch: pytest.MonkeyPatch, leak: bool
) -> None:
    """Production makes a fresh SDKTransport per call: the dispatched header must still
    be known to a separate GET, cancel and stream, and a leak there must refuse."""
    from mux.drivers.gemini import deployment

    from .test_deployment_isolation import owned_clients

    async def already_bound(request: Object) -> Object:
        return request

    monkeypatch.setattr(transport_module, "refresh_bound_request", already_bound)
    reads: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        echoed = completed(tools=[mcp_tool([OURS])], text="leaked " + TOKEN if leak else "answer")
        if request.method == "POST" and "cancel" in request.url.path:
            reads.append("cancel")
            return httpx.Response(200, json=echoed)
        if request.method == "POST":
            reads.append("create")
            return httpx.Response(200, json=completed(tools=[mcp_tool([OURS])]))
        if "stream" in str(request.url) or "text/event-stream" in request.headers.get("accept", ""):
            reads.append("stream")
            frame = {"event_type": "interaction.completed", "interaction": echoed}
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=("data: " + json.dumps(frame) + "\n\n").encode(),
            )
        reads.append("get")
        return httpx.Response(200, json=echoed)

    owned_clients(monkeypatch, respond)
    gemini = deployment.configured_transport(api_key="offline-selected")
    created = await gemini.create(
        {"agent": "agents/base", "agent_config": {"type": "antigravity"}, "tools": [mcp_tool()]}
    )
    assert "headers" not in created["tools"][0]  # pyright: ignore[reportIndexIssue,reportArgumentType]

    async def stream_first() -> Object:
        source = await gemini.open_stream("native")
        try:
            return await anext(aiter(source))
        finally:
            closer = getattr(source, "aclose", None)
            if callable(closer):
                await closer()  # pyright: ignore[reportUnknownVariableType]

    for name, read in (
        ("get", lambda: gemini.get("native")),
        ("cancel", lambda: gemini.cancel("native")),
        ("stream", stream_first),
    ):
        if leak:
            with pytest.raises(ProviderError) as refused:
                await read()
            assert refused.value.native_code == "mcp_token_echo", name
            assert TOKEN not in str(refused.value)
        else:
            value = await read()
            assert TOKEN not in json.dumps(value), name
    assert reads[0] == "create" and {"get", "cancel", "stream"} <= set(reads)


def test_the_shape_r12_actually_echoed_is_scrubbed() -> None:
    """r12's completed interaction: `$.tools[3].headers.Authorization`, a plain dict entry."""
    tools = [
        {"type": "code_execution"},
        {"type": "url_context"},
        {"type": "google_search"},
        mcp_tool(),
    ]
    clean = checked_storage_response(completed(tools=tools), protected=True, mcp_tokens={TOKEN})
    assert "headers" not in clean["tools"][3]  # pyright: ignore[reportIndexIssue,reportArgumentType]
    assert TOKEN not in json.dumps(clean)
