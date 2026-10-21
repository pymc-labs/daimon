"""C12 checks shared by live probes and offline regressions."""

import os
import re

import httpx
import pytest


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
        raise AssertionError("C12: Responses model is not gpt-6-luna; probe aborted")
