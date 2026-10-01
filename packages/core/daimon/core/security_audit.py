"""Request-local authorization decisions, without arguments or message bodies.

The synchronous policy records into the current MCP request, never starting tasks
or doing database I/O. The middleware persists exactly one event for the request.
Outside an audit scope the pure policy retains its existing behavior.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass
class AuditDecision:
    operation: str | None = None
    denied: bool = False
    reason: str = "completed"


_current: ContextVar[AuditDecision | None] = ContextVar("security_audit", default=None)


@contextmanager
def capture_decision() -> Iterator[AuditDecision]:
    decision = AuditDecision()
    token = _current.set(decision)
    try:
        yield decision
    finally:
        _current.reset(token)


def record_policy_decision(operation: str, outcome: str) -> None:
    decision = _current.get()
    if decision is not None and not decision.denied:
        decision.operation = operation
        decision.denied = outcome != "allow"
        decision.reason = "policy_allow" if outcome == "allow" else outcome
