from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from qa.live.config import Pricing
from qa.live.context import Context
from qa.live.cost import Ledger
from qa.live.evaluate import evaluate
from qa.live.report import Result
from qa.live.runner import Executor
from qa.live.schema import Assertion, Scenario, Step, load_catalog
from qa.live.tests.conftest import FakeBackend, FakeJudge
from qa.live.types import Pending, Turn, utcnow


def test_catalog_canary_with_pdf_and_mentioned_followup(
    tmp_path: Path,
    backend: FakeBackend,
    judge: FakeJudge,
    ledger: Ledger,
    pricing: Pricing,
    scenario: Scenario,
) -> None:
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "canary.pdf").write_bytes(b"fake PDF bytes; no renderer required")
    values = scenario.model_dump(by_alias=True)
    values["steps"][0]["file"] = "fixtures/canary.pdf"
    values["steps"][2]["mention"] = True
    values["assert"] = [
        {"kind": "same_thread", "turn": 2, "as_turn": 1},
        {"kind": "progress_seen", "turn": 1, "within_s": 2},
        {"kind": "no_blank_message", "turn": 1},
        {"kind": "text_present", "turn": 2, "pattern": "APPLE"},
        {"kind": "log_absent", "turn": 2, "event": "session_preparation.replaced"},
    ]
    (tmp_path / "canary.yaml").write_text(yaml.safe_dump(values))
    loaded = load_catalog(tmp_path)[0]
    assert isinstance(loaded, Scenario)
    result = Executor(backend, judge, ledger, pricing, "staging").run(loaded)
    assert result.status == "PASS"
    assert "send:thread:True" in backend.events
    assert len(result.turns) == 2
    assert len([check for check in result.checks if check.kind == "text_absent"]) == 6
    assert backend.events[-1] == "delete"


@pytest.mark.parametrize(
    "content",
    [
        "(empty response)",
        "Discord Error (404): Unknown Message",
        "access_token=oops",
    ],
)
def test_global_assertions_fail_unsafe_output(
    content: str,
    backend: FakeBackend,
    judge: FakeJudge,
    ledger: Ledger,
    pricing: Pricing,
    scenario: Scenario,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = backend.collect

    def collect(turn: Turn, timeout: float) -> None:
        original(turn, timeout)
        turn.messages[0]["content"] = content

    monkeypatch.setattr(backend, "collect", collect)
    scenario.assertions = [Assertion(kind="done_within_s", turn=2, maximum=10)]
    result = Executor(backend, judge, ledger, pricing, "staging").run(scenario)
    assert result.status == "FAIL"
    assert any(check.kind == "text_absent" and check.status == "FAIL" for check in result.checks)
    assert backend.events[-1] == "delete"


def test_context_nonce_thread_environment_and_regex_literals(tmp_path: Path) -> None:
    result = Result(
        "run",
        "qa",
        "staging",
        turns=[
            Turn(1, "trigger", "channel", utcnow(), thread_id="thread"),
        ],
    )
    context = Context(
        {
            "nonce": "abc12345",
            "guild_id": "guild",
            "channel:A": "channel",
            "env.default_agent": "haiku-qa",
        },
        result,
        tmp_path / "resolved",
    )
    assert (
        context.resolve("{nonce}:{guild_id}:{turn1.thread_id}:{channel:A}:{env.default_agent}")
        == "abc12345:guild:thread:channel:haiku-qa"
    )
    assert context.resolve(r"[a-f]{12} x{4,} {nonce}") == r"[a-f]{12} x{4,} abc12345"
    assert context.substitute({"args": ["{nonce}", 1, True]}) == {"args": ["abc12345", 1, True]}
    with pytest.raises(Pending, match="unavailable"):
        context.resolve("{fork.name}")


def test_templated_yaml_fixture_preserves_name_and_source(tmp_path: Path) -> None:
    original = tmp_path / "agent.yaml"
    original.write_text("name: qa-{nonce}\n")
    context = Context({"nonce": "abc12345"}, Result("run", "qa", "staging"), tmp_path / "resolved")
    step = context.step(Step(do="mention", text="create {nonce}", file=str(original)))
    assert step.text == "create abc12345"
    assert step.file and Path(step.file).name == original.name
    assert Path(step.file).read_text() == "name: qa-abc12345\n"
    assert original.read_text() == "name: qa-{nonce}\n"


def test_multiple_owned_channel_refs_and_reply_reference(
    backend: FakeBackend,
    judge: FakeJudge,
    ledger: Ledger,
    pricing: Pricing,
    scenario: Scenario,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str | None]] = []
    original = backend.send

    def send(
        channel: str, step: Step, *, mention: bool, reply_message_id: str | None = None
    ) -> str:
        calls.append((channel, reply_message_id))
        return original(channel, step, mention=mention, reply_message_id=reply_message_id)

    monkeypatch.setattr(backend, "send", send)
    scenario.steps = [
        Step(do="new_channel", ref="A"),
        Step(do="new_channel", ref="B"),
        Step(do="mention", channel="B", text="{channel:A}: {nonce}"),
        Step(do="wait_done"),
        Step(do="thread_reply", reply_to="turn1.chunk1", mention=True, text="reply"),
        Step(do="wait_done"),
    ]
    result = Executor(backend, judge, ledger, pricing, "staging").run(scenario)
    assert result.status == "PASS"
    assert calls == [("other-parent", None), ("thread", "answer-1")]
    assert backend.events.count("delete") == 2
    assert backend.events.count("create") == 2


def test_same_thread_progress_and_blank_failures(backend: FakeBackend, judge: FakeJudge) -> None:
    first = Turn(1, "1", "parent", utcnow(), thread_id="first")
    second = Turn(
        2,
        "2",
        "parent",
        utcnow(),
        thread_id="different",
        messages=[{"id": "3", "channel_id": "different", "content": "  "}],
    )
    for assertion in [
        Assertion(kind="same_thread", turn=2, as_turn=1),
        Assertion(kind="progress_seen", turn=2, within_s=10),
        Assertion(kind="no_blank_message", turn=2),
    ]:
        assert evaluate(assertion, [first, second], backend, judge).status == "FAIL"


def test_admin_string_fixture_is_substituted_without_shell(tmp_path: Path) -> None:
    import shlex

    original = tmp_path / "agent.yaml"
    original.write_text("name: qa-{nonce}\n")
    context = Context(
        {"nonce": "abc12345", "fork.name": "qa-fork"},
        Result("run", "qa", "staging"),
        tmp_path / "resolved",
    )
    step = context.step(
        Step(do="admin", tool="cli", args=shlex.join(["create", str(original), "{fork.name}"]))
    )
    assert isinstance(step.args, str)
    args = shlex.split(step.args)
    assert args[2] == "qa-fork"
    assert Path(args[1]).read_text() == "name: qa-abc12345\n"
    assert original.read_text() == "name: qa-{nonce}\n"
