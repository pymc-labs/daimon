"""`/avatars/<token>/<hash>.png`: the faces the app shows, served from its own origin.

The page's image policy allows only its own origin, so faces cannot come from the model
server's host. The token and hash in the path are the capability, as on that server.
"""

from __future__ import annotations

from typing import Annotated

from daimon.adapters.web.runtime import WebRuntime
from daimon.core.stores.agent_avatars import get_avatar_by_token
from fastapi import APIRouter, Query, Response

_SIZES = {128, 512}


def avatars_router(runtime: WebRuntime) -> APIRouter:
    router = APIRouter()

    @router.get("/avatars/{token}/{sha12}.png", include_in_schema=False)
    async def avatar(  # pyright: ignore[reportUnusedFunction]
        token: str, sha12: str, size: Annotated[int | None, Query()] = None
    ) -> Response:
        if size is not None and size not in _SIZES:
            return Response(status_code=400)
        async with runtime.sessionmaker() as db:
            png = await get_avatar_by_token(db, token=token, sha12=sha12, size=size)
        if png is None:
            return Response(status_code=404)
        return Response(
            png,
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    return router
