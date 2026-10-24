"""Private Responses boundary: journal before I/O, measure at the provider edge.

Codex's turn-level token notifications are deliberately not used for billing.
Only harness dynamic function calls may cross this boundary back to Codex.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import secrets
import socket
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from typing import Any, Literal, cast
from uuid import uuid4

import httpx
from aiohttp import web
from pydantic import JsonValue

from .errors import HarnessError
from .runtime import TurnContext


class ResponsesRelay:
    def __init__(
        self,
        context: TurnContext,
        history: list[JsonValue],
        tools: list[dict[str, JsonValue]],
        client: httpx.AsyncClient,
        url: str,
        headers: Callable[[], Awaitable[dict[str, str]]],
    ) -> None:
        self.context = context
        self.history = history
        self.result = history
        self.tools = tools
        self.names: set[str] = {str(tool["name"]) for tool in tools}
        self.client = client
        self.url = url
        self.headers = headers
        self.capability = secrets.token_urlsafe(32)
        self.failure: HarnessError | None = None
        self.lock = asyncio.Lock()
        self.stopping = False
        self.handlers: set[asyncio.Task[object]] = set()
        self.model_requests = 0
        self.compaction_requests = 0
        self.controller_user_id: str | None = None
        self.private_paths: tuple[str, ...] = (
            (str(context.state_dir),) if context.state_dir is not None else ()
        )
        self.outcomes: dict[str, asyncio.Task[None]] = {}

    @asynccontextmanager
    async def serve(self) -> AsyncIterator[str]:
        app = web.Application(client_max_size=16 * 1024 * 1024)
        app.router.add_post("/responses", self.respond)
        runner = web.AppRunner(app, access_log=None, shutdown_timeout=2)
        await runner.setup()
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        site = web.SockSite(runner, listener)
        await site.start()
        try:
            yield f"http://127.0.0.1:{port}"
        finally:
            self.stopping = True

            async def teardown() -> None:
                try:
                    handlers = list(self.handlers)
                    for task in handlers:
                        task.cancel()
                    await asyncio.gather(*handlers, return_exceptions=True)
                finally:
                    try:
                        await runner.cleanup()
                    finally:
                        listener.close()

            finalization = asyncio.create_task(teardown())
            interrupted = False
            while not finalization.done():
                try:
                    await asyncio.shield(finalization)
                except asyncio.CancelledError:
                    interrupted = True
            await finalization
            if interrupted:
                raise asyncio.CancelledError

    def validate_item(self, item: dict[str, JsonValue]) -> None:
        kind = item.get("type")
        if kind not in {"message", "reasoning", "function_call"}:
            raise HarnessError("codex_native_tool", "Native Codex tools are disabled")
        if kind == "function_call" and (
            item.get("name") not in self.names or item.get("namespace") not in (None, "functions")
        ):
            raise HarnessError("codex_native_tool", "Unregistered Codex function refused")

    async def respond(self, request: web.Request) -> web.StreamResponse:
        if not hmac.compare_digest(
            request.headers.get("Authorization", ""), "Bearer " + self.capability
        ):
            raise web.HTTPUnauthorized()
        if self.stopping or self.failure is not None:
            raise web.HTTPServiceUnavailable()
        task = cast(asyncio.Task[object], asyncio.current_task())
        self.handlers.add(task)
        try:
            async with self.lock:
                if self.stopping or self.failure is not None:
                    raise web.HTTPServiceUnavailable()
                return await self.forward(request)
        finally:
            self.handlers.discard(task)

    async def observation(
        self, kind: Literal["usage", "error"], data: dict[str, JsonValue]
    ) -> bool:
        """Finish one journal write before cancellation can choose another outcome."""

        async def commit() -> None:
            await self.context.emit(kind, data)

        request_id = str(data["request_id"])
        task = self.outcomes.get(request_id)
        if task is None:
            task = asyncio.create_task(commit())
            self.outcomes[request_id] = task
        interrupted = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                interrupted = True
        await task
        return interrupted

    def clean_paths(self, value: JsonValue) -> JsonValue:
        if isinstance(value, str):
            for path in sorted(self.private_paths, key=len, reverse=True):
                value = value.replace(path, "[private controller]")
            return value
        if isinstance(value, list):
            return [self.clean_paths(item) for item in value]
        if isinstance(value, dict):
            return {key: self.clean_paths(item) for key, item in value.items()}
        return value

    def clean_input(self, items: list[JsonValue]) -> list[JsonValue]:
        result: list[JsonValue] = []
        after_user = False
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("role") in {"system", "developer"}:
                continue
            if item.get("role") == "user":
                if (
                    self.controller_user_id is not None
                    and item.get("id") == self.controller_user_id
                ):
                    after_user = True
                    continue
                # In the pinned single-turn protocol, preamble injections precede
                # our original user item. Compaction's prompt and replacement
                # summary follow it. Never infer provenance from text prefixes.
                if not after_user:
                    continue
            result.append(self.clean_paths(item))
        return result

    async def forward(self, request: web.Request) -> web.StreamResponse:
        source = cast(dict[str, JsonValue], await request.json())
        incoming = cast(list[JsonValue], source.get("input", []))
        users = [item for item in incoming if isinstance(item, dict) and item.get("role") == "user"]
        identified_users = [item for item in users if isinstance(item.get("id"), str)]
        if self.controller_user_id is None and identified_users:
            # First request: the pinned controller appends the C3 copy after its
            # preamble (and may already append an ID-less compaction prompt).
            # The C3 copy's stable ID survives local context replacement.
            self.controller_user_id = str(identified_users[-1]["id"])
        if self.controller_user_id is not None and not any(
            item.get("id") == self.controller_user_id for item in users
        ):
            self.failure = HarnessError("codex_protocol", "Codex lost the original user item")
            raise web.HTTPBadGateway(text="Codex lost the original user item")
        # Local compaction appends an ID-less user prompt; replacement summaries
        # are ordinary ID-bearing user items after our stable anchor instead.
        compacting = bool(
            self.controller_user_id is not None
            and incoming
            and isinstance(incoming[-1], dict)
            and incoming[-1].get("role") == "user"
            and "id" not in incoming[-1]
        )
        limit = min(self.context.request.limits.max_tool_calls + 2, 128)
        count = self.compaction_requests if compacting else self.model_requests
        if count >= limit:
            self.failure = HarnessError("model_request_limit", "Maximum model requests exceeded")
            raise web.HTTPServiceUnavailable(text="Maximum model requests exceeded")
        if compacting:
            self.compaction_requests += 1
        else:
            self.model_requests += 1
        parts: list[JsonValue] = []
        for part in self.context.request.input:
            if part.type == "text":
                parts.append({"type": "input_text", "text": part.text})
            else:
                parts.append(
                    {
                        "type": "input_image",
                        "image_url": f"data:{part.media_type};base64,{part.data_b64}",
                        "detail": "auto",
                    }
                )
        # Harness history and C3 input must survive verbatim, regardless of text.
        items = self.history + [{"role": "user", "content": parts}] + self.clean_input(incoming)
        # Only these Responses fields cross the provider boundary. Never forward
        # Codex metadata, cache identifiers, storage, background or tier options.
        body: dict[str, JsonValue] = {
            "model": self.context.request.model,
            "input": cast(
                list[JsonValue],
                [
                    {
                        "role": "developer",
                        "content": self.context.request.instructions
                        + "\n\n"
                        + await self.context.skills(),
                    }
                ]
                + items,
            ),
            "tools": cast(list[JsonValue], self.tools),
            "store": False,
            "stream": True,
            "parallel_tool_calls": False,
            "include": ["reasoning.encrypted_content"],
        }
        reasoning = source.get("reasoning")
        if isinstance(reasoning, dict):
            body["reasoning"] = {
                key: value for key, value in reasoning.items() if key in {"effort", "summary"}
            }
        retained = items
        self.context.model_input = retained
        request_id = str(uuid4())
        reported = False
        await self.context.emit(
            "model_request_started", {"request_id": request_id, "model": self.context.request.model}
        )
        downstream = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        try:
            # Authentication acquisition is after the durable inventory, too.
            async with self.client.stream(
                "POST", self.url, json=body, headers=await self.headers()
            ) as upstream:
                upstream.raise_for_status()
                await downstream.prepare(request)
                tool_events: list[bytes] = []
                tool_indices: set[int] = set()
                lines: list[str] = []
                finished = False
                invalid_tool: HarnessError | None = None
                async for line in upstream.aiter_lines():
                    if line:
                        if line.startswith("data:"):
                            lines.append(line[5:].lstrip())
                        continue
                    if not lines:
                        continue
                    payload = "\n".join(lines)
                    lines.clear()
                    if payload == "[DONE]":
                        continue
                    # Provider summaries can echo private controller paths. Scrub
                    # before both Codex sees them and durable history retains them.
                    event = cast(dict[str, Any], self.clean_paths(json.loads(payload)))
                    kind = event.get("type", "")
                    encoded = ("data: " + json.dumps(event) + "\n\n").encode()
                    item = event.get("item")
                    if item is not None:
                        try:
                            self.validate_item(item)
                        except HarnessError as exc:
                            invalid_tool = exc
                        if item.get("type") not in {"message", "reasoning"}:
                            tool_indices.add(event["output_index"])
                    # Delay function events until successful provider completion.
                    # An incomplete response must never execute even a partial call.
                    if event.get("output_index") in tool_indices:
                        tool_events.append(encoded)
                        continue
                    if kind in {"response.completed", "response.incomplete", "response.failed"}:
                        response = event["response"]
                        usage = response.get("usage")
                        if usage is not None:
                            data = usage_data(usage)
                            interrupted = await self.observation(
                                "usage",
                                {
                                    "request_id": request_id,
                                    "model": self.context.request.model,
                                    **data,
                                },
                            )
                            reported = True
                            if interrupted:
                                raise asyncio.CancelledError
                        if not reported:
                            raise HarnessError(
                                "usage_missing", "Model response did not report usage"
                            )
                        status = response.get("status")
                        if status != "completed":
                            raise HarnessError(
                                "model_incomplete" if status == "incomplete" else "model_failed",
                                "Azure response did not complete",
                            )
                        if invalid_tool is not None:
                            raise invalid_tool
                        for output in response.get("output", []):
                            self.validate_item(output)
                        self.result = retained + response.get("output", [])
                        for buffered in tool_events:
                            await downstream.write(buffered)
                        await downstream.write(encoded)
                        finished = True
                        break
                    if kind == "error":
                        raise HarnessError("model_failed", "Azure Responses stream failed")
                    await downstream.write(encoded)
                if not finished:
                    raise HarnessError("usage_unavailable", "Azure Responses stream ended early")
                await downstream.write_eof()
        except (Exception, asyncio.CancelledError) as exc:
            if not reported:
                await self.observation(
                    "error",
                    {
                        "kind": "usage_unavailable",
                        "request_id": request_id,
                        "message": "Model request ended without usage",
                        "retryable": False,
                    },
                )
            self.failure = (
                exc
                if isinstance(exc, HarnessError)
                else HarnessError("model_failed", "Codex Responses relay failed")
            )
            if isinstance(exc, asyncio.CancelledError):
                raise
            # Do not leak provider exceptions, URLs, headers or bodies to Codex.
            if downstream.prepared:
                with suppress(ConnectionError):
                    await downstream.write(
                        b'data: {"type":"error","message":"Harness relay failed"}\n\n'
                    )
                    await downstream.write_eof()
            else:
                raise web.HTTPBadGateway(text="Harness relay failed") from None
        return downstream


def usage_data(usage: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Require actual counts; optional detail fields have the Responses default 0."""
    values = {
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
    }
    for source, destination, field in (
        ("input_tokens_details", "cached_input_tokens", "cached_tokens"),
        ("output_tokens_details", "reasoning_tokens", "reasoning_tokens"),
    ):
        details = usage.get(source) or {}
        if not isinstance(details, dict):
            raise HarnessError("usage_missing", "Invalid provider usage details")
        values[destination] = details.get(field, 0)
    if any(type(value) is not int or value < 0 for value in values.values()):
        raise HarnessError("usage_missing", "Invalid provider usage counts")
    return values
