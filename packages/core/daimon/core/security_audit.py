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
    scope: str | None = None
    """The operator-token scope the call was checked against."""


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


def record_scope_decision(scope: str, *, allowed: bool) -> None:
    """Record the operator-token scope a tool checked, denying when the token lacks it."""
    decision = _current.get()
    if decision is None:
        return
    decision.scope = scope
    if not allowed and not decision.denied:
        decision.denied = True
        decision.reason = "scope_missing"


def record_denial(reason: str) -> None:
    """Deny the current request for a reason no policy decision covers, e.g. a rate limit."""
    decision = _current.get()
    if decision is not None and not decision.denied:
        decision.denied = True
        decision.reason = reason
