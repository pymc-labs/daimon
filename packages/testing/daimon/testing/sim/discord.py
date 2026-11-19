"""Discord's REST API, simulated for hosts with no gateway: who is in one guild."""

from __future__ import annotations

import re

import httpx
from daimon.testing.sim.network import StrictFake

HOST = "discord.com"


class DiscordAPI(StrictFake):
    """One guild that `owner` owns: everyone asked about is a member with no roles."""

    def __init__(self, *, guild: int, owner: int) -> None:
        super().__init__(host=HOST)
        self.guild, self.owner = str(guild), str(owner)
        base = f"/api/v10/guilds/{self.guild}"
        self.route("GET", f"{base}/members/([0-9]+)", self._member)
        self.route("GET", base, lambda r, m: self._json({"id": self.guild, "owner_id": self.owner}))
        everyone = {"id": self.guild, "permissions": "0"}
        self.route("GET", f"{base}/roles", lambda r, m: self._json([everyone]))

    def refuse(self, status: int, message: str, param: str | None = None) -> httpx.Response:
        return httpx.Response(status, json={"message": message, "code": 0})

    @staticmethod
    def _json(body: object) -> httpx.Response:
        return httpx.Response(200, json=body)

    def _member(self, _: httpx.Request, match: re.Match[str]) -> httpx.Response:
        return self._json({"user": {"id": match[1]}, "roles": []})
