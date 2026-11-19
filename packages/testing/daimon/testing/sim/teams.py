"""The Bot Framework connector, simulated: posts and edits in one Entra tenant's Teams."""

from __future__ import annotations

import json
import re
from typing import Any, cast

import httpx
from daimon.testing.sim.network import StrictFake

HOST = "smba.trafficmanager.net"
_CARD = "application/vnd.microsoft.card.adaptive"
_CONVERSATION = r"/[^/]+/v3/conversations/([^/]+)"


def _card_words(card: Any) -> list[str]:  # noqa: ANN401  # Adaptive Card JSON
    """Every text and button title on an Adaptive Card, in order."""
    if isinstance(card, list):
        return [w for c in cast("list[Any]", card) for w in _card_words(c)]
    if not isinstance(card, dict):
        return []
    found: list[str] = []
    for key, value in cast("dict[str, Any]", card).items():
        if key in ("text", "title") and isinstance(value, str):
            found.append(value)
        elif key != "data":
            found.extend(_card_words(value))
    return found


class BotFrameworkAPI(StrictFake):
    """Teams for one Entra `tenant`: every person asked about is a member of it."""

    def __init__(self, *, tenant: str) -> None:
        super().__init__(host=HOST)
        self.tenant = tenant
        #: Each post and edit as (activity id, activity), in order.
        self.activities: list[tuple[str, dict[str, Any]]] = []
        self.route("POST", f"{_CONVERSATION}/activities", self._post)
        self.route("POST", f"{_CONVERSATION}/activities/([^/]+)", self._post)
        self.route("PUT", f"{_CONVERSATION}/activities/([^/]+)", self._edit)
        self.route("GET", f"{_CONVERSATION}/members/([^/]+)", self._member)
        # A team without its Entra group: Graph reads (history, files) are skipped.
        self.route("GET", r"/[^/]+/v3/teams/([^/]+)", self._team)

    def _team(self, _: httpx.Request, match: re.Match[str]) -> httpx.Response:
        return httpx.Response(200, json={"id": match[1], "name": "Sim"})

    def refuse(self, status: int, message: str, param: str | None = None) -> httpx.Response:
        return httpx.Response(status, json={"error": {"code": "BadArgument", "message": message}})

    def _post(self, request: httpx.Request, _: re.Match[str]) -> httpx.Response:
        activity_id = f"m-{len(self.activities) + 1}"
        self.activities.append((activity_id, json.loads(request.content)))
        return httpx.Response(201, json={"id": activity_id})

    def _edit(self, request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        self.activities.append((match[2], json.loads(request.content)))
        return httpx.Response(200, json={"id": match[2]})

    def _member(self, _: httpx.Request, match: re.Match[str]) -> httpx.Response:
        member = {"id": "29:member", "aadObjectId": match[2], "tenantId": self.tenant}
        return httpx.Response(200, json=member | {"userRole": "user"})

    def texts(self, start: int = 0) -> list[str]:
        """What the person sees, activity by activity: the text, else the cards' words."""
        shown: list[str] = []
        for _, activity in self.activities[start:]:
            attached = cast("list[dict[str, Any]]", activity.get("attachments") or [])
            cards = [a["content"] for a in attached if a.get("contentType") == _CARD]
            words = [activity.get("text") or "", *_card_words(cards)]
            shown.append("\n".join(w for w in words if w))
        return shown
