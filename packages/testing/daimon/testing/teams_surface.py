"""Capture real Teams adapter activities through a network-free TeamsSender."""

from __future__ import annotations

import itertools
import time
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

from daimon.adapters.teams.lifecycle import TeamsTurnLifecycle, TimedSender
from daimon.core.turn.state import TurnState
from daimon.testing.platform_surface import PlatformSurfaceCapture, SurfaceRecorder, SurfaceWindow
from microsoft_teams.api import Account, MessageActivityInput, SentActivity
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_IDS = itertools.count()


def _decode(payload: dict[str, Any]) -> dict[str, Any]:
    cards: list[str] = []
    controls: list[str] = []
    fallbacks: list[str] = []
    opaque = False

    def walk(value: object) -> None:
        nonlocal opaque
        if isinstance(value, str):
            cards.append(value)
            return
        if not isinstance(value, dict):
            opaque = True
            return
        node = cast(dict[str, Any], value)
        kind = node.get("type")
        if node.get("isVisible") is False:
            return
        if kind == "TextBlock" or kind == "TextRun":
            cards.append(str(node.get("text", "")))
        elif kind in {"Action.Execute", "Action.Submit", "Action.OpenUrl"}:
            controls.append(str(node.get("title", "")))
        elif kind not in {
            "AdaptiveCard",
            "Container",
            "ColumnSet",
            "Column",
            "ActionSet",
            "RichTextBlock",
        }:
            opaque = True
        if node.get("selectAction") or node.get("backgroundImage") or node.get("iconUrl"):
            opaque = True
        if node.get("fallbackText"):
            fallbacks.append(str(node["fallbackText"]))
        for key in ("body", "items", "columns", "actions", "inlines"):
            for child in node.get(key, []):
                walk(child)

    for attachment in payload.get("attachments", []):
        if attachment.get("contentType") == "application/vnd.microsoft.card.adaptive":
            walk(attachment.get("content", {}))
        else:
            opaque = True
    text = payload.get("text", "")
    return dict(
        body_texts=(text,) if text else (),
        card_texts=tuple(cards),
        control_labels=tuple(controls),
        fallback_text="\n".join(fallbacks),
        opaque_text=opaque,
    )


class OfflineTeamsSender:
    """Receives actual SDK activity objects; SDK aliases are serialized before capture."""

    def __init__(self, recorder: SurfaceRecorder, *, service_url: str | None) -> None:
        self.recorder = recorder
        self.service_url = service_url
        self._failures: dict[int, Exception] = {}
        self.calls = 0

    def fail_on(self, call: int, error: Exception) -> None:
        self._failures[call] = error

    async def send(
        self,
        conversation_id: str,
        activity: MessageActivityInput,
        *,
        service_url: str | None,
    ) -> SentActivity:
        index = self.calls
        self.calls += 1
        try:
            if error := self._failures.pop(index, None):
                raise error
            if conversation_id != self.recorder.destination_id or service_url != self.service_url:
                raise ValueError("Teams capture cannot accept another destination")
            payload = activity.model_dump(mode="json", by_alias=True, exclude_none=True)
            message_id = activity.id or f"qa-activity-{next(_IDS)}"
            operation = "edit" if activity.id is not None else "send"
            self.recorder.record(operation, message_id, payload, payload)
            return SentActivity(id=message_id, activity_params=activity)
        except Exception:
            self.recorder.delivery_failures += 1
            raise


class _CapturedLifecycle(TeamsTurnLifecycle):
    terminal_deliveries = 0

    async def on_terminal_success(self, state: TurnState) -> None:
        await super().on_terminal_success(state)
        self.terminal_deliveries += 1

    async def on_terminal_failure(self, state: TurnState, err: Exception) -> None:
        await super().on_terminal_failure(state, err)
        self.terminal_deliveries += 1


class TeamsSurfaceHarness:
    def __init__(
        self,
        *,
        evidence_id: str,
        session_id: str,
        root_turn_id: str,
        conversation_id: str = "19:qa-thread",
        service_url: str | None = "https://teams.invalid/",
        clock: Callable[[], float] = time.monotonic,
        cancel_key: str = "qa-cancel",
        unprompted: bool = False,
        completion_ping: bool = False,
        requester: Account | None = None,
        agent_name_prefix: str | None = None,
        ask_human: bool = False,
        direct_chat: bool = False,
        agent_name: str = "Daimon",
        model_id: str = "claude-haiku-5-5",
        markup: Decimal = Decimal(1),
        sessionmaker: async_sessionmaker[AsyncSession] | None = None,
        tenant_id: UUID | None = None,
        budget_channel_id: str | None = None,
    ) -> None:
        self.recorder = SurfaceRecorder(
            evidence_id=evidence_id,
            destination_id=conversation_id,
            thread_id=None,
            clock=clock,
            decode=_decode,
        )
        self._window = SurfaceWindow(
            platform="teams",
            evidence_id=evidence_id,
            session_id=session_id,
            root_turn_id=root_turn_id,
            recorder=self.recorder,
        )
        self.sender = OfflineTeamsSender(self.recorder, service_url=service_url)
        self.lifecycle = _CapturedLifecycle(
            sender=TimedSender(self.sender),
            conversation_id=conversation_id,
            service_url=service_url,
            cancel_key=cancel_key,
            clock=clock,
            unprompted=unprompted,
            completion_ping=completion_ping,
            requester=requester,
            agent_name_prefix=agent_name_prefix,
            ask_human=ask_human,
            direct_chat=direct_chat,
            agent_name=agent_name,
            model_id=model_id,
            markup=markup,
            sessionmaker=sessionmaker,
            tenant_id=tenant_id,
            budget_channel_id=budget_channel_id,
        )

    async def run(
        self,
        invoke: Callable[[TeamsTurnLifecycle], Awaitable[object]],
    ) -> PlatformSurfaceCapture:
        return await self._window.run(
            lambda: invoke(self.lifecycle), lambda: self.lifecycle.terminal_deliveries
        )

    def snapshot(self) -> PlatformSurfaceCapture:
        return self._window.snapshot()
