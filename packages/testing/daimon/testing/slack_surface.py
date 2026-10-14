"""Capture the real Slack adapter at a network-free Slack SDK API boundary."""

from __future__ import annotations

import asyncio
import itertools
import json as json_module
import time
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

from daimon.adapters.slack.lifecycle import SlackTurnLifecycle
from daimon.core.agent_identity import AgentIdentity
from daimon.core.turn.state import TurnState
from daimon.testing.platform_surface import PlatformSurfaceCapture, SurfaceRecorder, SurfaceWindow
from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.web.async_slack_response import AsyncSlackResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_TS = itertools.count(1700000000000000)


def _decode(payload: dict[str, Any]) -> dict[str, Any]:
    body: list[str] = []
    cards: list[str] = []
    controls: list[str] = []
    opaque = bool(payload.get("attachments") or payload.get("files") or payload.get("file_ids"))

    def text(node: object) -> tuple[str, ...]:
        nonlocal opaque
        if isinstance(node, str):
            return (node,) if node else ()
        if node is None:
            return ()
        if isinstance(node, list):
            return tuple(part for item in cast(list[object], node) for part in text(item))
        if isinstance(node, dict):
            value = cast(dict[str, Any], node)
            kind = value.get("type")
            if kind in {"plain_text", "mrkdwn", "raw_text", "text", "markdown"}:
                return text(value.get("text"))
            if kind in {
                "rich_text",
                "rich_text_section",
                "rich_text_list",
                "rich_text_preformatted",
                "rich_text_quote",
            }:
                return text(value.get("elements", []))
        # Images, mentions resolved by Slack, links, and unknown nodes require
        # a richer renderer. Never silently treat their text as fully observed.
        opaque = True
        return ()

    blocks = payload.get("blocks", [])
    for block in blocks:
        kind = block.get("type")
        if kind in {"markdown", "rich_text"}:
            body.extend(text(block))
        elif kind in {"section", "header"}:
            cards.extend(text(block.get("text")))
            cards.extend(text(block.get("fields", [])))
            if block.get("accessory"):
                opaque = True
        elif kind == "context":
            cards.extend(text(block.get("elements", [])))
        elif kind == "actions":
            for element in block.get("elements", []):
                controls.extend(text(element.get("text")))
                if element.get("type") != "button":
                    opaque = True
        elif kind == "table":
            for row in block.get("rows", []):
                body.extend(text(row))
        elif kind != "divider":
            opaque = True
    if not blocks:
        body.extend(text(payload.get("text", "")))
    return dict(
        body_texts=tuple(body),
        card_texts=tuple(cards),
        control_labels=tuple(controls),
        fallback_text=str(payload.get("text", "")),
        opaque_text=opaque,
    )


class OfflineSlackClient(AsyncWebClient):
    """Real SDK methods build their payloads; api_call never opens a connection."""

    def __init__(self, recorder: SurfaceRecorder) -> None:
        super().__init__(token="offline-n5-surface", base_url="https://slack.invalid/api/")  # pyright: ignore[reportUnknownMemberType]
        self.recorder = recorder
        self._failures: dict[str, Exception] = {}

    def fail_next(self, method: str, error: Exception) -> None:
        self._failures[method] = error

    async def api_call(
        self,
        api_method: str,
        *,
        http_verb: str = "POST",
        files: object = None,
        data: object = None,
        params: object = None,
        json: object = None,
        headers: object = None,
        auth: object = None,
    ) -> AsyncSlackResponse:
        try:
            return self._accept(api_method, http_verb, files, data, params, json)
        except Exception:
            self.recorder.delivery_failures += 1
            raise

    def _accept(
        self,
        method: str,
        http_verb: str,
        files: object,
        data: object,
        params: object,
        json: object,
    ) -> AsyncSlackResponse:
        if error := self._failures.pop(method, None):
            raise error
        if method not in {"chat.postMessage", "chat.update", "chat.delete"} or files:
            raise ValueError("unsupported offline Slack operation")
        values = json if json is not None else data if data is not None else params
        if not isinstance(values, dict):
            raise ValueError("Slack call has no serialized payload")
        request = cast(dict[str, Any], json_module.loads(json_module.dumps(values)))
        if request.get("channel") != self.recorder.destination_id:
            raise ValueError("Slack capture cannot accept another channel")
        if method == "chat.postMessage":
            if request.get("thread_ts") != self.recorder.thread_id:
                raise ValueError("Slack capture cannot accept another thread")
            value = next(_TS)
            ts = f"{value // 1000000}.{value % 1000000:06d}"
            payload = dict(request, ts=ts)
            operation = "send"
        else:
            ts = str(request["ts"])
            payload = self.recorder.payload(ts)
            operation = "delete" if method == "chat.delete" else "edit"
            if operation == "edit":
                if "text" in request and "blocks" not in request:
                    # The real lifecycle always specifies blocks for its updates.
                    raise ValueError("unsupported Slack text-only block update semantics")
                payload.update(request)
        self.recorder.record(operation, ts, request, payload)
        return AsyncSlackResponse(
            client=self,
            http_verb=http_verb,
            api_url=f"offline://slack/{method}",
            req_args={"json": request},
            data={
                "ok": True,
                "channel": self.recorder.destination_id,
                "ts": ts,
                "message": payload,
            },
            headers={},
            status_code=200,
        )


class _CapturedLifecycle(SlackTurnLifecycle):
    terminal_deliveries = 0

    async def on_terminal_success(self, state: TurnState) -> None:
        await super().on_terminal_success(state)
        self.terminal_deliveries += 1

    async def on_terminal_failure(self, state: TurnState, err: Exception) -> None:
        await super().on_terminal_failure(state, err)
        self.terminal_deliveries += 1


class SlackSurfaceHarness:
    def __init__(
        self,
        *,
        evidence_id: str,
        session_id: str,
        root_turn_id: str,
        channel: str = "C_QA",
        thread_ts: str = "1699999999.000001",
        author_id: str = "U_QA",
        clock: Callable[[], float] = time.monotonic,
        agent_name: str = "Daimon",
        model_id: str = "claude-haiku-5-5",
        markup: Decimal = Decimal(1),
        render_tables: bool = False,
        notify_on_completion: bool = False,
        ask_human: bool = False,
        identity: AgentIdentity | None = None,
        sessionmaker: async_sessionmaker[AsyncSession] | None = None,
        tenant_id: UUID | None = None,
        budget_channel_id: str | None = None,
    ) -> None:
        self.recorder = SurfaceRecorder(
            evidence_id=evidence_id,
            destination_id=channel,
            thread_id=thread_ts,
            clock=clock,
            decode=_decode,
        )
        self._window = SurfaceWindow(
            platform="slack",
            evidence_id=evidence_id,
            session_id=session_id,
            root_turn_id=root_turn_id,
            recorder=self.recorder,
        )
        self.client = OfflineSlackClient(self.recorder)
        self.cancel = asyncio.Event()
        self.registered: dict[str, str] = {}
        self.lifecycle = _CapturedLifecycle(
            client=self.client,
            channel=channel,
            thread_ts=thread_ts,
            cancel=self.cancel,
            author_id=author_id,
            agent_name=agent_name,
            model_id=model_id,
            markup=markup,
            register=lambda ts, cancel, author: self.registered.__setitem__(ts, author),
            deregister=self._deregister,
            clock=clock,
            render_tables=render_tables,
            notify_on_completion=notify_on_completion,
            ask_human=ask_human,
            identity=identity,
            sessionmaker=sessionmaker,
            tenant_id=tenant_id,
            budget_channel_id=budget_channel_id,
        )

    def _deregister(self, ts: str) -> None:
        self.registered.pop(ts, None)

    async def run(
        self,
        invoke: Callable[[SlackTurnLifecycle], Awaitable[object]],
    ) -> PlatformSurfaceCapture:
        return await self._window.run(
            lambda: invoke(self.lifecycle), lambda: self.lifecycle.terminal_deliveries
        )

    def snapshot(self) -> PlatformSurfaceCapture:
        return self._window.snapshot()
