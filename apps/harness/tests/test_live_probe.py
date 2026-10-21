"""Luna-only live checks run in CI without credentials or network access."""

import httpx
import pytest

from . import test_model_adapter as live_tests
from .live_probe import C12ModelMismatch, luna_deployment, verify_luna_response


@pytest.mark.parametrize(
    "deployment", ["gpt-6.1-sol", "GPT-6-LUNA", "gpt-6-luna ", "", " gpt-6-luna"]
)
async def test_non_luna_deployment_skips_before_http(monkeypatch, tmp_path, deployment):
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", deployment)
    calls = []

    def forbidden(request):
        calls.append(request)
        pytest.fail("Non-Luna probe reached HTTP")

    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        with pytest.raises(pytest.skip.Exception, match="C12"):
            model = luna_deployment()
            await client.post("https://unused.invalid/responses", json={"model": model})
    assert calls == []

    # Calling the live entry point directly also detects a reordered guard.
    def forbidden_adapter():
        pytest.fail("Non-Luna probe constructed the model adapter")

    monkeypatch.setattr(live_tests, "AzureModelAdapter", forbidden_adapter)
    with pytest.raises(pytest.skip.Exception, match="C12"):
        await live_tests.test_live_azure_responses(tmp_path, monkeypatch)


def test_missing_deployment_defaults_to_luna(monkeypatch):
    monkeypatch.delenv("AZURE_OPENAI_DEPLOYMENT", raising=False)
    assert luna_deployment() == "gpt-6-luna"


@pytest.mark.parametrize("echo", ["gpt-6.1-sol", "gpt-6-luna-impostor", "GPT-6-LUNA", "", None])
async def test_misleading_luna_alias_aborts_before_second_response(monkeypatch, echo):
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt-6-luna")
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"model": echo})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond),
        event_hooks={"response": [verify_luna_response]},
    ) as client:
        with pytest.raises(C12ModelMismatch, match="C12: Responses model"):
            for _ in range(2):
                await client.post(
                    "https://unused.invalid/responses", json={"model": luna_deployment()}
                )
    assert len(calls) == 1


@pytest.mark.parametrize("echo", ["gpt-6-luna", "gpt-6-luna-2026-09-22"])
async def test_luna_response_models_are_accepted(echo):
    response = httpx.Response(200, json={"model": echo})
    await verify_luna_response(response)


@pytest.mark.parametrize("echo", ["gpt-6.1-sol", "gpt-6-luna", "gpt-6-luna-2026-09-22"])
async def test_checked_client_through_real_adapter_on_loopback(monkeypatch, tmp_path, echo):
    import asyncio
    import json

    from daimon_harness import model_adapter
    from daimon_harness.model_adapter import AzureModelAdapter
    from daimon_harness.runtime import TurnContext
    from mux.drivers.selfhosted.wire import TurnRequest

    from .conftest import turn
    from .live_probe import checked_client, run_luna_probe

    calls = []
    handler_errors = []

    async def respond(reader, writer):
        try:
            async with asyncio.timeout(10):
                header = (await reader.readuntil(b"\r\n\r\n")).decode()
                length = next(
                    int(line.split(":", 1)[1])
                    for line in header.split("\r\n")
                    if line.lower().startswith("content-length:")
                )
                calls.append(json.loads(await reader.readexactly(length)))
                payload = json.dumps(
                    {
                        "id": "resp-probe",
                        "object": "response",
                        "created_at": 1,
                        "model": echo,
                        "status": "completed",
                        "output": [
                            {
                                "type": "message",
                                "id": "msg-probe",
                                "role": "assistant",
                                "status": "completed",
                                "content": [
                                    {"type": "output_text", "text": "ready", "annotations": []}
                                ],
                            }
                        ],
                        "usage": {
                            "input_tokens": 12,
                            "input_tokens_details": {"cached_tokens": 3},
                            "output_tokens": 8,
                            "output_tokens_details": {"reasoning_tokens": 2},
                            "total_tokens": 20,
                        },
                        "parallel_tool_calls": False,
                        "tool_choice": "auto",
                        "tools": [],
                    }
                ).encode()
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    + f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode()
                    + payload
                )
                await writer.drain()
        except Exception as exc:
            handler_errors.append(type(exc).__name__)
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(respond, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", f"http://127.0.0.1:{port}")
        monkeypatch.setenv("AZURE_OPENAI_API_KEY", "local-test-placeholder")
        monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt-6-luna")
        monkeypatch.setattr(model_adapter, "AsyncAzureOpenAI", checked_client)
        events = []

        async def emit(kind, data):
            events.append((kind, data))

        context = TurnContext(TurnRequest.model_validate(turn()), tmp_path, emit)
        async with asyncio.timeout(15):
            if echo == "gpt-6.1-sol":
                with pytest.raises(C12ModelMismatch, match="C12: Responses model") as error:
                    await run_luna_probe(AzureModelAdapter(), context)
                assert error.value.kind == "c12_model_mismatch"
                assert error.value.retryable is False
                assert error.value.__suppress_context__
                c12_errors = [
                    data
                    for kind, data in events
                    if kind == "error" and data["kind"] == "c12_model_mismatch"
                ]
                assert len(c12_errors) == 1
                assert c12_errors[0]["retryable"] is False
                assert not any(kind == "agent_message" for kind, _ in events)
            else:
                await run_luna_probe(AzureModelAdapter(), context)
                assert sum(kind == "usage" for kind, _ in events) == 1
                assert sum(kind == "agent_message" for kind, _ in events) == 1
                assert not any(kind == "error" for kind, _ in events)
    assert handler_errors == []
    assert len(calls) == 1  # Neither the SDK nor the adapter retried.
    assert calls[0]["model"] == "gpt-6-luna"
