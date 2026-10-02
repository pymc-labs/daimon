"""Offline checks for the event layout driver."""

from __future__ import annotations

# pyright: basic
import argparse
import json
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from scripts import hackathon_layout as layout


def test_csv_rejects_duplicate_slugs_and_bad_ids(tmp_path: Path) -> None:
    path = tmp_path / "teams.csv"
    path.write_text("team name,member Discord ids\nTeam A,123;456\nTeam-A,789\n")
    with pytest.raises(ValueError, match="duplicate team slug"):
        layout.read_teams(path)
    path.write_text("team name,member Discord ids\nTeam A,123;bad\n")
    with pytest.raises(ValueError, match="non-numeric"):
        layout.read_teams(path)


def test_setup_refuses_bot_without_role_management() -> None:
    class Guild:
        def request(self, method: str, path: str) -> object:
            if path.endswith("/roles"):
                return [{"id": layout.QA_GUILD, "permissions": str(layout.MANAGE_CHANNELS)}]
            return {"roles": []}

    with pytest.raises(RuntimeError, match="Manage Roles"):
        layout.require_setup_permissions(
            cast("layout.Discord", Guild()), layout.QA_GUILD, layout.QA_BOT_ID
        )


class FakeDiscord:
    def __init__(self) -> None:
        self.roles: list[dict] = []
        self.channels: list[dict] = []
        self.threads: list[dict] = []
        self.calls: list[tuple[str, str]] = []
        self.next_id = 100000000000000000

    def pace_thread(self) -> None:
        pass

    def request(self, method: str, path: str, **kwargs) -> object:
        self.calls.append((method, path))
        if method == "GET" and path.endswith("/roles"):
            return self.roles
        if method == "GET" and path.endswith("/channels"):
            return self.channels
        if method == "GET" and path.endswith("/threads/active"):
            return {"threads": self.threads}
        if method == "GET" and path == "/users/@me":
            return {"id": "999"}
        if method == "POST":
            self.next_id += 1
            item = {"id": str(self.next_id), **kwargs["json"]}
            if path.endswith("/roles"):
                self.roles.append(item)
            elif path.endswith("/channels"):
                self.channels.append(item)
            elif path.endswith("/threads"):
                item["parent_id"] = path.split("/")[2]
                self.threads.append(item)
            return item
        if method == "DELETE":
            return {}
        return {}


def test_setup_is_resumable_and_teardown_lifts_everything(tmp_path: Path, monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_cli(*args: str) -> str:
        calls.append(args)
        if args[:3] == ("channels", "isolation", "set"):
            return "123: isolated, agent team-a-copy"
        return "ok"

    monkeypatch.setattr(layout, "cli", fake_cli)
    args = argparse.Namespace(
        guild_id=layout.QA_GUILD,
        state=tmp_path / "state.json",
        run_id="reh3",
        fork_from="daimon",
        budget_usd=Decimal("25"),
        threads_per_team=3,
    )
    state = {"guild_id": args.guild_id, "run_id": args.run_id, "teams": {}}
    api = FakeDiscord()
    layout.setup_team(cast("layout.Discord", api), args, state, "Team A", ["123"])
    created = len([call for call in api.calls if call[0] == "POST"])
    layout.setup_team(
        cast("layout.Discord", api), args, json.loads(args.state.read_text()), "Team A", ["123"]
    )
    assert len([call for call in api.calls if call[0] == "POST"]) == created
    assert len([call for call in calls if call[:3] == ("channels", "isolation", "set")]) == 1
    layout.teardown_team(cast("layout.Discord", api), args, state, "team-a")
    assert state["teams"] == {}
    assert ("channels", "isolation", "lift", "discord", args.guild_id, state_id(api)) in calls
    assert any(call[:2] == ("agents", "--guild") and "archive" in call for call in calls)


def state_id(api: FakeDiscord) -> str:
    return str(api.channels[0]["id"])
