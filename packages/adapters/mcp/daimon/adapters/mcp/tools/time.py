"""Time tools: ``now`` and ``convert``.

``register_time_tools(mcp, runtime)`` wires the ``@mcp.tool`` closures for
this group; each closure delegates to a module-private ``_*_impl`` function
that can be unit-tested without a FastMCP Context. The ``runtime`` parameter
is unused (time tools have no daimon-specific state) but preserved for
``register_*_tools(mcp, runtime)`` signature uniformity.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from daimon.adapters.mcp.runtime import McpRuntime
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as err:
        raise ToolError(f"unknown timezone: {name!r}") from err


async def _now_impl(tz: str) -> str:
    return dt.datetime.now(tz=_zone(tz)).isoformat()


async def _convert_impl(time: str, from_tz: str, to_tz: str) -> str:
    src = _zone(from_tz)
    dst = _zone(to_tz)
    try:
        parsed: dt.datetime = dt.datetime.fromisoformat(time)
    except ValueError as err:
        raise ToolError(f"invalid ISO-8601 time: {time!r}") from err

    attached: dt.datetime
    if parsed.tzinfo is None:
        attached = parsed.replace(tzinfo=src)
    else:
        expected_offset: dt.timedelta | None = src.utcoffset(parsed.replace(tzinfo=None))
        if parsed.utcoffset() != expected_offset:
            raise ToolError(f"input offset {parsed.utcoffset()} disagrees with from_tz={from_tz!r}")
        attached = parsed
    return attached.astimezone(dst).isoformat()


def register_time_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    del runtime

    @mcp.tool
    async def now(ctx: Context, tz: str = "UTC") -> str:  # pyright: ignore[reportUnusedFunction]
        """Return the current wall-clock time in the given IANA timezone as ISO-8601."""
        del ctx
        return await _now_impl(tz)

    @mcp.tool
    async def convert(ctx: Context, time: str, from_tz: str, to_tz: str) -> str:  # pyright: ignore[reportUnusedFunction]
        """Convert an ISO-8601 ``time`` from ``from_tz`` to ``to_tz``.

        Naive input is interpreted as ``from_tz``; aware input must agree
        with ``from_tz`` at that wall time.
        """
        del ctx
        return await _convert_impl(time, from_tz, to_tz)
