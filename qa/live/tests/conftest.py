"""Fakes only: these fixtures never retrieve credentials or contact a deployment."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import JsonValue

from qa.live.config import Pricing
from qa.live.cost import Ledger
from qa.live.models import STAGING_LEGACY_MODEL
from qa.live.schema import Assertion, Scenario, Step
from qa.live.types import Message, Pending, Turn, Usage, utcnow


class FakeJudge:
    def __init__(self) -> None:
        self.usage: list[Usage] = []
        self.errors: list[Message] = []

    def evaluate(self, rubric: str, answer: str) -> tuple[bool, str]:
        return "APPLE" in answer, rubric


class FakeBackend:
    fallback_watch_s = 180.0

    def __init__(self) -> None:
        self.events: list[str] = []
        self.sent = 0
        self.error: BaseException | None = None
        self.log_rows: list[Message] = []
        self.log_error = False
        self.rows: JsonValue = [{"n": 1}]
        self.verdict = "answered"

    def context(self) -> dict[str, str]:
        return {"guild_id": "1435062989119295640"}

    def preflight(self, roles: set[str]) -> None:
        self.events.append("preflight")

    def create_channel(self, name: str) -> str:
        self.events.append("create")
        return "parent" if self.events.count("create") == 1 else "other-parent"

    def delete_channel(self, channel: str) -> None:
        self.events.append("delete")

    def verify_model(self, channel: str) -> None:
        pass

    def send(
        self, channel: str, step: Step, *, mention: bool, reply_message_id: str | None = None
    ) -> str:
        self.events.append(f"send:{channel}:{mention}")
        self.sent += 1
        return str(self.sent)

    def collect(self, turn: Turn, timeout: float) -> None:
        self.events.append("collect")
        if self.error:
            raise self.error
        turn.thread_id = "thread"
        turn.ended_at = utcnow()
        turn.settled = True
        turn.progress_seen_s = 0.5
        turn.first_visible_s = 1
        turn.done_s = 2
        turn.messages = [
            {
                "id": f"answer-{turn.number}",
                "channel_id": "thread",
                "content": "APPLE",
                "embeds": [{"footer": {"text": "daimon $0.02 used"}}],
                "reactions": [{"emoji": {"name": "👍"}}],
                "attachments": [{"filename": f"file-{turn.number}.csv", "size": 12}],
            }
        ]
        turn.verdicts = [self.verdict]

    def react(self, channel: str, message: str, emoji: str) -> None:
        self.events.append("react")

    def admin(self, step: Step, channel: str) -> None:
        self.events.append(f"admin:{step.tool or step.do}")

    def logs(self, assertion: Assertion, turn: Turn) -> list[Message]:
        if self.log_error:
            raise Pending("logging unavailable")
        return self.log_rows

    def db_check(self, sql: str, turn: Turn | None = None) -> JsonValue:
        return self.rows

    def usage(self, turn: Turn) -> Usage:
        return Usage(
            input_tokens=10,
            output_tokens=5,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            usd=0.02,
            source="turn_outcomes",
            models=[STAGING_LEGACY_MODEL],
        )

    def channel_messages(self, turn: Turn) -> list[Message]:
        return self.log_rows

    def thread_name(self, turn: Turn) -> str:
        return "Inventory example"

    def classify(self, message: Message) -> str:
        return "working" if message.get("working") else self.verdict


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def judge() -> FakeJudge:
    return FakeJudge()


@pytest.fixture
def pricing() -> Pricing:
    return Pricing(per_turn_usd=0.25, judge_input_per_million=1.0, judge_output_per_million=5.0)


@pytest.fixture
def ledger(tmp_path: Path) -> Ledger:
    return Ledger(tmp_path / "cost" / "ledger.jsonl")


@pytest.fixture
def scenario() -> Scenario:
    return Scenario.model_validate(
        {
            "id": "QA-D1-FOLLOWUP-REUSE",
            "title": "follow-up reuse",
            "friction": ["D1"],
            "sources": [],
            "set": "A",
            "surface": "discord",
            "tier": "canary",
            "priority": "P0",
            "est_turns": 2,
            "steps": [
                {"do": "mention", "text": "APPLE"},
                {"do": "wait_done"},
                {"do": "thread_reply", "text": "repeat"},
                {"do": "wait_done"},
            ],
            "assert": [{"kind": "text_present", "turn": 2, "pattern": "APPLE"}],
        }
    )
