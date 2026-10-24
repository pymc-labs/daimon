"""A completed GCS-mounted interaction echoes our mount transform: scrub it, refuse the rest.

Gemini returns the request's environment, including
`network.allowlist[i].transform` (the storage Authorization header), on a
completed interaction (F2-G r12). Only that exact echo of a transform this
transport sent is removed; the token or any transform anywhere else still
fails closed, and nothing quotes the token.
"""

import json
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from copy import deepcopy

import httpx
import pytest
from google import genai
from google.genai.types import HttpOptions, HttpRetryOptions
from mux.drivers.gemini.mcp_auth import RedactedCredential
from mux.drivers.gemini.transport import Object, SDKTransport
from mux.errors import ProviderError

TOKEN = RedactedCredential("offline-mount-token-never-escapes")
OURS = {"Authorization": "Bearer " + TOKEN}


def completed(*, transform: object = OURS, domain: str = "storage.googleapis.com") -> Object:
    rule: dict[str, object] = {"domain": domain}
    if transform is not None:
        rule["transform"] = transform
    return {
        "id": "native",
        "status": "completed",
        "created": "2026-10-22T13:02:25Z",
        "updated": "2026-10-22T13:08:12Z",
        "environment_id": "workspace",
        "environment": {
            "type": "remote",
            "sources": [{"type": "gcs", "source": "gs://skills/v1/", "target": ".agents/skills/s"}],
            "network": {"allowlist": [rule]},
        },
        "steps": [
            {"type": "function_call", "id": "f1", "name": "view_file", "arguments": {}},
            {
                "type": "function_result",
                "call_id": "f1",
                "name": "view_file",
                "is_error": False,
                "result": "SKILL body",
            },
            {"type": "model_output", "content": [{"type": "text", "text": "final answer"}]},
        ],
        "usage": {"total_input_tokens": 10, "total_output_tokens": 2},
    }


def sse(frame: Object) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=("data: " + json.dumps(frame) + "\n\n").encode(),
    )


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
            yield SDKTransport(client, protect_storage_credentials=True, storage_tokens={TOKEN})
        finally:
            await client.aio.aclose()


async def call(sdk: SDKTransport, operation: str) -> Object:
    if operation == "create":
        return await sdk.create({"agent": "antigravity-preview-05-2026", "tools": [], "input": []})
    if operation == "stream":
        return await anext(await sdk.open_stream("native"))
    return await getattr(sdk, operation)("native")


def responder(body: Object, operation: str) -> Callable[[httpx.Request], httpx.Response]:
    if operation == "stream":
        frame: Object = {"event_type": "interaction.completed", "interaction": body}
        return lambda request: sse(frame)
    return lambda request: httpx.Response(200, json=body)


OPERATIONS = ["get", "cancel", "create", "stream"]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", OPERATIONS)
async def test_our_echoed_mount_transform_is_scrubbed_and_the_answer_survives(
    operation: str, caplog: pytest.LogCaptureFixture, recwarn: pytest.WarningsRecorder
) -> None:
    caplog.set_level(logging.DEBUG)
    async with transport(responder(completed(), operation)) as sdk:
        result = await call(sdk, operation)
    interaction = result["interaction"] if operation == "stream" else result
    assert isinstance(interaction, dict)
    assert interaction["environment"]["network"] == {  # type: ignore[index]
        "allowlist": [{"domain": "storage.googleapis.com"}]
    }
    assert interaction["steps"][-1]["content"][0]["text"] == "final answer"  # type: ignore[index]
    assert TOKEN not in json.dumps(result) and "Authorization" not in json.dumps(result)
    assert TOKEN not in caplog.text
    assert not any(TOKEN in str(warning.message) for warning in recwarn)


def leaked_in_model_output(body: Object) -> None:
    body["steps"][-1]["content"][0]["text"] = "here it is: Bearer " + TOKEN  # type: ignore[index]


def leaked_in_tool_result(body: Object) -> None:
    body["steps"][1]["result"] = "env: " + TOKEN  # type: ignore[index]


def leaked_in_another_field(body: Object) -> None:
    body["environment"]["headers"] = {"Authorization": "Bearer " + TOKEN}  # type: ignore[index]


def leaked_as_an_extra_rule_field(body: Object) -> None:
    body["environment"]["network"]["allowlist"][0]["note"] = TOKEN  # type: ignore[index]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize(
    "leak",
    [
        leaked_in_model_output,
        leaked_in_tool_result,
        leaked_in_another_field,
        leaked_as_an_extra_rule_field,
    ],
)
async def test_the_token_anywhere_else_still_refuses_without_quoting_it(
    operation: str,
    leak: Callable[[Object], None],
    caplog: pytest.LogCaptureFixture,
    recwarn: pytest.WarningsRecorder,
) -> None:
    body = completed()
    leak(body)
    caplog.set_level(logging.DEBUG)
    async with transport(responder(body, operation)) as sdk:
        with pytest.raises(ProviderError, match="gcs_token_echo") as error:
            await call(sdk, operation)
    assert TOKEN not in str(error.value) and TOKEN not in caplog.text
    assert not any(TOKEN in str(warning.message) for warning in recwarn)


def nested_transform(body: Object) -> None:
    body["steps"][0]["network"] = {"transform": {"X": "y"}}  # type: ignore[index]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            completed(transform={"Authorization": "Bearer someone-elses-token"}), id="foreign"
        ),
        pytest.param(completed(transform={**OURS, "X-Extra": "1"}), id="altered"),
        pytest.param(completed(domain="evil.example"), id="other-domain"),
    ],
)
async def test_a_transform_that_is_not_our_exact_echo_refuses(operation: str, body: Object) -> None:
    async with transport(responder(deepcopy(body), operation)) as sdk:
        with pytest.raises(ProviderError, match="gcs_transform_echo"):
            await call(sdk, operation)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", OPERATIONS)
async def test_a_transform_outside_the_mount_rule_location_refuses(operation: str) -> None:
    body = completed()
    nested_transform(body)
    async with transport(responder(body, operation)) as sdk:
        with pytest.raises(ProviderError, match="gcs_transform_echo"):
            await call(sdk, operation)


@pytest.mark.asyncio
async def test_without_a_dispatched_token_any_transform_still_refuses() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=completed()))
    ) as http:
        client = genai.Client(
            api_key="offline-fixture",
            http_options=HttpOptions(
                httpx_async_client=http, retry_options=HttpRetryOptions(attempts=1)
            ),
        )
        sdk = SDKTransport(client, protect_storage_credentials=True)
        with pytest.raises(ProviderError, match="gcs_transform_echo"):
            await sdk.get("native")
        await client.aio.aclose()


@pytest.mark.asyncio
async def test_scrubbed_protected_read_matches_the_unprotected_parse() -> None:
    """The scrubbed path returns exactly what the SDK parse returns, minus the echo."""
    clean = completed(transform=None)
    async with transport(responder(completed(), "get")) as sdk:
        scrubbed = await sdk.get("native")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=clean))
    ) as http:
        client = genai.Client(
            api_key="offline-fixture",
            http_options=HttpOptions(
                httpx_async_client=http, retry_options=HttpRetryOptions(attempts=1)
            ),
        )
        plain = await SDKTransport(client).get("native")
        await client.aio.aclose()
    assert scrubbed == plain


# The REAL echo (F2-G r13, read structure-only from the live interaction):
# Gemini returns the transform as a LIST of header objects, on GET and on the
# stream's interaction.completed event.
REAL_LIST = [{"Authorization": "Bearer " + TOKEN}]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", OPERATIONS)
async def test_the_real_list_shaped_echo_is_scrubbed_and_the_answer_survives(
    operation: str, caplog: pytest.LogCaptureFixture, recwarn: pytest.WarningsRecorder
) -> None:
    caplog.set_level(logging.DEBUG)
    async with transport(responder(completed(transform=REAL_LIST), operation)) as sdk:
        result = await call(sdk, operation)
    interaction = result["interaction"] if operation == "stream" else result
    assert isinstance(interaction, dict)
    assert interaction["environment"]["network"] == {  # type: ignore[index]
        "allowlist": [{"domain": "storage.googleapis.com"}]
    }
    assert interaction["steps"][-1]["content"][0]["text"] == "final answer"  # type: ignore[index]
    assert TOKEN not in json.dumps(result) and "Authorization" not in json.dumps(result)
    assert TOKEN not in caplog.text
    assert not any(TOKEN in str(warning.message) for warning in recwarn)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize(
    ("transform", "reason"),
    [
        pytest.param([OURS, OURS], "unrecognized_shape", id="list-of-two"),
        pytest.param([], "unrecognized_shape", id="empty-list"),
        pytest.param([{**OURS, "X-Extra": "1"}], "unrecognized_shape", id="list-extra-key"),
        pytest.param([{"authorization": "Bearer " + TOKEN}], "unrecognized_shape", id="key-case"),
        pytest.param(
            [{"Authorization": "bearer " + TOKEN}], "not_dispatched_by_this_transport", id="scheme"
        ),
        pytest.param(["Bearer " + TOKEN], "unrecognized_shape", id="list-of-string"),
        pytest.param([[OURS]], "unrecognized_shape", id="nested-list"),
        pytest.param(
            [{"Authorization": "Bearer someone-else"}],
            "not_dispatched_by_this_transport",
            id="foreign-token",
        ),
    ],
)
async def test_any_other_list_shape_refuses_with_a_path_and_reason_but_no_value(
    operation: str,
    transform: object,
    reason: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    async with transport(responder(completed(transform=transform), operation)) as sdk:
        with pytest.raises(ProviderError, match="gcs_transform_echo") as error:
            await call(sdk, operation)
    prefix = "$.interaction" if operation == "stream" else "$"
    assert f"path={prefix}.environment.network.allowlist[0].transform" in caplog.text
    assert f"reason={reason}" in caplog.text
    assert TOKEN not in caplog.text and TOKEN not in str(error.value)
    assert "someone-else" not in caplog.text


@pytest.mark.asyncio
async def test_a_transform_outside_the_mount_location_logs_its_path() -> None:
    import logging as _logging

    records: list[_logging.LogRecord] = []

    class Collect(_logging.Handler):
        def emit(self, record: _logging.LogRecord) -> None:
            records.append(record)

    handler = Collect()
    _logging.getLogger("mux.drivers.gemini.transport").addHandler(handler)
    try:
        body = completed(transform=REAL_LIST)
        body["steps"][0]["network"] = {"transform": {"X": "y"}}  # type: ignore[index]
        async with transport(responder(body, "get")) as sdk:
            with pytest.raises(ProviderError, match="gcs_transform_echo"):
                await sdk.get("native")
    finally:
        _logging.getLogger("mux.drivers.gemini.transport").removeHandler(handler)
    (message,) = [r.getMessage() for r in records]
    assert "path=$.steps[0].network.transform" in message
    assert "reason=outside_mount_location" in message


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", OPERATIONS)
async def test_a_token_used_as_a_key_elsewhere_refuses_and_is_never_logged(
    operation: str, caplog: pytest.LogCaptureFixture
) -> None:
    # Review probe: the real echo plus model output whose network object is
    # keyed by the token. The token check runs before any path is logged.
    body = completed(transform=REAL_LIST)
    body["steps"][0]["arguments"] = {"network": {"Bearer " + TOKEN: {"transform": 1}}}  # type: ignore[index]
    caplog.set_level(logging.DEBUG)
    async with transport(responder(body, operation)) as sdk:
        with pytest.raises(ProviderError, match="gcs_token_echo") as error:
            await call(sdk, operation)
    assert TOKEN not in caplog.text and TOKEN not in str(error.value)


@pytest.mark.asyncio
async def test_diagnostic_paths_name_only_schema_keys() -> None:
    import logging as _logging

    records: list[_logging.LogRecord] = []

    class Collect(_logging.Handler):
        def emit(self, record: _logging.LogRecord) -> None:
            records.append(record)

    secret_key = "unknown-secret-looking-key"
    handler = Collect()
    _logging.getLogger("mux.drivers.gemini.transport").addHandler(handler)
    try:
        body = completed(transform=REAL_LIST)
        body["steps"][0]["arguments"] = {  # type: ignore[index]
            secret_key: {"network": {"transform": {"X": "y"}}}
        }
        async with transport(responder(body, "get")) as sdk:
            with pytest.raises(ProviderError, match="gcs_transform_echo"):
                await sdk.get("native")
    finally:
        _logging.getLogger("mux.drivers.gemini.transport").removeHandler(handler)
    (message,) = [r.getMessage() for r in records]
    assert "path=$.steps[0].arguments.<key>.network.transform" in message
    assert secret_key not in message
