"""Discord's REST API, simulated: who is in one guild, and what the room-fact reader lists.

The gateway sim (`tests/simulated/discord.py`) fills `channels`, `threads` and `members`
from what it shows the bot, so the REST side and the gateway side agree."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import httpx
from daimon.testing.sim.network import StrictFake

HOST = "discord.com"


class DiscordAPI(StrictFake):
    """One guild that `owner` owns: everyone asked about is a member with no roles.

    The listing routes the room-fact reader uses (`audience_rest`) answer from
    `channels`, `threads` and `members` (user id to whether it is a bot); the owner is
    always a member."""

    def __init__(self, *, guild: int, owner: int) -> None:
        super().__init__(host=HOST)
        self.guild, self.owner = str(guild), str(owner)
        self.channels: list[dict[str, Any]] = []
        self.threads: Callable[[], list[dict[str, Any]]] = lambda: []
        self.members: dict[str, bool] = {}
        base = f"/api/v10/guilds/{self.guild}"
        self.route("GET", f"{base}/members/([0-9]+)", self._member)
        self.route("GET", f"{base}/members", self._members)
        self.route("GET", base, lambda r, m: self._json(self._guild()))
        self.route("GET", f"{base}/roles", lambda r, m: self._json(self._guild()["roles"]))
        self.route("GET", f"{base}/channels", lambda r, m: self._json(self.channels))
        self.route(
            "GET",
            f"{base}/threads/active",
            lambda r, m: self._json({"threads": self.threads(), "members": []}),
        )

    def refuse(self, status: int, message: str, param: str | None = None) -> httpx.Response:
        return httpx.Response(status, json={"message": message, "code": 0})

    @staticmethod
    def _json(body: object) -> httpx.Response:
        return httpx.Response(200, json=body)

    def _guild(self) -> dict[str, Any]:
        everyone = {"id": self.guild, "permissions": "0"}
        return {"id": self.guild, "owner_id": self.owner, "roles": [everyone]}

    def _member(self, _: httpx.Request, match: re.Match[str]) -> httpx.Response:
        return self._json({"user": {"id": match[1]}, "roles": []})

    def _members(self, request: httpx.Request, _: re.Match[str]) -> httpx.Response:
        listed = {self.owner: False, **self.members}
        after = int(request.url.params.get("after", "0"))
        limit = int(request.url.params.get("limit", "1"))
        ids = sorted(int(user_id) for user_id in listed if int(user_id) > after)[:limit]
        no_roles: list[str] = []
        page: list[dict[str, object]] = [
            {"user": {"id": str(i), "bot": listed[str(i)]}, "roles": no_roles} for i in ids
        ]
        return self._json(page)
