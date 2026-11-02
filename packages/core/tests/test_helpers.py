"""Helper turns: their conversation key and the words a refused helper sends back."""

from typing import cast

import pytest
from daimon.core.errors import UserFacingError
from daimon.core.helpers import helper_thread_key, refusal_words, serve_helpers
from daimon.core.turn.admission import Admission
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.errors import AdmissionDenied


def test_each_helper_keeps_its_own_conversation_per_thread() -> None:
    keys = {
        helper_thread_key("t1", "ag_a"),
        helper_thread_key("t1", "ag_b"),
        helper_thread_key("t2", "ag_a"),
    }
    assert len(keys) == 3
    assert helper_thread_key("t1", "ag_a") != "t1", "never the asking agent's own session"


@pytest.mark.parametrize(
    ("error", "words"),
    [
        (AdmissionDenied(reason="cap_exceeded"), "argon can't run for this person: the person"),
        (AdmissionDenied(reason="backend_unsupported"), "its model provider can't run it here"),
        (UserFacingError("Sign in to GitHub first."), "argon can't run: Sign in to GitHub"),
        (RuntimeError("boom"), "argon stopped with an error before it answered."),
    ],
)
def test_a_refused_helper_tells_the_asking_agent_why(error: Exception, words: str) -> None:
    assert words in refusal_words(error, "argon")
    assert "boom" not in refusal_words(error, "argon"), "internal errors stay in the logs"


async def test_only_a_persons_own_turns_serve_helpers() -> None:
    # A routine's turn returns before it touches anything.
    async with serve_helpers(
        cast(TurnDeps, None),
        cast(Admission, None),
        origin="routine",
        deadline=None,
        confirm_write=None,
        provider_action=None,
    ):
        pass
