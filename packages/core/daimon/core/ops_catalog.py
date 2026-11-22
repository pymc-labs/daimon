"""What an ops event can be: the fixed catalog of codes, areas and reasons. Pure, no I/O.

Each code names its area, severity, plain-words title, what its issues are about (the
subject) and whether one issue is made per person. A fingerprint groups events into one
issue: the code, area, subject and reason (and the person where the code says so).
Reasons are a closed vocabulary (a termination reason, an exception class name, an HTTP
status, a skip reason or a category), never exception text; `clean_reason` enforces that.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal, get_args

OpsCode = Literal[
    "turn.failed",
    "turn.shed",
    "dm.failed",
    "routine.fire_failed",
    "routine.goal_unmet",
    "routine.delivery_failed",
    "routine.owner_notice_failed",
    "delivery.output_failed",
    "watch.poll_failed",
    "watch.paused_failing",
    "wake.failed",
    "bridge.relay_failed",
    "bridge.relay_refused",
    "bridge.tool_errors",
    "connector.call_failed",
    "connector.reconnect_needed",
]
OpsArea = Literal["turn", "dm", "routine", "delivery", "watch", "wake", "bridge"]
OpsSeverity = Literal["error", "warning"]
SubjectKind = Literal["agent", "routine", "watch", "server", "connector", "platform", "wake"]

OPS_CODES: Final[tuple[OpsCode, ...]] = get_args(OpsCode)
OPS_AREAS: Final[tuple[OpsArea, ...]] = get_args(OpsArea)

AREA_LABELS: Final[Mapping[OpsArea, str]] = {
    "turn": "Answers",
    "routine": "Routines",
    "bridge": "Bridged tools",
    "dm": "Direct messages",
    "watch": "Watches",
    "delivery": "Delivery",
    "wake": "Follow-ups",
}


@dataclass(frozen=True)
class CodeSpec:
    area: OpsArea
    severity: OpsSeverity
    title: str
    subject: SubjectKind
    by_person: bool = False


CATALOG: Final[Mapping[OpsCode, CodeSpec]] = {
    "turn.failed": CodeSpec("turn", "error", "Answer failed", "agent"),
    "turn.shed": CodeSpec("turn", "warning", "Turned away while busy", "platform"),
    "dm.failed": CodeSpec("dm", "error", "Direct message not answered", "platform"),
    "routine.fire_failed": CodeSpec("routine", "error", "Routine failed to run", "routine"),
    "routine.goal_unmet": CodeSpec(
        "routine", "warning", "Routine could not finish its task", "routine"
    ),
    "routine.delivery_failed": CodeSpec(
        "delivery", "error", "Routine result not delivered", "routine"
    ),
    "routine.owner_notice_failed": CodeSpec(
        "routine", "warning", "Owner not told of a failed run", "routine"
    ),
    "delivery.output_failed": CodeSpec(
        "delivery", "warning", "File or table not delivered", "platform"
    ),
    "watch.poll_failed": CodeSpec("watch", "warning", "Watch check failed", "watch"),
    "watch.paused_failing": CodeSpec(
        "watch", "error", "Watch paused after repeated errors", "watch"
    ),
    "wake.failed": CodeSpec("wake", "error", "Follow-up did not run", "wake"),
    "bridge.relay_failed": CodeSpec("bridge", "error", "Bridged tools unavailable", "server"),
    "bridge.relay_refused": CodeSpec("bridge", "warning", "Bridged tool call refused", "server"),
    "bridge.tool_errors": CodeSpec("bridge", "warning", "Bridged tool returned errors", "server"),
    "connector.call_failed": CodeSpec("bridge", "warning", "Connector call failed", "connector"),
    "connector.reconnect_needed": CodeSpec(
        "bridge", "warning", "Connector needs reconnecting", "connector", by_person=True
    ),
}

#: A reason is one token: no spaces, so no sentence or exception message passes.
_REASON = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
OTHER: Final = "other"
NOTE_MAX_CHARS: Final = 300


def clean_reason(reason: str | None) -> str | None:
    """`reason` if it is a fixed-vocabulary token, `other` if it is anything else."""
    if reason is None:
        return None
    return reason if _REASON.fullmatch(reason) else OTHER


def clean_note(note: str | None) -> str | None:
    """One trimmed line of at most `NOTE_MAX_CHARS`, or None when empty."""
    text = " ".join((note or "").split())[:NOTE_MAX_CHARS].strip()
    return text or None


def fingerprint_of(
    code: OpsCode, area: OpsArea, subject: str | None, reason: str | None, person: str | None
) -> str:
    """The stable id of an issue; 32 hex characters, so it is also a valid UUID string."""
    parts = (code, area, subject or "", reason or "", (CATALOG[code].by_person and person) or "")
    return hashlib.md5("|".join(parts).encode(), usedforsecurity=False).hexdigest()


_STATUS_WORDS: Final[Mapping[str, str]] = {
    "400": "Bad request",
    "401": "Not signed in",
    "403": "Refused",
    "404": "Not found",
    "413": "Too large",
    "429": "Rate limited",
    "502": "Server error",
    "503": "Unavailable",
}
_REASON_WORDS: Final[Mapping[str, str]] = {
    "upstream": "Provider error",
    "ceiling": "Ran too long",
    "timeout": "Timed out",
    "connection_lost": "Connection lost",
    "session_terminated": "Ended early",
    "mcp_degraded_empty": "Tools were down",
    "retrying_unsettled": "Still retrying",
    "rate_limited": "Rate limited",
    "resolver_miss": "Agent not found",
    "recovery_failed": "Could not recover",
    "reducer_bug": "Internal error",
    "session_preparation_failed": "Could not start",
    "admission_concurrency_shed": "Busy",
    "unknown": "Unknown",
    "discovery": "Could not open",
    "reply": "Reply failed",
    "dropped": "Dropped",
    "not_sent": "Not sent",
    "post_failed": "Post failed",
    "skipped": "Skipped",
    "upload_failed": "Upload failed",
    "expired": "Sign-in expired",
    "blocked_access": "Blocked access",
    "missing_input": "Missing input",
    "source_unavailable": "Source unavailable",
    "partial_result": "Partial result",
    "fly_job": "Model run",
}


def _words(text: str) -> str:
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text).replace("_", " ").replace("-", " ")
    spaced = spaced.strip().lower()
    return spaced[:1].upper() + spaced[1:]


def reason_label(reason: str | None) -> str | None:
    """A reason in plain words; one the catalog does not know reads as its own words."""
    if reason is None:
        return None
    return _STATUS_WORDS.get(reason) or _REASON_WORDS.get(reason) or _words(reason)


def subject_label(kind: SubjectKind, key: str) -> str:
    """The name of a subject with no stored record: a platform, server, connector or wake kind."""
    if kind == "platform":
        return key.capitalize()
    return _REASON_WORDS.get(key) or key
