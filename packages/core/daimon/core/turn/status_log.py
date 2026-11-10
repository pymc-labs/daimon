"""The `turn.status_log` switch, read by the reducer and the answer extractors.

Off (the default), consecutive `agent.message` texts are joined into one block and
the answer is every text after the last tool call. On, each message is its own block
and the answer keeps every message set apart by a blank line. The working card keeps
the last few messages either way (`status_lines.roll_draft`).

A turn pins the value its place resolved (the `status_log` feature) for its whole run;
outside a turn it is the deployment's value (`DAIMON_FEATURES__STATUS_LOG`, else the
deprecated `DAIMON_TURN__STATUS_LOG`), read once per process.
`status_log_override` is what pins it.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache

_OVERRIDE: ContextVar[bool | None] = ContextVar("turn_status_log", default=None)


@lru_cache(maxsize=1)
def _configured() -> bool:
    from daimon.core.config import load_turn_settings
    from daimon.core.features.deployment import deployment_value
    from daimon.core.features.registry import FEATURES

    value = deployment_value(None, FEATURES["status_log"])
    if value is not None:
        return value.value == "on"
    return load_turn_settings().status_log


def status_log_enabled() -> bool:
    """Whether this turn keeps its messages apart."""
    override = _OVERRIDE.get()
    return override if override is not None else _configured()


@contextmanager
def status_log_override(enabled: bool) -> Iterator[None]:
    """Pin the switch inside the block, whatever the environment says."""
    token = _OVERRIDE.set(enabled)
    try:
        yield
    finally:
        _OVERRIDE.reset(token)
