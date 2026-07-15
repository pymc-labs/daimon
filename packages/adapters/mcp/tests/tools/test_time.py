from __future__ import annotations

import datetime as dt

import pytest
from daimon.adapters.mcp.tools.time import _convert_impl, _now_impl
from fastmcp.exceptions import ToolError

pytestmark = pytest.mark.asyncio


async def test_now_impl_returns_iso_with_offset_for_iana_tz() -> None:
    out = await _now_impl("America/Los_Angeles")
    parsed = dt.datetime.fromisoformat(out)
    assert parsed.tzinfo is not None, (
        "now should return an offset-aware ISO-8601 string for an IANA tz"
    )


async def test_now_impl_default_tz_is_utc() -> None:
    out = await _now_impl("UTC")
    assert out.endswith("+00:00"), "now('UTC') should end with +00:00 offset"


async def test_now_impl_raises_tool_error_on_unknown_tz() -> None:
    with pytest.raises(ToolError, match="unknown timezone"):
        await _now_impl("foo/bar")


async def test_convert_impl_naive_input_interpreted_as_from_tz() -> None:
    out = await _convert_impl("2026-05-08T12:00:00", "America/Los_Angeles", "UTC")
    assert out == "2026-05-08T19:00:00+00:00", (
        "naive LA wall time 12:00 on 2026-05-08 (mid-DST, UTC-7) should convert to 19:00 UTC"
    )


async def test_convert_impl_aware_input_with_matching_offset_succeeds() -> None:
    out = await _convert_impl("2026-05-08T12:00:00-07:00", "America/Los_Angeles", "UTC")
    assert out == "2026-05-08T19:00:00+00:00", (
        "aware -07:00 input agreeing with LA DST should convert to 19:00 UTC"
    )


async def test_convert_impl_aware_input_with_mismatched_offset_raises() -> None:
    with pytest.raises(ToolError, match="disagrees with from_tz"):
        await _convert_impl("2026-05-08T12:00:00+00:00", "America/Los_Angeles", "UTC")


async def test_convert_impl_invalid_iso_raises() -> None:
    with pytest.raises(ToolError, match="invalid ISO-8601"):
        await _convert_impl("not a date", "UTC", "UTC")


async def test_convert_impl_unknown_tz_raises() -> None:
    with pytest.raises(ToolError, match="unknown timezone"):
        await _convert_impl("2026-05-08T12:00:00", "foo/bar", "UTC")
