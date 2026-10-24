"""Explicit MockTransport SDK conformance edge; impossible to select a live key."""

import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import cast

import httpx
from google import genai
from google.genai.types import HttpOptions, HttpRetryOptions
from pydantic import JsonValue

from mux.drivers.gemini.flash import FLASH_PRIMARY
from mux.drivers.gemini.transport import SDKTransport, object_value
from mux.state.memory import CrashPoint
from mux.state.store import StateStore
from mux.testing.conformance.gemini import PENDING_REASONS, GeminiScript
from mux.testing.conformance.runner import Adapter, PendingKind, PendingReason


class GeminiSDKScript(GeminiScript):
    """Native responses/faults cross the real SDK before driver projection.

    Mutation counters and retained HTTP request bytes are independent of driver
    inputs/receipts. The same scenario is reconstructed against committed store
    records on restart; no verdict or projected event is injected.
    """

    def __init__(self) -> None:
        super().__init__(model=FLASH_PRIMARY)
        self.http_requests: list[httpx.Request] = []
        self.sdk: SDKTransport | None = None

    def attach(self, sdk: SDKTransport) -> None:
        self.sdk = sdk
        self.ma.__init__(
            sdk, storage=self.storage, state_store=self.store, account_scope_id="project"
        )

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.http_requests.append(request)
        if request.method == "POST" and request.url.path.endswith("/interactions"):
            raw = await self.create(object_value(cast(JsonValue, json.loads(request.content))))
        elif request.method == "POST" and request.url.path.endswith(":cancel"):
            raw = await self.cancel(request.url.path.rsplit("/", 1)[-1].removesuffix(":cancel"))
        elif request.method == "POST" and request.url.path.endswith("/cancel"):
            raw = await self.cancel(request.url.path.split("/")[-2])
        elif request.method == "GET" and request.url.path.endswith("/interactions/root"):
            if request.url.params.get("stream") == "true":
                return httpx.Response(
                    200, headers={"content-type": "text/event-stream"}, content=b""
                )
            raw = await self.get("root")
        else:
            raise AssertionError("unscripted SDK conformance request")
        return httpx.Response(200, json=raw)

    def restart_store(
        self, store: StateStore, *, crash: Mapping[str, CrashPoint] | None = None
    ) -> StateStore:
        restarted = super().restart_store(store, crash=crash)
        if self.sdk is None:
            raise ValueError("SDK conformance transport must remain explicitly attached")
        self.attach(self.sdk)
        return restarted


@asynccontextmanager
async def offline_sdk_adapter() -> AsyncIterator[Adapter]:
    """Fresh SDK/store/native script per case; fixed mock edge and no key discovery."""
    script = GeminiSDKScript()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(script.handle), trust_env=False, follow_redirects=False
    ) as http:
        client = genai.Client(
            api_key="offline-fixture",
            http_options=HttpOptions(
                httpx_async_client=http, retry_options=HttpRetryOptions(attempts=1)
            ),
        )
        try:
            script.attach(SDKTransport(client))
            pending = {
                f"C{i:02}": PendingReason(
                    PendingKind.ADAPTER_DEPENDENCY,
                    "This SDK component supplies only C06/C07 native envelopes.",
                )
                for i in range(1, 19)
                if i not in (6, 7)
            }
            pending.update(PENDING_REASONS)
            yield Adapter(script.ma, script.store, script, pending=pending)
        finally:
            await client.aio.aclose()
