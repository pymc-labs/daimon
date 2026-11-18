"""The shared credential submit ladder, driven through a fake platform port.

The writes are stubbed: their own suites cover the stores. This pins the
order of card edits, replies and resumes every platform gets.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Unpack, cast

import pytest
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.core import credential_flow
from daimon.core.agent_pins import FormPinRefused
from daimon.core.credential_flow import (
    WRITE_FAILED_MESSAGE,
    CardEdit,
    collision_lines,
    spent_outcome,
    submit_env,
    submit_env_file,
)
from daimon.core.credential_submit import EnvSubmitPlan, EnvSubmitResult
from daimon.core.env_file import EnvEntry
from daimon.core.posted_controls import NO_LONGER_VALID_MESSAGE, CardState
from daimon.core.stores.agent_files import AgentEnvEncryptionRequiredError
from daimon.core.stores.domain import ChatPlatform, CredentialRequestRow

_NOW = datetime(2024, 1, 1, tzinfo=UTC)
_AGENT = cast(BetaManagedAgentsAgent, object())
_PLAN = EnvSubmitPlan(related_before=frozenset(), shadowed="OLD_KEY", refuse_replacement=False)
PLATFORMS: tuple[ChatPlatform, ...] = ("discord", "slack", "teams")


def _row(kind: str = "env") -> CredentialRequestRow:
    return CredentialRequestRow(
        token="tok-1",
        kind=kind,
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        target="MY_KEY",
        mcp_server_url=None,
        requester_platform_user_id="user-1",
        channel_id="C1",
        idempotency_key=uuid.uuid4(),
        target_name="helper",
        responder_name="Daimon",
        created_at=_NOW,
        expires_at=_NOW + timedelta(hours=1),
        used_at=_NOW,
    )


class _Port:
    def __init__(self, platform: ChatPlatform) -> None:
        self.platform: ChatPlatform = platform
        self.sessionmaker: Any = None
        self.log_prefix = "test"
        self.replies: list[str] = []
        self.edits: list[tuple[str, dict[str, Any]]] = []
        self.resumed = 0
        self.failed: list[str] = []

    async def reply(self, text: str) -> None:
        self.replies.append(text)

    async def edit_card(
        self, row: CredentialRequestRow, state: CardState, **card: Unpack[CardEdit]
    ) -> None:
        self.edits.append((state, dict(card)))

    async def resume(self, row: CredentialRequestRow) -> None:
        self.resumed += 1

    def write_failed(self, kind: str, err: Exception, **_: object) -> None:
        self.failed.append(kind)


def _stub(monkeypatch: pytest.MonkeyPatch, name: str, result: object) -> None:
    async def fake(*_: object, **__: object) -> object:
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(credential_flow, name, fake)


@pytest.mark.parametrize("platform", PLATFORMS)
@pytest.mark.parametrize("queued", [True, False])
async def test_an_applied_key_walks_the_card_and_resumes_only_queued_work(
    monkeypatch: pytest.MonkeyPatch, platform: ChatPlatform, queued: bool
) -> None:
    _stub(monkeypatch, "apply_env_submit", EnvSubmitResult(consumed=_row(), queued=queued))
    port = _Port(platform)
    await submit_env(port, row=_row(), agent=_AGENT, value="secret", plan=_PLAN)
    assert [state for state, _ in port.edits] == ["received", "applied"]
    assert port.edits[1][1]["outcome"].kind == "key"
    assert port.replies == [], "the card is the receipt"
    assert port.resumed == int(queued)


@pytest.mark.parametrize("platform", PLATFORMS)
@pytest.mark.parametrize(
    ("state", "refusal", "replaces"),
    [("superseded", None, None), ("refused", "replacement_admin_required", "OLD_KEY")],
)
async def test_an_unapplied_key_ends_the_card_and_tells_the_submitter(
    monkeypatch: pytest.MonkeyPatch,
    platform: ChatPlatform,
    state: str,
    refusal: str | None,
    replaces: str | None,
) -> None:
    result = EnvSubmitResult(consumed=_row(), state=cast(Any, state))
    _stub(monkeypatch, "apply_env_submit", result)
    port = _Port(platform)
    await submit_env(port, row=_row(), agent=_AGENT, value="secret", plan=_PLAN)
    assert port.edits == [("received", {}), (state, {"refusal": refusal, "replaces": replaces})]
    assert len(port.replies) == 1 and port.resumed == 0


@pytest.mark.parametrize("platform", PLATFORMS)
@pytest.mark.parametrize(
    ("raised", "reply", "failed"),
    [
        (FormPinRefused("pinned elsewhere"), "pinned elsewhere", []),
        (AgentEnvEncryptionRequiredError(), str(AgentEnvEncryptionRequiredError()), []),
        (RuntimeError("secret in the text"), WRITE_FAILED_MESSAGE, ["env"]),
        (None, NO_LONGER_VALID_MESSAGE, []),
    ],
)
async def test_a_key_that_wrote_nothing_only_replies(
    monkeypatch: pytest.MonkeyPatch,
    platform: ChatPlatform,
    raised: Exception | None,
    reply: str,
    failed: list[str],
) -> None:
    _stub(monkeypatch, "apply_env_submit", raised or EnvSubmitResult(consumed=None))
    port = _Port(platform)
    await submit_env(port, row=_row(), agent=_AGENT, value="secret", plan=_PLAN)
    assert port.replies == [reply] and port.edits == [] and port.failed == failed


_ENTRIES = (EnvEntry(name="A_KEY", value="a", line=1), EnvEntry(name="B_KEY", value="b", line=2))


@pytest.mark.parametrize(
    ("platform", "nothing_changed"),
    [
        ("discord", "Take them out of the file, or ask to replace them by name."),
        ("slack", "Ask Daimon to replace a key you already have."),
        ("teams", "Ask Daimon to replace a key you already have."),
    ],
)
async def test_a_file_holding_a_stored_key_saves_nothing(
    monkeypatch: pytest.MonkeyPatch, platform: ChatPlatform, nothing_changed: str
) -> None:
    held = frozenset({"A_KEY"})
    _stub(monkeypatch, "apply_env_file_submit", (_row("env_file"), [_ENTRIES[0]], False, held))
    port = _Port(platform)
    await submit_env_file(port, row=_row("env_file"), agent=_AGENT, entries=_ENTRIES)
    [(state, card)] = port.edits
    assert state == "refused" and card["refusal"] == "env_file_invalid"
    assert card["refusal_lines"][0] == "line 1: A_KEY is already set."
    assert card["refusal_lines"][-1].endswith(nothing_changed)
    assert port.replies[0].startswith("No keys were saved for helper.")
    assert "a" not in card["refusal_lines"][0].split(), "no value reaches the card"
    assert port.resumed == 0


@pytest.mark.parametrize("platform", PLATFORMS)
async def test_a_clean_file_goes_straight_to_applied_and_resumes(
    monkeypatch: pytest.MonkeyPatch, platform: ChatPlatform
) -> None:
    _stub(
        monkeypatch,
        "apply_env_file_submit",
        (_row("env_file"), list[EnvEntry](), True, frozenset[str]()),
    )
    port = _Port(platform)
    await submit_env_file(port, row=_row("env_file"), agent=_AGENT, entries=_ENTRIES)
    [(state, card)] = port.edits
    assert state == "applied" and card["outcome"].count == 2
    assert port.replies == [] and port.resumed == 1


def test_collision_lines_name_three_keys_then_count_the_rest() -> None:
    entries = [EnvEntry(name=f"K{n}", value="v", line=n) for n in range(1, 6)]
    lines = collision_lines(entries, frozenset(e.name for e in entries))
    assert lines == (*(f"line {n}: K{n} is already set." for n in (1, 2, 3)), "…and 2 more.")


@pytest.mark.parametrize(
    ("reason", "outcome"),
    [
        ("token_rejected", "token_rejected"),
        ("mcp_permission_required", "policy_refused"),
        ("target_unavailable", "write_failed"),
    ],
)
def test_spent_outcome_records_why_nothing_was_written(reason: Any, outcome: str) -> None:
    assert spent_outcome(reason) == outcome
