"""The read limit and the response limit are one rule: every allowed read fits.

`fly_read_file` once allowed a 4 MiB chunk while the transport refused any response
over 8 MiB. A chunk travels twice in a response, so the largest read never fitted.
"""

from __future__ import annotations

import httpx
import pytest
from daimon.core.compute_provider import ComputeProviderError
from daimon.core.compute_thin_mcp import (
    MAX_READ_BYTES,
    MAX_RESPONSE_BYTES,
    READ_BYTE_COST,
    call_thin_mcp,
)
from fastmcp import FastMCP

# The JSON-RPC wrapper, the other result fields and the longest path, twice over.
_ENVELOPE_BYTES = 64 * 1024


def test_the_largest_read_fits_the_response_limit_in_its_worst_case():
    assert MAX_READ_BYTES * READ_BYTE_COST + _ENVELOPE_BYTES <= MAX_RESPONSE_BYTES


async def _read(content: str) -> object:
    thin = FastMCP("thin-test")

    @thin.tool
    async def read_file(path: str, offset: int, limit: int):
        return {"path": path, "content": content, "bytes": len(content), "eof": False}

    app = thin.http_app(path="/mcp/", stateless_http=True, json_response=True)
    async with app.router.lifespan_context(app):
        try:
            return await call_thin_mcp(
                "https://session.test/mcp/",
                "per-session-token",
                "read_file",
                {"path": "/" + "p" * 4095, "offset": 0, "limit": MAX_READ_BYTES},
                transport=httpx.ASGITransport(app=app),
            )
        except ComputeProviderError as error:
            # Leave the server's task group first: it would wrap the error in a group.
            refusal = error
    raise refusal


async def test_the_costliest_chunk_of_the_largest_read_is_carried():
    # A control byte is the most expensive byte to carry: 13 response bytes.
    worst = "\x01" * MAX_READ_BYTES
    assert await _read(worst) == {
        "path": "/" + "p" * 4095,
        "content": worst,
        "bytes": MAX_READ_BYTES,
        "eof": False,
    }


@pytest.mark.parametrize(
    ("byte", "cost"),
    # Plain text is carried twice; a control byte costs the most.
    [("a", 2), ("\x01", READ_BYTE_COST)],
)
async def test_a_chunk_whose_cost_reaches_the_response_limit_is_refused(byte, cost):
    with pytest.raises(ComputeProviderError, match="too large"):
        await _read(byte * (MAX_RESPONSE_BYTES // cost))
