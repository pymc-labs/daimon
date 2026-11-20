"""Any host or MCP server no other service plays: recorded exchanges and the simulator only."""

from __future__ import annotations

from daimon.core.evals.format import Effect, ExchangeResponse
from daimon.core.evals.world.model import HttpCall, ItemRef, WorldRequest
from daimon.core.evals.world.overlay import Overlay


class GenericService:
    name = "generic"

    def claims(self, item: ItemRef, call: HttpCall) -> bool:
        return True

    def effects(self, request: WorldRequest) -> tuple[Effect, ...]:
        return ()

    def answer(
        self, item: ItemRef, request: WorldRequest, overlay: Overlay
    ) -> ExchangeResponse | None:
        return None

    def remember(self, request: WorldRequest, response: ExchangeResponse, overlay: Overlay) -> None:
        return None

    def check(self, request: WorldRequest, response: ExchangeResponse) -> str | None:
        if request.surface == "http" and response.http is None:
            return "An HTTP request gets an HTTP answer."
        return None

    def guidance(self, item: ItemRef, request: WorldRequest) -> str:
        if request.surface == "http" and request.raw is not None:
            return f"The real service at {request.raw.host}; use its documented API shapes."
        return f"The MCP server {request.server}, tool {request.tool}; follow its schema."
