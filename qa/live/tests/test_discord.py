from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import pytest
from pydantic import JsonValue

from qa.live.config import Config, Pricing
from qa.live.discord import DiscordBackend, fingerprint, load_driver
from qa.live.schema import Assertion, Step
from qa.live.types import Message, Pending, Turn, utcnow


class FakeDriver:
    TERMINAL = frozenset({"answered", "error", "over_cap"})

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.current: list[Message] = []
        self.baseline: list[Message] = []
        self.role = "user"
        self.permissions = ~0
        self.category_guild = "1435062989119295640"
        self.trigger: Message = {"id": "trigger", "reactions": []}
        self.qa_id = "1533049261032341668"

    def set_role(self, role: str) -> None:
        self.role = role

    def call(self, method: str, path: str, *, body: Message | None = None) -> JsonValue:
        self.calls.append((method, path))
        if path == "/users/@me":
            return {"id": self.qa_id}
        if path == "/channels/1435062989119295641":
            return {"guild_id": self.category_guild, "type": 4}
        if path.startswith("/guilds/") and method == "POST":
            assert body and body["parent_id"] == "1435062989119295641"
            return {"id": "parent"}
        if path.endswith("/messages") and method == "POST":
            return {"id": "123"}
        if "/reactions/" in path and method == "GET":
            return [{"id": "1530628070405308456"}]
        if "/messages/" in path and method == "GET":
            return self.trigger
        return {}

    def messages(self, channel_id: str, *, after: str | None, limit: int = 50) -> list[Message]:
        return self.baseline

    def turn_messages(self, channel_id: str, *, after: str, daimon_id: str) -> list[Message]:
        return self.current

    def exists(self, channel: str) -> bool:
        return False

    def classify(self, message: Message) -> str:
        return "working" if message.get("working") else "answered"

    def cmd_say(self, args: argparse.Namespace) -> None:
        assert args.file
        print("123")

    def cmd_warm(self, args: argparse.Namespace) -> None:
        self.calls.append(("WARM", ""))

    def cmd_access(self, args: argparse.Namespace) -> None:
        self.calls.append(("ACCESS", args.guild))

    def cmd_selftest(self, args: argparse.Namespace) -> None:
        self.calls.append(("SELFTEST", ""))

    def effective_permissions(
        self, *, guild: Message, overwrites: list[Message], user_id: str, api_guild_id: str
    ) -> int:
        return self.permissions


@pytest.fixture
def driver() -> FakeDriver:
    return FakeDriver()


@pytest.fixture
def backend(pricing: Pricing, driver: FakeDriver) -> DiscordBackend:
    config = Config(pricing=pricing, poll_interval_s=0.001, settle_s=0.001)
    return DiscordBackend(config, "staging", driver=driver)


def test_driver_bridge_reuses_functions_without_calls(tmp_path: Path) -> None:
    path = tmp_path / "qa.py"
    path.write_text("""
_role = 'user'
TERMINAL = frozenset({'answered'})
def _call(method, path, *, body=None): return {'role': _role}
def _messages(channel_id, *, after, limit=50): return []
def _exists(channel_id): return False
def _effective_permissions(**kwargs): return 123
def classify(message): return 'answered'
""")
    driver = load_driver(path)
    driver.set_role("admin")
    assert driver.call("GET", "unused") == {"role": "admin"}
    assert driver.classify({}) == "answered"


def test_preflight_permissions_identity_and_category(
    backend: DiscordBackend, driver: FakeDriver
) -> None:
    backend.preflight({"user"})
    assert driver.calls[-1] == ("WARM", "")
    driver.permissions = 0
    with pytest.raises(ValueError, match="permissions"):
        backend.preflight({"user"})
    driver.permissions = ~0
    driver.qa_id = "not-allowed"
    with pytest.raises(ValueError, match="identity"):
        backend.preflight({"user"})
    driver.category_guild = "customer"
    with pytest.raises(ValueError, match="category"):
        backend.preflight({"user"})


def test_only_owned_channel_writes_and_upload(backend: DiscordBackend, driver: FakeDriver) -> None:
    for action in [
        lambda: backend.delete_channel("customer"),
        lambda: backend.send("customer", Step(do="mention", text="x"), mention=True),
        lambda: backend.react("customer", "1", "👍"),
    ]:
        with pytest.raises(ValueError, match="outside"):
            action()
    assert not driver.calls
    channel = backend.create_channel("qa-test")
    assert (
        backend.send(channel, Step(do="mention", text="x", file="fake.csv"), mention=True) == "123"
    )
    backend.react(channel, "123", "👍")
    backend.delete_channel(channel)
    assert not backend.owned


def test_collection_excludes_previous_turn_and_settles(
    backend: DiscordBackend, driver: FakeDriver
) -> None:
    backend.create_channel("qa-test")
    old: Message = {"id": "old", "channel_id": "thread", "content": "previous"}
    driver.baseline = [old]
    backend.threads.add("thread")
    trigger = backend.send("thread", Step(do="thread_reply", text="new"), mention=False)
    fresh: Message = {"id": "new", "channel_id": "thread", "content": "APPLE"}
    driver.current = [old, fresh]
    turn = Turn(2, trigger, "thread", utcnow())
    backend.collect(turn, 0.1)
    assert turn.messages == [fresh]
    assert turn.thread_id == "thread"
    assert turn.ended_at and turn.done_s is not None
    assert not turn.parent_messages
    assert fingerprint(old) != fingerprint(fresh)


def test_queue_reaction_is_visible_without_terminal(
    backend: DiscordBackend, driver: FakeDriver
) -> None:
    backend.create_channel("qa-test")
    driver.trigger["reactions"] = [{"emoji": {"name": "⌛"}}]
    turn = Turn(1, "123", "parent", utcnow())
    with pytest.raises(Pending, match="timed out"):
        backend.collect(turn, 0.01)
    assert turn.first_visible_s is not None
    assert turn.done_s is None
    assert turn.trigger_reactions == driver.trigger["reactions"]


def test_collection_trusts_only_webhooks_owned_by_daimon_application(
    monkeypatch: pytest.MonkeyPatch, backend: DiscordBackend, driver: FakeDriver
) -> None:
    backend.create_channel("qa-test")
    answer: Message = {
        "id": "answer",
        "channel_id": "thread",
        "content": "APPLE",
        "webhook_id": "webhook",
        "author": {"id": "webhook-author"},
        "application_id": backend.target.daimon_id,
    }
    driver.baseline = [answer]
    queried: list[str] = []

    def selected(channel_id: str, *, after: str, daimon_id: str) -> list[Message]:
        queried.append(daimon_id)
        return [answer] if daimon_id == "webhook-author" else []

    monkeypatch.setattr(driver, "turn_messages", selected)
    turn = Turn(1, "123", "parent", utcnow())
    backend.collect(turn, 0.1)
    assert turn.messages == [answer]
    assert "webhook-author" in queried
    answer["application_id"] = "unrelated-application"
    queried.clear()
    with pytest.raises(Pending):
        backend.collect(Turn(2, "456", "parent", utcnow()), 0.01)
    assert "webhook-author" not in queried


def test_reply_reference_and_mention_are_sent_to_owned_thread(
    monkeypatch: pytest.MonkeyPatch, backend: DiscordBackend, driver: FakeDriver
) -> None:
    backend.create_channel("qa-test")
    backend.threads.add("thread")
    payloads: list[Message] = []
    original = driver.call

    def capture(method: str, path: str, *, body: Message | None = None) -> JsonValue:
        if method == "POST" and path.endswith("/messages"):
            assert body
            payloads.append(body)
        return original(method, path, body=body)

    monkeypatch.setattr(driver, "call", capture)
    backend.send(
        "thread",
        Step(do="thread_reply", text="follow up"),
        mention=True,
        reply_message_id="prior-chunk",
    )
    assert payloads == [
        {
            "content": f"<@{backend.target.daimon_id}> follow up",
            "message_reference": {"message_id": "prior-chunk", "channel_id": "thread"},
        }
    ]


def test_working_card_cannot_settle(backend: DiscordBackend, driver: FakeDriver) -> None:
    backend.create_channel("qa-test")
    driver.current = [{"id": "x", "channel_id": "thread", "working": True}]
    turn = Turn(1, "123", "parent", utcnow())
    with pytest.raises(Pending):
        backend.collect(turn, 0.01)
    assert turn.done_s is None


def test_log_query_is_scoped_and_fields_filtered(
    monkeypatch: pytest.MonkeyPatch, backend: DiscordBackend
) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps(
                [
                    {"jsonPayload": {"event": "turn.message_missing", "delivered": False}},
                    {"jsonPayload": {"event": "turn.message_missing", "delivered": True}},
                ]
            ),
            "",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    turn = Turn(1, "123", "parent", utcnow(), ended_at=utcnow(), thread_id="thread")
    rows = backend.logs(
        Assertion(
            kind="log_present", turn=1, event="turn.message_missing", fields={"delivered": False}
        ),
        turn,
    )
    assert len(rows) == 1
    assert "--project=pymc-daimon-staging" in calls[0]
    assert "timestamp>=" in calls[0][3] and "timestamp<=" in calls[0][3]
    assert 'jsonPayload.thread_id="thread"' in calls[0][3]
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1))
    with pytest.raises(Pending):
        backend.logs(Assertion(kind="log_absent", turn=1, event="a"), turn)


def test_unconfigured_hooks_and_prod_admin_are_refused(backend: DiscordBackend) -> None:
    backend.create_channel("qa-test")
    with pytest.raises(Pending, match="hook"):
        backend.admin(Step(do="admin", tool="change"), "parent")
    backend.env = "prod"
    with pytest.raises(ValueError, match="production"):
        backend.admin(Step(do="restart_workers"), "parent")


def test_usage_footer_and_unavailable_db(
    backend: DiscordBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DAIMON_QA_STAGING_DATABASE_URL", raising=False)
    turn = Turn(
        1,
        "123",
        "parent",
        utcnow(),
        messages=[
            {"embeds": [{"footer": {"text": "daimon · 42s · 1.2k in / 340 out · $0.0123"}}]},
        ],
    )
    usage = backend.usage(turn)
    assert usage.usd == 0.0123 and usage.input_tokens == 1200 and usage.output_tokens == 340
    with pytest.raises(Pending, match="database unavailable"):
        backend.db_check("select 1")


def test_request_correlated_log_absence_is_required(
    monkeypatch: pytest.MonkeyPatch, backend: DiscordBackend
) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        records = (
            [{"jsonPayload": {"event": "turn.completed", "rid": "qa-rid"}}]
            if len(calls) % 2
            else []
        )
        return subprocess.CompletedProcess(command, 0, json.dumps(records), "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    turn = Turn(1, "123", "parent", utcnow(), ended_at=utcnow(), thread_id="thread")
    assertion = Assertion(kind="log_absent", turn=1, event="session_preparation.replaced")
    assert backend.logs(assertion, turn) == []
    assert 'jsonPayload.rid="qa-rid"' in calls[1][3]
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "[]"))
    with pytest.raises(Pending, match="correlated"):
        backend.logs(assertion, turn)


def test_readonly_queries_bind_context_and_extract_scalar(
    backend: DiscordBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, dict[str, object] | None]] = []

    async def query(sql: str, params: dict[str, object] | None = None) -> JsonValue:
        calls.append((sql, params))
        return [{"id": "qa-tenant"}] if "FROM tenants" in sql else [{"count": 0}]

    monkeypatch.setattr(backend, "_query", query)
    backend.parent = "parent"
    turn = Turn(1, "trigger", "parent", utcnow(), thread_id="thread")
    result = backend.db_check("SELECT count(*) FROM x WHERE tenant_id = :tenant_id", turn)
    assert result == 0
    assert calls[-1][1] == {
        "tenant_id": "qa-tenant",
        "guild_id": "1435062989119295640",
        "channel_id": "parent",
        "thread_id": "thread",
        "trigger_message_id": "trigger",
    }


def test_measured_usage_records_real_token_and_model_evidence(
    backend: DiscordBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qa.live.schema import MODEL

    async def query(sql: str, params: dict[str, object] | None = None) -> JsonValue:
        assert params and params["thread"] == "thread"
        return [
            {
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_read_input_tokens": 300,
                "cache_creation_input_tokens": 0,
                "cost_usd": "0.00023",
                "model_ids": [MODEL],
            }
        ]

    monkeypatch.setattr(backend, "_query", query)
    turn = Turn(1, "123", "parent", utcnow(), ended_at=utcnow(), thread_id="thread")
    usage = backend.usage(turn)
    assert usage.input_tokens == 100 and usage.cache_read_input_tokens == 300
    assert usage.usd == 0.00023 and usage.models == [MODEL]
