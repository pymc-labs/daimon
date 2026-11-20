"""A case's script, played as the people in the conversation: steps fire on chat events and
act (press a button, react, reply, stop); defaults answer whatever no step does."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Final, Literal, Protocol

from daimon.core.evals.chat.events import ChatLog, answered
from daimon.core.evals.effects import glob_matches
from daimon.core.evals.format import (
    ButtonChoice,
    CardButton,
    Case,
    Check,
    NeutralCard,
    PlatformPayloads,
    Script,
    ScriptAction,
    ScriptDefaults,
    ScriptStep,
    Trigger,
)
from daimon.core.evals.scoring.checks import check_value

EventKind = Literal["card", "message", "reaction", "form", "credential_request", "tool_call",
                    "turn_end", "start"]  # fmt: skip
MAX_WAIT_MS: Final = 10_000
FAKE_SECRET: Final = "eval-fake-secret"
HIDDEN: Final = "[secret]"
SUBMIT: Final = CardButton(id="submit", label="Submit", style="primary", action="submit")


class Presses(Protocol):
    """The real submit paths a card's answer enters; a card's id is its request token."""

    async def submit_credential(self, token: str, value: str) -> bool: ...

    async def skip_credential(self, token: str) -> bool: ...


RequesterReply = Callable[[str], Awaitable[str | None]]
"""The simulated requester: the agent's question in, the person's reply out."""


@dataclass(frozen=True)
class Seen:
    """One thing that happened in the chat, as triggers read it."""

    event: EventKind
    card: NeutralCard | None = None
    text: str | None = None
    tool: str | None = None
    server: str | None = None


async def regex_hit(pattern: str, text: str) -> bool:
    """The case regex rule (`check_value`): bounded, timed out, off the event loop."""
    return await asyncio.to_thread(check_value, Check(op="regex", pattern=pattern), True, text)


async def _fits(on: Trigger, seen: Seen) -> bool:
    if on.event != seen.event:
        return False
    card = seen.card
    if on.card_kind is not None and (card is None or card.kind != on.card_kind):
        return False
    if on.button_action is not None and not any(
        b.action == on.button_action for b in ((card and card.buttons) or [])
    ):
        return False
    text = seen.text or ""
    if on.text_contains is not None and on.text_contains.lower() not in text.lower():
        return False
    if on.tool is not None and not (
        glob_matches(on.tool.tool, seen.tool) and glob_matches(on.tool.server, seen.server)
    ):
        return False
    return on.text_regex is None or await regex_hit(on.text_regex, text)


async def pick_button(card: NeutralCard, choice: ButtonChoice | None) -> CardButton | None:
    for button in card.buttons or []:
        if choice is None:
            return button
        if choice.id is not None and button.id != choice.id:
            continue
        if choice.action is not None and button.action != choice.action:
            continue
        if choice.label_regex is not None and not await regex_hit(choice.label_regex, button.label):
            continue
        return button
    return None


def _redacted(card: NeutralCard, fields: dict[str, str] | None) -> dict[str, str] | None:
    secret = {f.id for f in card.fields or [] if f.kind == "secret"}
    return fields and {k: HIDDEN if k in secret else v for k, v in fields.items()}


def _default(card: NeutralCard, defaults: ScriptDefaults) -> ButtonChoice | None:
    if card.kind in ("confirmation", "host_call", "provider_action"):
        rule = defaults.confirmation
        if rule == "ignore":
            return None
        if rule == "always" and not any(b.action == "always" for b in card.buttons or []):
            rule = "approve"
        return ButtonChoice(action=rule)
    if card.kind == "credential_request" and defaults.credential_request == "skip":
        return ButtonChoice(action="skip")
    if card.kind == "form" and defaults.form == "skip":
        return ButtonChoice(action="skip")
    return None


@dataclass
class ScriptPlayer:
    case: Case
    chat: ChatLog
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    replies: list[str] = field(default_factory=list[str])
    """Follow-up messages the script sent, for the next turn."""
    presses: Presses | None = None
    simulate_reply: RequesterReply | None = None
    _form: dict[str, str] | None = None
    _hits: dict[str, int] = field(default_factory=dict[str, int])
    _fired: set[str] = field(default_factory=set[str])

    @property
    def script(self) -> Script:
        return self.case.script or Script()

    @property
    def requester(self) -> str:
        people = self.case.surface.participants
        return next((p.id for p in people if p.is_requester), people[0].id if people else "user")

    async def _steps(self, seen: Seen) -> list[ScriptStep]:
        due: list[ScriptStep] = []
        for step in self.script.steps or []:
            if (step.once and step.id in self._fired) or not await _fits(step.on, seen):
                continue
            self._hits[step.id] = hits = self._hits.get(step.id, 0) + 1
            if hits >= step.on.nth:
                self._fired.add(step.id)
                due.append(step)
        return due

    async def happened(self, seen: Seen) -> CardButton | None:
        """Play every step `seen` fires; the button pressed on its card, if any."""
        pressed: CardButton | None = None
        for step in await self._steps(seen):
            if step.delay_ms:
                await asyncio.sleep(min(step.delay_ms, MAX_WAIT_MS) / 1000)
            for action in step.do:
                button = await self._act(step, action, seen.card)
                pressed = pressed or button
        return pressed

    async def answer(
        self, card: NeutralCard, payloads: PlatformPayloads | None = None
    ) -> CardButton | None:
        """Show `card` and press what the script, or else its defaults, press on it."""
        self.chat.add("card", card=card, platform_payloads=payloads)
        kind: EventKind = "credential_request" if card.kind == "credential_request" else "card"
        defaults = self.script.defaults or ScriptDefaults()
        self._form = None
        pressed = await self.happened(Seen(kind, card=card))
        fields, self._form = self._form, None
        if pressed is None and fields is None:
            if card.kind == "credential_request" and defaults.credential_request == "submit_fake":
                fields = {f.id: FAKE_SECRET for f in card.fields or [] if f.kind == "secret"}
                pressed = SUBMIT
            elif choice := _default(card, defaults):
                pressed = await pick_button(card, choice)
        if pressed is not None:
            await self._press(card, pressed, fields)
        shown = _redacted(card, fields)
        self.chat.add("card_update", self.requester if pressed else "bot",
                      card=answered(card, pressed, self.requester, shown))  # fmt: skip
        if pressed is None and card.kind == "question" and defaults.question == "simulate":
            await self.question(card.body or card.headline or "")
        return pressed

    async def _press(
        self, card: NeutralCard, button: CardButton, fields: dict[str, str] | None
    ) -> None:
        """Send the answer where a person's press sends it."""
        if card.kind == "credential_request" and self.presses is not None:
            secret = next((f.id for f in card.fields or [] if f.kind == "secret"), None)
            if button.action == "skip":
                await self.presses.skip_credential(card.id)
            elif fields and secret in fields:
                await self.presses.submit_credential(card.id, fields[secret])
        elif fields and card.kind in ("form", "question"):
            self.replies.append("\n".join(f"{k}: {v}" for k, v in fields.items()))

    async def question(self, text: str) -> None:
        """Answer an unscripted question as the requester would, when the case asks for it."""
        if (self.script.defaults or ScriptDefaults()).question != "simulate" or not text.strip():
            return
        if self.simulate_reply is None or not (reply := await self.simulate_reply(text)):
            return
        self.chat.add("message", self.requester, message_id=self.chat.message_id(), text=reply,
                      scripted="simulated")  # fmt: skip
        self.replies.append(reply)

    async def _act(
        self, step: ScriptStep, action: ScriptAction, card: NeutralCard | None
    ) -> CardButton | None:
        who = action.as_ or self.requester
        match action.action:
            case "click" if card is not None:
                return await pick_button(card, action.button)
            case "submit_form" if card is not None:
                self._form = dict(action.fields or {})
                return next(
                    (b for b in card.buttons or [] if b.action in ("submit", "save")), SUBMIT
                )
            case "react" | "unreact":
                kind = "reaction_add" if action.action == "react" else "reaction_remove"
                self.chat.add(kind, who, emoji=action.emoji, message_id=action.target or
                              self._last_bot_message(), scripted=step.id)  # fmt: skip
            case "reply" if action.text:
                self.chat.add("message", who, message_id=self.chat.message_id(),
                              text=action.text, scripted=step.id)  # fmt: skip
                self.replies.append(action.text)
            case "edit" | "delete":
                self.chat.add(action.action, who, message_id=action.target, text=action.text,
                              scripted=step.id)  # fmt: skip
            case "stop":
                self.chat.add("status", who, text="stop", scripted=step.id)
                self.stop.set()
            case "wait":
                await asyncio.sleep(min(action.ms or 0, MAX_WAIT_MS) / 1000)
            case _:
                pass
        return None

    def _last_bot_message(self) -> str | None:
        return next(
            (e.message_id for e in reversed(self.chat.events)
             if e.kind == "message" and e.actor == "bot"),
            None,
        )  # fmt: skip
