"""Best-effort, process-local operator alerts over a Discord webhook."""

from __future__ import annotations

import asyncio
from time import monotonic

import httpx
import structlog
from pydantic import SecretStr

log = structlog.get_logger(__name__)

_WINDOW_S = 600.0
_last_sent: dict[str, float] = {}
_pending: set[asyncio.Task[None]] = set()


def alert_ops(url: SecretStr | None, *, key: str, message: str) -> None:
    """Queue an alert without delaying or failing the caller."""
    if not isinstance(url, SecretStr):
        return
    now = monotonic()
    if now - _last_sent.get(key, float("-inf")) < _WINDOW_S:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(_post(url.get_secret_value(), " ".join(message.split())[:1900]))
    _pending.add(task)
    task.add_done_callback(_pending.discard)
    _last_sent[key] = now


async def _post(url: str, message: str) -> None:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(
                url,
                json={"content": message, "allowed_mentions": {"parse": []}},
            )
            response.raise_for_status()
    except Exception:
        log.warning("ops.alert_failed")
