from datetime import timedelta

import pytest

from qa.live.config import Config, Pricing, Target
from qa.live.discord import DiscordBackend
from qa.live.evaluate import evaluate
from qa.live.schema import Assertion
from qa.live.tests.conftest import FakeBackend, FakeJudge
from qa.live.tests.test_burst_timing import snowflake
from qa.live.tests.test_discord import FakeDriver
from qa.live.types import Message, Pending, Turn, utcnow


def test_channel_text_since_turn(backend: FakeBackend, judge: FakeJudge) -> None:
    turn = Turn(1, "trigger", "parent", utcnow(), settled=True, ended_at=utcnow())
    backend.log_rows = [{"id": "1", "content": "-# qa-agent\n400"}]
    assert (
        evaluate(
            Assertion(kind="channel_text_present", since_turn=1, pattern="^400$"),
            [turn],
            backend,
            judge,
        ).status
        == "PASS"
    )
    assert (
        evaluate(
            Assertion(kind="channel_text_absent", since_turn=1, pattern="MEMORY-ERROR"),
            [turn],
            backend,
            judge,
        ).status
        == "PASS"
    )
    assert turn.channel_history == backend.log_rows


def test_channel_history_filters_user_and_earlier_content(pricing: Pricing) -> None:
    driver = FakeDriver()
    backend = DiscordBackend(
        Config(pricing=pricing, staging=Target(enabled=True)), "staging", driver=driver
    )
    backend.owned.add("parent")
    now = utcnow()
    turn = Turn(1, "trigger", "parent", now, ended_at=now, settled=True)
    driver.baseline = [
        {
            "id": snowflake(now - timedelta(seconds=1)),
            "content": "old",
            "author": {"id": backend.target.daimon_id},
        },
        {"id": snowflake(now + timedelta(seconds=1)), "content": "user", "author": {"id": "user"}},
        {
            "id": snowflake(now + timedelta(seconds=2)),
            "content": "routine",
            "application_id": backend.target.daimon_id,
        },
    ]
    assert [row["content"] for row in backend.channel_messages(turn)] == ["routine"]
    turn.settled = False
    with pytest.raises(Pending):
        backend.channel_messages(turn)
    turn.settled = True
    driver.baseline *= 34
    with pytest.raises(Pending):
        backend.channel_messages(turn)


def test_agent_header_evidence_is_per_turn_and_deduplicated(pricing: Pricing) -> None:
    driver = FakeDriver()
    config = Config(
        pricing=pricing,
        staging=Target(enabled=True, qa_agent_name="qa-agent"),
        settle_s=0.001,
        poll_interval_s=0.001,
    )
    backend = DiscordBackend(config, "staging", driver=driver)
    backend.owned.add("parent")
    driver.current = [{"id": "123", "channel_id": "thread", "content": "-# qa-agent\nB3"}]
    turn = Turn(3, "trigger", "parent", utcnow())
    backend.collect(turn, 1)
    assert turn.agent_subtext_headers == [{"message_id": "123", "line": "-# qa-agent"}]
    assert Turn(1, "other", "parent", utcnow()).agent_subtext_headers == []


def test_empty_channel_absence_is_pending(backend: FakeBackend, judge: FakeJudge) -> None:
    turn = Turn(1, "trigger", "parent", utcnow(), settled=True, ended_at=utcnow())
    check = evaluate(
        Assertion(kind="channel_text_absent", since_turn=1, pattern="bad"), [turn], backend, judge
    )
    assert check.status == "PENDING"


def test_channel_history_includes_threads_only_after_terminal_answer(pricing: Pricing) -> None:
    class Driver(FakeDriver):
        def messages(self, channel_id: str, *, after: str | None, limit: int = 50) -> list[Message]:
            return self.baseline if channel_id == "thread" else []

    driver = Driver()
    backend = DiscordBackend(
        Config(pricing=pricing, staging=Target(enabled=True)), "staging", driver=driver
    )
    backend.owned.add("parent")
    backend.threads.add("thread")
    now = utcnow()
    turn = Turn(
        1, snowflake(now), "parent", now, ended_at=now + timedelta(seconds=20), settled=True
    )
    driver.baseline = [
        {
            "id": snowflake(now + timedelta(seconds=25)),
            "content": "bad routine",
            "author": {"id": backend.target.daimon_id},
        }
    ]
    assert [row["content"] for row in backend.channel_messages(turn)] == ["bad routine"]
    driver.baseline.append(
        {
            "id": snowflake(now + timedelta(seconds=5)),
            "content": "early setup",
            "application_id": backend.target.daimon_id,
        }
    )
    assert [row["content"] for row in backend.channel_messages(turn)] == ["bad routine"]
    driver.baseline.pop()
    turn.messages = list(driver.baseline)
    assert backend.channel_messages(turn) == []
