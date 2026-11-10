"""Pure decision logic for organic thread participation (replying in a thread unprompted).

Platform-agnostic and free of I/O. Three pieces:

- Whether a thread is followed is the `participation` feature (`daimon.core.features`).
- `decide_pre_classifier`: the gates that cost nothing, applied to a snapshot
  the adapter gathered. `Respond` here means "spend a classifier call".
- The classifier prompt and verdict parser; `decide_post_classifier` maps the
  verdict back to a decision.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Literal, cast
from xml.sax.saxutils import escape, quoteattr


class SkipReason(StrEnum):
    NOT_ON = "not_on"
    RATE_LIMITED = "rate_limited"
    CLASSIFIER_SILENCED = "classifier_silenced"


@dataclass(frozen=True)
class ParticipationSnapshot:
    """Everything the pre-classifier gates need, gathered by the adapter."""

    on: bool
    auto_responses_in_window: int
    max_per_window: int


@dataclass(frozen=True)
class Respond:
    pass


@dataclass(frozen=True)
class Skip:
    reason: SkipReason


ParticipationDecision = Respond | Skip


def decide_pre_classifier(snapshot: ParticipationSnapshot) -> ParticipationDecision:
    """Apply every gate that needs no model call. `Respond` means "ask the classifier"."""
    if not snapshot.on:
        return Skip(SkipReason.NOT_ON)
    if snapshot.auto_responses_in_window >= snapshot.max_per_window:
        return Skip(SkipReason.RATE_LIMITED)
    return Respond()


# --- Classifier prompt and verdict -------------------------------------------


@dataclass(frozen=True)
class ClassifierMessage:
    author_name: str
    content: str
    is_bot: bool


ClassifierDecision = Literal["respond", "silence"]


@dataclass(frozen=True)
class ClassifierVerdict:
    decision: ClassifierDecision
    reason: str


SILENCE_ON_ERROR: Final[ClassifierVerdict] = ClassifierVerdict(
    decision="silence", reason="classifier_error"
)


def classifier_system_prompt(bot_display_name: str) -> str:
    return f"""\
You decide whether {bot_display_name}, an AI data-science assistant that is a member
of a team chat, should reply to the newest messages in a thread it has been asked
to follow. Nobody @mentioned it; the people in the thread simply kept talking.
The messages arrived in a burst and the thread has gone quiet, so this is the
first moment {bot_display_name} could speak without interrupting.

Decide RESPOND when:
- A message asks a question no human has answered yet, and it is the kind of
  question {bot_display_name} has been helping with in this thread
- A message replies to, quotes, or addresses {bot_display_name}
- A message clearly expects {bot_display_name} to continue the work it was doing

Decide SILENCE when:
- The exchange is human-to-human (people talking to each other, nothing
  directed at {bot_display_name})
- The newest message is a closing pleasantry ("thanks", "got it", "ok")
- {bot_display_name} has nothing to add beyond what a human already said

Output EXACTLY this JSON and nothing else:
{{"decision": "respond" | "silence", "reason": "<short>"}}
"""


def build_classifier_prompt(
    recent: list[ClassifierMessage],
    candidates: list[ClassifierMessage],
    *,
    bot_display_name: str,
) -> str:
    """Render the recent window and the burst of candidate messages as XML, oldest first."""

    def _msg(m: ClassifierMessage) -> str:
        author = bot_display_name if m.is_bot else f"user:{m.author_name}"
        return f"<msg author={quoteattr(author)}>{escape(m.content)}</msg>"

    lines = ["<thread_window>", *(_msg(m) for m in recent), "</thread_window>"]
    lines += ["<candidates>", *(_msg(m) for m in candidates), "</candidates>"]
    return "\n".join(lines)


def parse_classifier_response(text: str) -> ClassifierVerdict:
    """Parse the model's JSON. Tolerates a markdown code fence. Raises ValueError otherwise."""
    try:
        raw: object = json.loads(_strip_code_fence(text.strip()))
    except json.JSONDecodeError as err:
        raise ValueError(f"classifier output is not JSON: {text!r}") from err
    if not isinstance(raw, dict):
        raise ValueError(f"classifier output is not an object: {text!r}")
    payload = cast(dict[str, object], raw)
    decision = payload.get("decision")
    reason = str(payload.get("reason", ""))
    # Branch per literal so the verdict's type is narrowed by the check itself.
    if decision == "respond":
        return ClassifierVerdict(decision="respond", reason=reason)
    if decision == "silence":
        return ClassifierVerdict(decision="silence", reason=reason)
    raise ValueError(f"unexpected decision: {decision!r}")


def _strip_code_fence(text: str) -> str:
    if not text.startswith("```"):
        return text
    body = text.split("\n", 1)[1] if "\n" in text else text[3:]
    if body.endswith("```"):
        body = body[: -len("```")]
    return body.strip()


def decide_post_classifier(verdict: ClassifierVerdict) -> ParticipationDecision:
    """Exhaustive over `ClassifierDecision`: a new verdict value fails type-checking here."""
    match verdict.decision:
        case "respond":
            return Respond()
        case "silence":
            return Skip(SkipReason.CLASSIFIER_SILENCED)
