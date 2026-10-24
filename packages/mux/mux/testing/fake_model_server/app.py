"""The ASGI app: model API routes plus a small JSON control surface."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, MutableMapping, Sequence
from typing import Any
from urllib.parse import parse_qsl

from pydantic import TypeAdapter

from .render import BODY_RENDERERS, Api, Ids, encode_frame, error_body, stream_frames
from .script import (
    CONVERSATION_HEADER,
    DEFAULT_CONVERSATION,
    Fail,
    RecordedRequest,
    Reply,
    ScriptBook,
    Step,
)

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]

CONTROL_PREFIX = "/_fake/"
_STEPS = TypeAdapter(list[Step])
_REDACTED_HEADERS = {"authorization", "x-api-key", "api-key"}


def route_api(path: str) -> Api | None:
    """Recognise OpenAI, Azure OpenAI (``/openai/...``) and Anthropic URL shapes by suffix."""
    path = path.rstrip("/")
    if path.endswith("/responses"):
        return "responses"
    if path.endswith("/chat/completions"):
        return "chat_completions"
    if path.endswith("/messages"):
        return "anthropic_messages"
    return None


class FakeModelServer:
    """Serve scripted model responses; record every request received.

    A request picks its script from the ``x-fake-conversation`` header, else from the body's
    ``model`` field when a script exists under that name, else from the default conversation.
    An exhausted script answers HTTP 500 and records ``answered_with="exhausted"``.
    """

    def __init__(self) -> None:
        self.book = ScriptBook()
        self._ids = Ids()

    def script(self, steps: Sequence[Step], *, conversation: str = DEFAULT_CONVERSATION) -> None:
        self.book.script(steps, conversation=conversation)

    @property
    def requests(self) -> list[RecordedRequest]:
        return self.book.requests

    def requests_for(self, conversation: str | None = None) -> list[RecordedRequest]:
        return self.book.requests_for(conversation)

    def reset(self) -> None:
        self.book.reset()

    async def wait_for_requests(
        self, count: int, *, conversation: str | None = None, timeout_s: float = 10
    ) -> list[RecordedRequest]:
        return await self.book.wait_for_requests(
            count, conversation=conversation, timeout_s=timeout_s
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self._lifespan(receive, send)
        elif scope["type"] == "http":
            await self._http(scope, receive, send)

    async def _lifespan(self, receive: Receive, send: Send) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return

    async def _http(self, scope: Scope, receive: Receive, send: Send) -> None:
        body = await _read_body(receive)
        path = scope["path"]
        if path.startswith(CONTROL_PREFIX):
            await self._control(scope, body, send)
            return
        api = route_api(path)
        if api is None:
            await _json(send, 404, {"error": {"message": f"no fake route for {path}"}})
        elif scope["method"] != "POST":
            await _json(send, 405, {"error": {"message": "POST only"}})
        else:
            await self._model_call(api, scope, body, receive, send)

    async def _model_call(
        self, api: Api, scope: Scope, raw: bytes, receive: Receive, send: Send
    ) -> None:
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            await _json(send, 400, error_body(api, Fail(status=400, message="invalid JSON body")))
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        conversation = self._conversation(headers, body)
        step = self.book.next_step(conversation)
        stream = bool(body.get("stream"))
        self.book.requests.append(
            RecordedRequest(
                api=api,
                conversation=conversation,
                method=scope["method"],
                path=scope["path"],
                query=dict(parse_qsl(scope["query_string"].decode())),
                headers={
                    k: "<redacted>" if k in _REDACTED_HEADERS else v for k, v in headers.items()
                },
                body=body,
                stream=stream,
                answered_with="exhausted" if step is None else step.kind,
            )
        )
        if step is None:
            exhausted = Fail(status=500, message=f"fake model script {conversation!r} exhausted")
            await _json(send, 500, error_body(api, exhausted))
        elif isinstance(step, Fail):
            if await _delay(step.delay_s, receive):
                retry = [(b"retry-after", str(step.retry_after_s).encode())]
                extra = retry if step.retry_after_s else []
                await _json(send, step.status, error_body(api, step), extra)
        elif await _delay(step.delay_s, receive):
            await self._reply(api, step, body, stream, send)

    def _conversation(self, headers: dict[str, str], body: dict[str, Any]) -> str:
        named = headers.get(CONVERSATION_HEADER)
        if named:
            return named
        model = body.get("model")
        if isinstance(model, str) and self.book.has(model):
            return model
        return DEFAULT_CONVERSATION

    async def _reply(
        self, api: Api, reply: Reply, body: dict[str, Any], stream: bool, send: Send
    ) -> None:
        model = str(body.get("model") or "fake-model")
        if not stream:
            await _json(send, 200, BODY_RENDERERS[api](reply, model, self._ids))
            return
        headers = [(b"content-type", b"text/event-stream"), (b"cache-control", b"no-cache")]
        await send({"type": "http.response.start", "status": 200, "headers": headers})
        for frame in stream_frames(api, reply, model, self._ids, body):
            await send(
                {"type": "http.response.body", "body": encode_frame(frame), "more_body": True}
            )
        await send({"type": "http.response.body", "body": b""})

    async def _control(self, scope: Scope, raw: bytes, send: Send) -> None:
        """Script and inspect the server from another process over HTTP."""
        action = scope["path"].removeprefix(CONTROL_PREFIX).strip("/")
        query = dict(parse_qsl(scope["query_string"].decode()))
        conversation = query.get("conversation", DEFAULT_CONVERSATION)
        method = scope["method"]
        if action == "scripts" and method == "POST":
            self.script(_STEPS.validate_json(raw), conversation=conversation)
            await _json(send, 200, {"remaining": self.book.remaining(conversation)})
        elif action == "requests" and method == "GET":
            found = self.requests_for(query.get("conversation"))
            await _json(send, 200, [item.model_dump(mode="json") for item in found])
        elif action == "reset" and method == "POST":
            self.reset()
            await _json(send, 200, {"ok": True})
        else:
            await _json(send, 404, {"error": {"message": f"unknown control route {action}"}})


async def _read_body(receive: Receive) -> bytes:
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message["type"] != "http.request":
            return b"".join(chunks)
        chunks.append(message.get("body", b""))
        if not message.get("more_body"):
            return b"".join(chunks)


async def _delay(seconds: float, receive: Receive) -> bool:
    """Sleep, but stop at once if the client disconnects; False means no response is wanted."""
    if seconds <= 0:
        return True
    sleeper = asyncio.ensure_future(asyncio.sleep(seconds))
    watcher = asyncio.ensure_future(_until_disconnect(receive))
    done, pending = await asyncio.wait({sleeper, watcher}, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    return sleeper in done


async def _until_disconnect(receive: Receive) -> None:
    while (await receive())["type"] != "http.disconnect":
        pass


async def _json(
    send: Send, status: int, payload: object, extra_headers: list[tuple[bytes, bytes]] | None = None
) -> None:
    data = json.dumps(payload).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(data)).encode())]
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": headers + (extra_headers or []),
        }
    )
    await send({"type": "http.response.body", "body": data})
