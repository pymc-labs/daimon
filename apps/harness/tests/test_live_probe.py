"""Luna-only live checks run in CI without credentials or network access."""

import httpx
import pytest

from . import test_model_adapter as live_tests
from .live_probe import luna_deployment, verify_luna_response


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
        with pytest.raises(AssertionError, match="C12: Responses model"):
            for _ in range(2):
                await client.post(
                    "https://unused.invalid/responses", json={"model": luna_deployment()}
                )
    assert len(calls) == 1


@pytest.mark.parametrize("echo", ["gpt-6-luna", "gpt-6-luna-2026-09-22"])
async def test_luna_response_models_are_accepted(echo):
    response = httpx.Response(200, json={"model": echo})
    await verify_luna_response(response)
