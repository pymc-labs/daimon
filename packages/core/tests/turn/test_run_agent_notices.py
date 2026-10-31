"""`turn.run`: the agent hears of failures around its turns, the person never sees them inline."""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from daimon.core.errors import TurnError
from daimon.core.stores.agent_notices import record_agent_notices
from daimon.core.turn import run as run_module
from daimon.core.turn.agent_notices import LOST_WORKSPACE_NOTICE
from daimon.core.turn.driver import SEND_ATTEMPTED
from daimon.core.turn.prepare import ContinuityOutcome
from daimon.core.turn.state import McpServerFailure, TurnState
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_DROPPED = McpServerFailure(
    server_name="notion",
    error_type="mcp_connection_failed_error",
    message="timeout",
    retry_status="exhausted",
)


async def test_the_next_turn_takes_queued_notices_in_front_of_its_message(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Queued notices lead the message once; the turn after gets the message alone."""
    async with db_session_factory.begin() as db:
        tenant = await make_tenant(db)
        await record_agent_notices(
            db,
            tenant_id=tenant.id,
            platform="discord",
            thread_id="T",
            agent_id="agt",
            texts=["dropped"],
        )
    deps = SimpleNamespace(sessionmaker=db_session_factory)
    prepared = SimpleNamespace(
        admission=SimpleNamespace(agent=SimpleNamespace(id="agt")),
        continuity=ContinuityOutcome(),
        mapping_id=None,
    )

    async def reseed() -> str:
        return "hi again"

    kwargs: dict[str, Any] = {
        "tenant_id": tenant.id,
        "platform": "discord",
        "thread_id": "T",
        "reseed_user_message": reseed,
        "now": datetime.now(UTC),
    }

    first, first_reseed, taken = await run_module._with_agent_notices(  # pyright: ignore[reportPrivateUsage]
        deps,  # pyright: ignore[reportArgumentType]
        prepared,  # pyright: ignore[reportArgumentType]
        user_message="hi",
        **kwargs,
    )
    assert first.startswith("<daimon_notices>") and first.endswith("</daimon_notices>\n\nhi"), (
        "the notices lead the person's message"
    )
    assert (await first_reseed()).endswith("</daimon_notices>\n\nhi again"), (
        "a session recovered mid-turn is told too"
    )
    assert taken == ["dropped"], "what was told comes back, to queue again if never sent"
    again, again_reseed, _ = await run_module._with_agent_notices(  # pyright: ignore[reportPrivateUsage]
        deps,  # pyright: ignore[reportArgumentType]
        prepared,  # pyright: ignore[reportArgumentType]
        user_message="still there?",
        **kwargs,
    )
    assert again == "still there?", "a notice is told once"
    assert again_reseed is reseed, "and the reseed is untouched"


def _outcome(
    state: TurnState, continuity: ContinuityOutcome | None = None, *, loss_told: bool = False
) -> Any:
    return run_module.RunOutcome(
        state=state,
        ma_session_id="sess",
        mapping_id=None,
        recovered=False,
        continuity=continuity or ContinuityOutcome(),
        loss_told=loss_told,
    )


async def _run(monkeypatch: pytest.MonkeyPatch, outcome: Any, *, sent: bool) -> list[list[str]]:
    """Run one chat turn whose message carried the queued notice "earlier"."""
    queued: list[list[str]] = []

    async def no_summary(*_a: Any, user_message: str, **_k: Any) -> tuple[str, None]:
        return user_message, None

    async def with_notices(
        *_a: Any, user_message: str, reseed_user_message: Any, **_k: Any
    ) -> tuple[str, Any, list[str]]:
        return f"[notices]\n\n{user_message}", reseed_user_message, ["earlier"]

    async def queue(_deps: Any, texts: list[str], **_k: Any) -> None:
        queued.append(list(texts))

    async def turn(*_a: Any, **_k: Any) -> Any:
        mark = SEND_ATTEMPTED.get()
        assert mark is not None, "the turn runs with a send mark"
        if sent:
            mark.append(True)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(run_module, "_with_handoff_summary", no_summary)
    monkeypatch.setattr(run_module, "_with_agent_notices", with_notices)
    monkeypatch.setattr(run_module, "_queue_agent_notices", queue)
    monkeypatch.setattr(run_module, "run_prepared_turn_impl", turn)
    observation = SimpleNamespace(
        activate=contextlib.nullcontext,
        finish=lambda **_k: None,
        session_id=None,
        model_by_session={},
    )
    prepared = SimpleNamespace(
        admission=SimpleNamespace(
            observation=observation,
            account_id=uuid.uuid4(),
            agent=SimpleNamespace(id="agt"),
            backend_footer_profile=None,
            backend_revision=None,
        ),
        ma_session_id="sess",
        _finalize=None,
    )
    deps = SimpleNamespace(
        running_turns=SimpleNamespace(track=contextlib.nullcontext),
        backend_selection_enabled=False,
    )
    with contextlib.suppress(RuntimeError):
        await run_module.run_prepared_turn(
            deps,  # pyright: ignore[reportArgumentType]
            prepared,  # pyright: ignore[reportArgumentType]
            tenant_id=uuid.uuid4(),
            platform="discord",
            thread_id="T",
            external_user_id="42",
            user_message="hi",
            lifecycle=None,  # pyright: ignore[reportArgumentType]
            cancel=None,  # pyright: ignore[reportArgumentType]
            reseed_user_message=None,  # pyright: ignore[reportArgumentType]
            recovery_lifecycle=None,  # pyright: ignore[reportArgumentType]
        )
    return queued


async def test_an_answered_turn_without_a_server_queues_it_for_the_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dropped server is the agent's to mention next turn; sent notices are not kept."""
    queued = await _run(
        monkeypatch, _outcome(TurnState(content=[], mcp_failures=(_DROPPED,))), sent=True
    )
    assert len(queued) == 1 and len(queued[0]) == 1, "one notice, for the dropped server"
    assert "`notion`" in queued[0][0], "the notice names the server"


async def test_notices_go_back_when_the_turn_never_sent_its_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The agent never saw them, so the next turn still has them."""
    queued = await _run(monkeypatch, RuntimeError("provider failed"), sent=False)
    assert queued == [["earlier"]], "taken notices are queued again"


async def test_a_failed_turn_after_a_lost_workspace_queues_the_loss_not_the_servers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The person saw the error card; the agent hears of the loss next turn."""
    failed = TurnState(error=TurnError(kind="upstream", message="boom"), mcp_failures=(_DROPPED,))
    lost = ContinuityOutcome(state="replaced_after_loss", transfer_kind="history")
    queued = await _run(monkeypatch, _outcome(failed, lost), sent=True)
    assert queued == [[LOST_WORKSPACE_NOTICE]], "only the loss; the card covered the failure"


async def test_a_failed_turn_whose_replacement_heard_of_the_loss_queues_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The replacement was sent its loss framing; telling it again would repeat it."""
    failed = TurnState(error=TurnError(kind="upstream", message="boom"))
    lost = ContinuityOutcome(state="replaced_after_loss", transfer_kind="history")
    queued = await _run(monkeypatch, _outcome(failed, lost, loss_told=True), sent=True)
    assert queued == [[]], "no loss notice the agent already had"
