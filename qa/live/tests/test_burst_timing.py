"""Burst cadence and delivery timestamps must not depend on late polling."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from qa.live.config import Pricing
from qa.live.cost import Ledger
from qa.live.discord import DiscordBackend
from qa.live.evaluate import evaluate
from qa.live.runner import Executor
from qa.live.schema import Assertion, Scenario, Step
from qa.live.tests.conftest import FakeBackend, FakeJudge
from qa.live.tests.test_discord import FakeDriver
from qa.live.types import Turn, utcnow


def snowflake(at: datetime) -> str:
    return str((int(at.timestamp() * 1000) - 1420070400000) << 22)


def test_burst_posts_on_schedule_and_watches_before_next_trigger(
    judge: FakeJudge, ledger: Ledger, pricing: Pricing, scenario: Scenario
) -> None:
    class BurstBackend(FakeBackend):
        def __init__(self) -> None:
            super().__init__()
            self.posts: list[float] = []
            self.watching: list[int] = []
            self.complete = threading.Event()
            self.probes = 0

        def verify_model(self, channel: str) -> None:
            self.probes += 1
            time.sleep(0.15)

        def send(
            self, channel: str, step: Step, *, mention: bool, reply_message_id: str | None = None
        ) -> str:
            if self.sent:
                assert self.sent in self.watching
            self.posts.append(time.monotonic())
            identity = super().send(channel, step, mention=mention)
            if self.sent == 3:
                self.complete.set()
            return identity

        def collect(self, turn: Turn, timeout: float) -> None:
            self.watching.append(turn.number)
            assert self.complete.wait(1), "a watcher blocked the next burst post"
            super().collect(turn, timeout)

        def delete_channel(self, channel: str) -> None:
            assert self.watching == [1, 2, 3]
            super().delete_channel(channel)

    backend = BurstBackend()
    scenario.tier = "full"
    scenario.est_turns = 3
    scenario.steps = [
        Step(do="burst", texts=["B1", "B2", "B3"], interval_s=0.02),
        Step(do="wait_done", timeout_s=1),
    ]
    scenario.assertions = [Assertion(kind="no_silent_drop", turn=i, max=20) for i in (1, 2, 3)]
    result = Executor(backend, judge, ledger, pricing, "staging").run(scenario)
    assert result.status == "PASS"
    assert backend.probes == 1
    assert all(0.005 < b - a < 0.1 for a, b in zip(backend.posts, backend.posts[1:], strict=False))
    assert backend.events[-1] == "delete"


@pytest.mark.parametrize(
    "reused,expected,source", [(False, 3.2, "message_created"), (True, 4.9, "card_edited")]
)
def test_late_poll_uses_message_creation_or_reused_card_edit(
    reused: bool,
    expected: float,
    source: str,
    monkeypatch: pytest.MonkeyPatch,
    pricing: Pricing,
    judge: FakeJudge,
) -> None:
    from qa.live.config import Config, Target

    origin = datetime(2026, 10, 10, 8, 0, tzinfo=UTC)
    driver = FakeDriver()
    config = Config(
        pricing=pricing, staging=Target(enabled=True), settle_s=0.001, poll_interval_s=0.001
    )
    backend = DiscordBackend(config, "staging", driver=driver)
    backend.owned.add("parent")
    created = origin + timedelta(seconds=-20 if reused else 3.2)
    driver.current = [
        {
            "id": snowflake(created),
            "channel_id": "thread",
            "content": "B1",
            "edited_timestamp": (origin + timedelta(seconds=4.9)).isoformat(),
        }
    ]
    # Polling first sees the result at +47s; a reaction observation cannot
    # erase the earlier authoritative card creation/edit evidence.
    driver.trigger["reactions"] = [{"emoji": {"name": "👀"}}]
    monkeypatch.setattr("qa.live.discord.utcnow", lambda: origin + timedelta(seconds=47))
    turn = Turn(1, snowflake(origin), "parent", origin - timedelta(seconds=1))
    backend.collect(turn, 0.1)
    assert turn.first_visible_s == pytest.approx(expected)
    assert turn.first_visible_evidence["source"] == source
    for kind in ("reply_within_s", "no_silent_drop"):
        assert (
            evaluate(Assertion(kind=kind, turn=1, max=20), [turn], backend, judge).status == "PASS"
        )


def test_anchored_answer_uses_content_and_multiline_without_footer_join(
    backend: FakeBackend, judge: FakeJudge
) -> None:
    turn = Turn(1, "1", "parent", utcnow())
    turn.messages = [
        {"id": "1", "content": "**B1**.", "embeds": [{"footer": {"text": "qa-agent · $0.01 used"}}]}
    ]
    assertion = Assertion(kind="text_present", turn=1, pattern=r"^\s*\**B1\**\.?\s*$")
    assert evaluate(assertion, [turn], backend, judge).status == "PASS"
    turn.messages[0]["content"] = "preface\nB1\nother line"
    assert evaluate(assertion, [turn], backend, judge).status == "PASS"
    turn.messages = [{"content": "B"}, {"content": "1"}]
    assertion.pattern = r"B\s+1"
    assert evaluate(assertion, [turn], backend, judge).status == "FAIL"
    turn.messages = [
        {"content": "", "embeds": [{"footer": {"text": "not sure what B1 refers to"}}]}
    ]
    assertion.kind = "text_absent"
    assertion.pattern = "(?i)refers to"
    assert evaluate(assertion, [turn], backend, judge).status == "FAIL"


def test_every_failed_burst_watcher_retains_its_traceback(
    backend: FakeBackend, judge: FakeJudge, ledger: Ledger, pricing: Pricing, scenario: Scenario
) -> None:
    backend.error = ValueError("offline harness bug")
    scenario.tier = "full"
    scenario.est_turns = 3
    scenario.steps = [Step(do="burst", texts=["B1", "B2", "B3"]), Step(do="wait_done")]
    result = Executor(backend, judge, ledger, pricing, "staging").run(scenario)
    assert result.status == "PENDING"
    assert len(result.errors) == 3
    assert all(e["type"] == "ValueError" and e["frames"] for e in result.errors)
    assert backend.events[-1] == "delete"
