"""The eval surface: a `TurnLifecycle` that records what a turn shows as neutral chat
events instead of posting it anywhere."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from anthropic.types import RawMessageStreamEvent
from daimon.core.confirmation import ConfirmationAnswer, ConfirmationPrompt
from daimon.core.evals.chat.events import ChatLog, confirmation_card
from daimon.core.evals.chat.previews import confirmation_payloads, message_payloads
from daimon.core.evals.chat.script import ScriptPlayer, Seen
from daimon.core.monitoring import MonitoringNotice
from daimon.core.turn.lifecycle import Acknowledgment, InterruptSource, ReconnectReason
from daimon.core.turn.state import TextBlock, ToolUseBlock, TurnState

_ANSWERS: dict[str, ConfirmationAnswer] = {"approve": "approved", "always": "approved"}


@dataclass
class EvalSurfaceLifecycle:
    """One item's chat across its turns. `reply_to` is the message a turn answers."""

    chat: ChatLog
    script: ScriptPlayer
    reply_to: str | None = None
    state: TurnState = field(default_factory=TurnState)
    error: Exception | None = None

    async def on_render(self, state: TurnState) -> None:
        self.state = state

    async def on_terminal_success(self, state: TurnState) -> None:
        self.state = state

    async def on_terminal_failure(self, state: TurnState, err: Exception) -> None:
        self.state, self.error = state, err

    async def on_sse_event(self, event: RawMessageStreamEvent) -> None:
        return None

    async def on_reconnect(self, reason: ReconnectReason) -> None:
        return None

    async def on_rate_limited(self, until: datetime | None) -> None:
        return None

    async def on_interrupt_sent(self, source: InterruptSource) -> None:
        return None

    async def on_acknowledgment(self, phase: Acknowledgment) -> None:
        kind = "reaction_add" if phase == "accepted" else "reaction_remove"
        self.chat.add(kind, emoji="👀", message_id=self.reply_to)

    async def on_monitoring_notice(self, notice: MonitoringNotice) -> bool:
        self.chat.add("notice", text=notice.text, format="plain")
        return True

    async def confirm(self, prompt: ConfirmationPrompt) -> ConfirmationAnswer:
        """The confirmation hook: the card goes to the chat and the script presses it."""
        card_id = f"c{uuid.uuid4().hex[:12]}"
        card = confirmation_card(card_id, prompt)
        pressed = await self.script.answer(card, confirmation_payloads(card_id, prompt))
        return "expired" if pressed is None else _ANSWERS.get(pressed.action, "denied")

    async def end_turn(self) -> str | None:
        """Post the turn's reply and play the script on it; the reply text, if any."""
        for block in self.state.content:
            if isinstance(block, ToolUseBlock):
                await self.script.happened(
                    Seen("tool_call", tool=block.name, server=block.mcp_server_name)
                )
        text = "\n\n".join(b.text for b in self.state.content if isinstance(b, TextBlock)).strip()
        if text:
            self.chat.add("message", message_id=self.chat.message_id(), text=text,
                          format="markdown", reply_to=self.reply_to,
                          platform_payloads=message_payloads(text))  # fmt: skip
            await self.script.happened(Seen("message", text=text))
        await self.script.happened(Seen("turn_end", text=text))
        return text or None
