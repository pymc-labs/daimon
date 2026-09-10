"""Pure decision logic for organic thread participation (replying in a thread unprompted).

Platform-agnostic and free of I/O. Three pieces:

- `resolve_participation`: the scope cascade. Deployment default, then
  workspace, channel, thread; the most specific explicit value wins, except
  that `DISABLED` at any tier is final for every tier below it. That is the
  difference between `OFF` (nobody asked yet, but a thread may be turned on)
  and `DISABLED` (an admin or operator has said no, and asking does nothing).
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


class ParticipationMode(StrEnum):
    ON = "on"
    OFF = "off"
    DISABLED = "disabled"


class ParticipationScope(StrEnum):
    WORKSPACE = "workspace"
    CHANNEL = "channel"
    THREAD = "thread"


ParticipationTier = Literal["deployment", "workspace", "channel", "thread"]


@dataclass(frozen=True)
class ResolvedParticipation:
    mode: ParticipationMode
    tier: ParticipationTier
    """The tier whose value is in effect: the one to change to get a different answer."""


def resolve_participation(
    *,
    deployment: ParticipationMode,
    workspace: ParticipationMode | None,
    channel: ParticipationMode | None,
    thread: ParticipationMode | None,
) -> ResolvedParticipation:
    """Walk the cascade broadest first: `DISABLED` stops the walk, else the last set value wins."""
    effective = ResolvedParticipation(deployment, "deployment")
    tiers: tuple[tuple[ParticipationMode | None, ParticipationTier], ...] = (
        (workspace, "workspace"),
        (channel, "channel"),
        (thread, "thread"),
    )
    for value, tier in tiers:
        if effective.mode is ParticipationMode.DISABLED:
            break
        if value is not None:
            effective = ResolvedParticipation(value, tier)
    return effective


class SkipReason(StrEnum):
    NOT_ON = "not_on"
    RATE_LIMITED = "rate_limited"
    CLASSIFIER_SILENCED = "classifier_silenced"


@dataclass(frozen=True)
class AutoRespondSnapshot:
    """Everything the pre-classifier gates need, gathered by the adapter."""

    mode: ParticipationMode
    auto_responses_in_window: int
    max_per_window: int


@dataclass(frozen=True)
class Respond:
    pass


@dataclass(frozen=True)
class Skip:
    reason: SkipReason


AutoRespondDecision = Respond | Skip


def decide_pre_classifier(snap: AutoRespondSnapshot) -> AutoRespondDecision:
    """Apply every gate that needs no model call. `Respond` means "ask the classifier"."""
    if snap.mode is not ParticipationMode.ON:
        return Skip(SkipReason.NOT_ON)
    if snap.auto_responses_in_window >= snap.max_per_window:
        return Skip(SkipReason.RATE_LIMITED)
    return Respond()


# --- Classifier prompt and verdict -------------------------------------------


@dataclass(frozen=True)
class ClassifierMessage:
    author_name: str
    content: str
    is_bot: bool


@dataclass(frozen=True)
class ClassifierVerdict:
    decision: str  # "respond" | "silence"
    reason: str
    confidence: float


SILENCE_ON_ERROR: Final[ClassifierVerdict] = ClassifierVerdict(
    decision="silence", reason="classifier_error", confidence=0.0
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
{{"decision": "respond" | "silence", "reason": "<short>", "confidence": 0.0-1.0}}
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
    if not isinstance(decision, str) or decision not in ("respond", "silence"):
        raise ValueError(f"unexpected decision: {decision!r}")
    confidence = payload.get("confidence", 0.0)
    return ClassifierVerdict(
        decision=decision,
        reason=str(payload.get("reason", "")),
        confidence=float(confidence) if isinstance(confidence, (int, float)) else 0.0,
    )


def _strip_code_fence(text: str) -> str:
    if not text.startswith("```"):
        return text
    body = text.split("\n", 1)[1] if "\n" in text else text[3:]
    if body.endswith("```"):
        body = body[: -len("```")]
    return body.strip()


def decide_post_classifier(verdict: ClassifierVerdict) -> AutoRespondDecision:
    if verdict.decision == "respond":
        return Respond()
    return Skip(SkipReason.CLASSIFIER_SILENCED)


# --- Notes returned by the participation tool ---------------------------------


def scope_label(scope: ParticipationScope, scope_id: str | None) -> str:
    return (
        "the whole workspace"
        if scope is ParticipationScope.WORKSPACE
        else f"{scope.value} {scope_id}"
    )


def build_participation_note(
    *,
    scope: ParticipationScope,
    scope_id: str | None,
    requested: ParticipationMode | None,
    effective: ResolvedParticipation,
) -> str:
    """One sentence stating what was written and what is now in effect at that scope.

    Returned by the tool rather than recalled from a prompt, so the agent reports
    the cascade's actual answer: a thread turned `on` under a `disabled` channel
    stays silent, and the note says so.
    """
    where = scope_label(scope, scope_id)
    wrote = (
        f"{where} now inherits its setting"
        if requested is None
        else f"{where} is set to {requested.value}"
    )
    if effective.mode is ParticipationMode.ON:
        return f"{wrote}; I will reply in threads there when a message calls for it."
    if effective.mode is ParticipationMode.DISABLED:
        return (
            f"{wrote}, but participation is disabled at the {effective.tier} level, "
            "so I only reply when @mentioned and this cannot be turned on below that level."
        )
    return f"{wrote}; I only reply there when @mentioned."
