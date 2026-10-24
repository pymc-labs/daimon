"""Shared no-I/O admission guard for explicit offline Discord observers."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import httpx
from daimon.testing.db import require_owned_sessionmaker

if TYPE_CHECKING:
    from daimon.adapters.discord.runtime import DiscordRuntime
    from mux.testing.drivers.anthropic.ma_transport import ScriptedTransport


def require_discord_runtime_capture(runtime: DiscordRuntime, native: ScriptedTransport) -> None:
    """Validate current outer and nested dependencies before a real gateway event."""
    require_owned_sessionmaker(runtime.sessionmaker)
    require_owned_sessionmaker(runtime.turn_deps.sessionmaker)
    # This title/admission slice does not authorize opaque native runtimes.
    # None uses the captured native SDK and the already checked DB factory.
    deps = runtime.turn_deps
    runtimes: object = deps.turn_runtimes
    if (
        deps.turn_runtime_factory is not None
        or type(runtimes) is not dict
        or cast(dict[object, object], runtimes)
        or deps.backend is not None
        or deps.backend_session_ref is not None
        or deps.state_store is not None
    ):
        raise ValueError("offline ingress requires absent injected provider runtime dependencies")
    for sdk in (runtime.anthropic, runtime.turn_deps.anthropic, runtime.turn_deps.messages):
        if sdk is None:
            continue
        if sdk is not runtime.anthropic:
            raise ValueError("every native dependency must use the actual isolated SDK transport")
        client = sdk._client  # pyright: ignore[reportPrivateUsage]
        transport = client._transport  # pyright: ignore[reportPrivateUsage]
        if (
            not isinstance(transport, httpx.MockTransport)
            or transport.handler != native.dispatch
            or client._mounts  # pyright: ignore[reportPrivateUsage]
        ):
            raise ValueError("native capture must be the actual isolated SDK transport")
