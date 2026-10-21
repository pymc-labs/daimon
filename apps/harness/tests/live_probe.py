"""C12 checks shared by live probes and offline regressions."""

import os
import re

import httpx
import pytest
from daimon_harness.model_adapter import AzureModelAdapter
from daimon_harness.runtime import TurnContext
from openai import AsyncAzureOpenAI, DefaultAsyncHttpxClient


class C12ModelMismatch(RuntimeError):
    kind = "c12_model_mismatch"
    retryable = False


def checked_client(**kwargs) -> AsyncAzureOpenAI:
    # The real adapter already supplies this keyword; replace rather than duplicate it.
    kwargs["max_retries"] = 0
    return AsyncAzureOpenAI(
        **kwargs,
        http_client=DefaultAsyncHttpxClient(event_hooks={"response": [verify_luna_response]}),
    )


async def run_luna_probe(adapter: AzureModelAdapter, context: TurnContext) -> None:
    try:
        await adapter.run(context, [])
    except Exception as exc:
        # httpx hook errors are wrapped by the SDK and again by usage metering.
        cause = exc
        while cause is not None:
            if isinstance(cause, C12ModelMismatch):
                await context.emit(
                    "error",
                    {"kind": cause.kind, "message": str(cause), "retryable": cause.retryable},
                )
                raise cause from None
            cause = cause.__cause__
        raise


def luna_deployment() -> str:
    deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-6-luna")
    if deployment != "gpt-6-luna":
        pytest.skip("C12: live tests only permit the gpt-6-luna deployment")
    return deployment


async def verify_luna_response(response: httpx.Response) -> None:
    """Check the actual Responses model before the SDK can continue its loop.

    Azure aliases need not identify their underlying model. This post-I/O check
    cannot prevent a misconfigured first request, but stops subsequent requests.
    """
    if not response.is_success:
        return  # Let the SDK report API failures normally.
    await response.aread()
    model = response.json().get("model")
    if not isinstance(model, str) or not re.fullmatch(r"gpt-6-luna(?:-\d{4}-\d{2}-\d{2})?", model):
        raise C12ModelMismatch("C12: Responses model is not gpt-6-luna; probe aborted")
