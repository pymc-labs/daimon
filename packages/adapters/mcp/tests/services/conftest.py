"""Transport-level Gemini stub for the media service tests.

Mirrors ``build_stub_anthropic`` (``packages/testing/daimon/testing/ma.py``):
attach a real ``genai.Client`` to an ``httpx.MockTransport`` so the real SDK
request-building and response-parsing code runs in every test. Handlers
return camelCase Gemini REST JSON (``usageMetadata``, ``candidates``, ...)
via ``httpx.Response`` — never a hand-rolled client double.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
from google import genai
from google.genai import types


def make_stub_gemini(
    handler: Callable[[httpx.Request], httpx.Response],
) -> genai.Client:
    """Return a real ``genai.Client`` whose async HTTP transport is a MockTransport.

    Verified injection point (google-genai 2.7.0, ``_api_client.py``): pass an
    ``httpx.AsyncClient`` wired to a ``MockTransport`` via
    ``http_options.httpx_async_client``.
    """
    transport = httpx.MockTransport(handler)
    httpx_async_client = httpx.AsyncClient(transport=transport)
    return genai.Client(
        api_key="test-key",
        http_options=types.HttpOptions(httpx_async_client=httpx_async_client),
    )
