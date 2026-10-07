"""Short-lived cache for Slack installs lacking chat:write.customize."""

from __future__ import annotations

import time

_TTL_SECONDS = 15 * 60
_NO_CUSTOMIZE_SCOPE: dict[str, float] = {}


def missing_customize_scope(token: str | None) -> bool:
    if not token:
        return False
    expires_at = _NO_CUSTOMIZE_SCOPE.get(token, 0)
    if expires_at <= time.monotonic():
        _NO_CUSTOMIZE_SCOPE.pop(token, None)
        return False
    return True


def remember_missing_customize_scope(token: str | None) -> None:
    if token:
        _NO_CUSTOMIZE_SCOPE[token] = time.monotonic() + _TTL_SECONDS


def clear_missing_customize_scope(token: str) -> None:
    _NO_CUSTOMIZE_SCOPE.pop(token, None)
