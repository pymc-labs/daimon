"""A Gemini thread whose conversation was pruned is told to start a new thread."""

from __future__ import annotations

from typing import Any

import pytest
from daimon.core.turn import gemini_prepare, provider_gate
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.notices import RefusalNouns, admission_refusal_text
from mux.errors import ContinuityLost


async def test_a_pruned_conversation_refuses_with_the_session_lost_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ContinuityLost from the session read is a refusal, not a generic failure."""

    async def pruned(request: Any, prepare: Any) -> None:
        raise ContinuityLost("binding", ("Gemini conversation pruned after it sat idle",))

    monkeypatch.setattr(provider_gate, "gated_turn", pruned)
    with pytest.raises(AdmissionDenied) as refused:
        await gemini_prepare.prepare_gemini_turn(None)  # pyright: ignore[reportArgumentType]
    assert (refused.value.reason, refused.value.cause) == ("backend_unsupported", "session_lost")


def test_the_session_lost_copy_says_to_start_a_new_thread() -> None:
    text = admission_refusal_text(
        "backend_unsupported",
        RefusalNouns(scope="server", admin="a server admin", billing="/billing"),
        cause="session_lost",
    )
    assert "new thread" in text, "the person is told what to do"
