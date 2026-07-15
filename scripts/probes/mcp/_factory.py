"""Minimal ASGI factory for the uvicorn --factory probe."""

from __future__ import annotations

from fastmcp import Context, FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse


class BearerMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.url.path.startswith("/mcp"):
            auth = request.headers.get("authorization", "")
            if not auth.startswith("Bearer probe-token-"):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            request.scope["auth_sub"] = auth.removeprefix("Bearer probe-token-")
        return await call_next(request)


def create_app():
    mcp = FastMCP("probe-factory")

    @mcp.tool
    async def whoami(ctx: Context) -> dict:
        scope = ctx.request_context.request.scope if ctx.request_context.request else {}
        return {"sub": scope.get("auth_sub")}

    app = mcp.http_app(path="/mcp")
    app.add_middleware(BearerMiddleware)

    # /healthz as a starlette route
    from starlette.routing import Route

    async def healthz(request):
        return JSONResponse({"ok": True})

    app.router.routes.append(Route("/healthz", healthz, methods=["GET"]))
    return app
