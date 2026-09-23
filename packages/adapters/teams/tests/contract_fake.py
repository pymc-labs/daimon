"""A Bot Framework transport fake that answers the way Teams does.

``conftest.TeamsApiFake`` answers every POST with ``{"id": ...}``. Teams
does not: only the first request of a stream gets ``201 {"id"}``, every
later streaming request (informative, streaming, final) gets ``202 {}``,
and a stopped, timed-out or oversized stream is refused with a 403. Code
that only ever sees ``{"id"}`` never exercises the SDK's placeholder-id
path, its 403 classification, or the adapter's handling of either.

``TeamsContractFake`` is an SDK ``Middleware`` (the same seam as
``TeamsApiFake``) that keeps a model of what the user sees in the chat and
answers from that model. Each response shape is tagged with its source:

- DOC: https://learn.microsoft.com/en-us/microsoftteams/platform/bots/streaming-ux
  ("Response codes" and "Stop streaming agent response"), read 2026-09-23.
- GUESS: not documented and not observed. To be confirmed in a live spike;
  change the constant here, not the tests.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any, Literal, cast

import httpx
from microsoft_teams.common import Client, ClientOptions  # pyright: ignore[reportMissingTypeStubs]
from microsoft_teams.common.http.client import (  # pyright: ignore[reportMissingTypeStubs]
    MiddlewareContext,
    _wrap_response_json,  # pyright: ignore[reportPrivateUsage]
)

# DOC: every streaming 403 carries error code ContentStreamNotAllowed.
STREAM_ERROR_CODE = "ContentStreamNotAllowed"
# DOC (message text). The docs table writes "canceled" without a trailing
# period and the Stop section writes it with one; the SDK only matches "cancel".
CANCELED_BY_USER = "Content stream was canceled by user."
# DOC
EXCEEDED_STREAMING_TIME = "Content stream finished due to exceeded streaming time."
# DOC: the streaming size error is a 403, not a 413.
MESSAGE_SIZE_TOO_LARGE = "Message size too large"
# DOC
ALREADY_COMPLETED = "Content stream is not allowed on an already completed streamed message"
# DOC (message text); GUESS that this is what a second concurrent stream in
# the same chat gets. The docs only say one stream per chat is supported.
STREAM_NOT_ALLOWED = "Content stream is not allowed"

# GUESS: the Bot Framework ErrorResponse envelope. The SDK parses exactly
# this shape (http_stream.py `_send`: response.json()["error"]["message"]).
ERROR_ENVELOPE = "error"

# GUESS: status for a plain (non-streaming) message create. Bot Framework
# REST documents a ResourceResponse body; 201 vs 200 is not pinned here.
PLAIN_CREATE_STATUS = 201
# GUESS: a plain message over the size limit. Teams' documented limit is
# ~100 KB; the status the connector returns for it was not observed.
PLAIN_TOO_LARGE_STATUS = 413

JsonDict = dict[str, Any]
PutBody = Literal["id", "empty"]
SecondStream = Literal["allow", "reject"]

_ACTIVITIES = re.compile(r"/v3/conversations/(?P<conv>[^/]+)/activities(?:/(?P<id>[^/]+))?$")


@dataclasses.dataclass
class VisibleMessage:
    """One message bubble as the user sees it."""

    id: str
    conversation_id: str
    kind: Literal["stream", "plain"]
    body: JsonDict
    # open: streaming; final: finished; stopped: user pressed Stop;
    # timed_out: 2-minute limit hit, only a non-streaming PUT can still edit it.
    state: Literal["open", "final", "stopped", "timed_out"] = "open"
    stream_requests: int = 0

    @property
    def text(self) -> str:
        return json.dumps(self.body)


@dataclasses.dataclass
class Exchange:
    method: str
    url: str
    body: JsonDict
    status: int
    response: JsonDict
    at: float = dataclasses.field(default_factory=time.monotonic)


@dataclasses.dataclass
class TeamsContractFake:
    """Answers outbound Bot Framework calls with Teams' documented shapes.

    Knobs (all default to the documented happy path):

    - ``put_body``: what a non-streaming PUT returns. ``"id"`` is the Bot
      Framework ResourceResponse; ``"empty"`` is the unobserved F3 case.
    - ``second_stream``: what a second concurrent stream in one chat gets
      (N3). ``"reject"`` answers its start with 403 "not allowed".
    - ``stream_size_limit`` / ``plain_size_limit``: request-body bytes over
      which a streaming send gets 403 "Message size too large" and a plain
      send gets ``PLAIN_TOO_LARGE_STATUS``.
    - ``fail_final_status``: answer every ``streamType: final`` send with
      this status (a Bot Framework outage on the terminal send).

    Triggers: ``press_stop()`` (the Stop button), ``expire_streams()``
    (the 2-minute limit), and ``on_stream_request`` (called before each
    streaming request is answered, so a test can fire a trigger at an
    exact point in the stream).
    """

    put_body: PutBody = "id"
    second_stream: SecondStream = "allow"
    stream_size_limit: int | None = None
    plain_size_limit: int | None = None
    fail_final_status: int | None = None
    on_stream_request: Callable[[TeamsContractFake, VisibleMessage], None] | None = None

    messages: dict[str, VisibleMessage] = dataclasses.field(
        default_factory=dict[str, VisibleMessage]
    )
    exchanges: list[Exchange] = dataclasses.field(default_factory=list[Exchange])
    stop_refused: asyncio.Event = dataclasses.field(default_factory=asyncio.Event)
    _next_id: int = 1000

    # ---- triggers -------------------------------------------------------

    def press_stop(self) -> None:
        for message in self.open_streams():
            message.state = "stopped"

    def expire_streams(self) -> None:
        for message in self.open_streams():
            message.state = "timed_out"

    # ---- what the user sees ----------------------------------------------

    def open_streams(self, conversation_id: str | None = None) -> list[VisibleMessage]:
        return [
            m
            for m in self.messages.values()
            if m.kind == "stream"
            and m.state == "open"
            and (conversation_id is None or m.conversation_id == conversation_id)
        ]

    def bubbles_containing(self, needle: str) -> list[VisibleMessage]:
        return [m for m in self.messages.values() if needle in m.text]

    def first_delivery_of(self, needle: str) -> Exchange | None:
        """The first accepted request that put ``needle`` in front of the user."""
        return next(
            (e for e in self.exchanges if e.status < 300 and needle in json.dumps(e.body)), None
        )

    def requests_to(self, message_id: str) -> list[Exchange]:
        return [
            e
            for e in self.exchanges
            if e.body.get("id") == message_id or e.url.endswith(f"/activities/{message_id}")
        ]

    # ---- transport ------------------------------------------------------

    def _new_id(self) -> str:
        self._next_id += 1
        return f"1728640{self._next_id}"  # DOC: stream ids look like "1728640934763"

    async def send(
        self,
        context: MiddlewareContext,
        next: Callable[[], Awaitable[httpx.Response]],
    ) -> httpx.Response:
        del next  # nothing leaves the test process
        # The SDK's conversation client always sends activities as ``json=``.
        raw: object = context.json  # pyright: ignore[reportUnknownMemberType]
        body: JsonDict = cast(JsonDict, raw) if isinstance(raw, dict) else {}
        status, payload = self._answer(context.method, httpx.URL(context.url).path, body)
        self.exchanges.append(Exchange(context.method, context.url, body, status, payload))
        response = httpx.Response(
            status, json=payload, request=httpx.Request(context.method, context.url)
        )
        # Replace the SDK's terminal sender faithfully: it raises on non-2xx
        # and makes an empty body read as {}. A middleware that skips
        # ``next`` skips both, and a 502 would then look like a success.
        response.raise_for_status()
        _wrap_response_json(response)
        return response

    def _answer(self, method: str, path: str, body: JsonDict) -> tuple[int, JsonDict]:
        match = _ACTIVITIES.search(path)
        if match is None:
            return 200, {}
        conversation_id, target = match.group("conv"), match.group("id")
        size = len(json.dumps(body).encode())

        if method == "PUT" and target is not None:
            return self._put(target, conversation_id, body)
        if method != "POST":
            return 200, {}

        stream_info = _stream_info(body)
        if stream_info is None:
            return self._plain(conversation_id, body, size)
        stream_id = body.get("id") or stream_info.get("streamId")
        if not stream_id:
            return self._start_stream(conversation_id, body, size)
        return self._continue_stream(str(stream_id), body, stream_info, size)

    def _plain(self, conversation_id: str, body: JsonDict, size: int) -> tuple[int, JsonDict]:
        if self.plain_size_limit is not None and size > self.plain_size_limit:
            return PLAIN_TOO_LARGE_STATUS, _error("MessageSizeTooBig", MESSAGE_SIZE_TOO_LARGE)
        message = VisibleMessage(
            id=self._new_id(), conversation_id=conversation_id, kind="plain", body=body
        )
        message.state = "final"
        self.messages[message.id] = message
        return PLAIN_CREATE_STATUS, {"id": message.id}

    def _start_stream(
        self, conversation_id: str, body: JsonDict, size: int
    ) -> tuple[int, JsonDict]:
        if self.second_stream == "reject" and self.open_streams(conversation_id):
            return 403, _error(STREAM_ERROR_CODE, STREAM_NOT_ALLOWED)
        if self.stream_size_limit is not None and size > self.stream_size_limit:
            return 403, _error(STREAM_ERROR_CODE, MESSAGE_SIZE_TOO_LARGE)
        message = VisibleMessage(
            id=self._new_id(), conversation_id=conversation_id, kind="stream", body=body
        )
        message.stream_requests = 1
        self.messages[message.id] = message
        return 201, {"id": message.id}  # DOC: 201 created {"id": streamId}

    def _continue_stream(
        self,
        stream_id: str,
        body: JsonDict,
        stream_info: JsonDict,
        size: int,
    ) -> tuple[int, JsonDict]:
        message = self.messages.get(stream_id)
        if message is None:
            return 404, _error("ActivityNotFound", "Unknown stream id")  # GUESS
        if self.on_stream_request is not None:
            self.on_stream_request(self, message)
        message.stream_requests += 1
        if message.state == "stopped":
            self.stop_refused.set()
            return 403, _error(STREAM_ERROR_CODE, CANCELED_BY_USER)
        if message.state == "timed_out":
            return 403, _error(STREAM_ERROR_CODE, EXCEEDED_STREAMING_TIME)
        if message.state == "final":
            return 403, _error(STREAM_ERROR_CODE, ALREADY_COMPLETED)
        if self.stream_size_limit is not None and size > self.stream_size_limit:
            return 403, _error(STREAM_ERROR_CODE, MESSAGE_SIZE_TOO_LARGE)
        is_final = stream_info.get("streamType") == "final"
        if is_final and self.fail_final_status is not None:
            return self.fail_final_status, _error("ServiceError", "Bot Framework outage")
        message.body = body
        if is_final:
            message.state = "final"
        return 202, {}  # DOC: 202 {} for every request after the first

    def _put(self, target: str, conversation_id: str, body: JsonDict) -> tuple[int, JsonDict]:
        message = self.messages.get(target)
        if message is None or message.conversation_id != conversation_id:
            return 404, _error("ActivityNotFound", "Unknown activity id")  # GUESS
        # GUESS: a non-streaming PUT onto a timed-out (or open) stream edits
        # the bubble and ends the stream. The SDK relies on this after the
        # 2-minute limit; it has not been observed.
        message.body = body
        message.state = "final"
        return 200, ({"id": target} if self.put_body == "id" else {})


def _stream_info(body: JsonDict) -> JsonDict | None:
    entities = body.get("entities")
    for entity in cast(list[Any], entities) if isinstance(entities, list) else []:
        if isinstance(entity, dict):
            typed = cast(JsonDict, entity)
            if str(typed.get("type", "")).lower() == "streaminfo":
                return typed
    return None


def _error(code: str, message: str) -> JsonDict:
    return {ERROR_ENVELOPE: {"code": code, "message": message}}


def build_contract_client(fake: TeamsContractFake) -> Client:
    client = Client(ClientOptions())
    client.use(fake)
    return client
