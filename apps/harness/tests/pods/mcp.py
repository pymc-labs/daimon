"""Offline HTTPS MCP fixture: exact bearer required, deliberate credential echo."""

import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

TOKEN = "Bearer synthetic-forwarding-check"
mcp = FastMCP("authenticated-smoke", stateless_http=True)


@mcp.tool()
def whoami() -> str:
    return "DAIMON_MCP_TOOL_OK " + TOKEN


class ExactBearer(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.headers.get("authorization") != TOKEN:
            return Response(status_code=401)
        response = await call_next(request)
        # Receipt contains status/path only. Never log the request headers.
        print(
            f"authenticated_mcp path={request.url.path} status={response.status_code}", flush=True
        )
        return response


app = mcp.streamable_http_app()
app.add_middleware(ExactBearer)
uvicorn.run(app, host="0.0.0.0", port=8765, ssl_certfile="/tls/tls.crt", ssl_keyfile="/tls/tls.key")
