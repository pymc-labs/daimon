"""Serves the built single-page app: files by path, `index.html` for any other page."""

from __future__ import annotations

from pathlib import Path

from daimon.core.config import WebSettings
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response

# Where the image build puts the built app.
PACKAGE_STATIC_DIR = Path(__file__).parent / "static"
_API_ROOTS = frozenset({"api", "auth"})
_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]
_IMMUTABLE = "public, max-age=31536000, immutable"


def static_dir(web: WebSettings) -> Path:
    return web.static_dir or PACKAGE_STATIC_DIR


def _file_inside(root: Path, relative: str) -> Path | None:
    """The file at `relative` under `root`, or None when absent or outside it."""
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root.resolve()) or not candidate.is_file():
        return None
    return candidate


def spa_router(web: WebSettings) -> APIRouter:
    """A catch-all for pages. Register it after every other route."""
    root = static_dir(web)
    router = APIRouter()

    @router.api_route("/{path:path}", methods=_METHODS, include_in_schema=False)
    async def page(path: str, request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        # Only reads get pages; anything else, and any unknown api or auth path, is a 404.
        if request.method not in ("GET", "HEAD") or path.split("/", 1)[0] in _API_ROOTS:
            raise HTTPException(status_code=404, detail="Not found")
        file = _file_inside(root, path) if path else None
        if file is not None:
            # Built assets carry a content hash in their name, so they never change.
            cache = _IMMUTABLE if path.startswith("assets/") else "no-cache"
            return FileResponse(file, headers={"Cache-Control": cache})
        index = _file_inside(root, "index.html")
        if index is None:
            raise HTTPException(status_code=404, detail="Not found")
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    return router
