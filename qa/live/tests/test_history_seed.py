from qa.live.config import Pricing
from qa.live.cost import Ledger
from qa.live.runner import Executor
from qa.live.schema import Assertion, Scenario, Step
from qa.live.tests.conftest import FakeBackend, FakeJudge


def test_channel_seed_is_not_watched_or_billed(
    backend: FakeBackend, judge: FakeJudge, ledger: Ledger, pricing: Pricing, scenario: Scenario
) -> None:
    scenario.steps = [
        Step(do="channel_message", text="context"),
        Step(do="mention", text="APPLE"),
        Step(do="wait_done"),
    ]
    scenario.assertions = [Assertion(kind="text_present", turn=1, pattern="APPLE")]
    r = Executor(backend, judge, ledger, pricing, "staging").run(scenario)
    assert r.status == "PASS"
    assert r.seed_messages == [{"message_id": "1", "channel_id": "parent"}]
    assert len(r.turns) == 1 and r.turns[0].trigger_id == "2"
    assert backend.events.count("collect") == 1


def test_seed_only_no_phantom_charge(
    backend: FakeBackend, judge: FakeJudge, ledger: Ledger, pricing: Pricing, scenario: Scenario
) -> None:
    scenario.tier = "full"
    scenario.steps = [Step(do="channel_message", text="context")]
    scenario.assertions = []
    r = Executor(backend, judge, ledger, pricing, "staging").run(scenario)
    assert r.turns == [] and len(r.seed_messages) == 1
    assert ledger._today() == 0
