"""Helper turns: their conversation key and the words a refused helper sends back."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from daimon.core.errors import UserFacingError
from daimon.core.helpers import (
    _in_thread,  # pyright: ignore[reportPrivateUsage]
    helper_thread_key,
    refusal_words,
    serve_helpers,
)
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_identity import matching_turn
from daimon.core.stores.domain import Role, TurnOriginRow
from daimon.core.turn.admission import Admission
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.provider_actions import (
    NativeActionResponse,
    ProviderActionContext,
    ProviderActionPrompt,
    ProviderActionRequester,
)

NOW = datetime(2026, 11, 2, tzinfo=UTC)


def _origin(*, helper_of: uuid.UUID | None) -> TurnOriginRow:
    return TurnOriginRow(
        id=uuid.uuid4(),
        tenant_id=uuid.UUID(int=1),
        account_id=uuid.uuid4(),
        platform="slack",
        parent_channel_id="C1",
        thread_id="1.2",
        responder_ma_agent_id="ag_argon",
        responder_name="argon",
        configuration_target_ma_agent_id=None,
        configuration_target_name=None,
        role=Role.USER,
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
        helper_of=helper_of,
    )


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


def test_a_helpers_turn_is_found_by_its_own_session_thread() -> None:
    helper = _origin(helper_of=uuid.uuid4())
    agent = derive_agent_uuid(tenant_id=helper.tenant_id, ma_agent_id="ag_argon")
    key = helper_thread_key("1.2", "ag_argon")

    assert (helper.thread_id, helper.session_thread_id) == ("1.2", key)
    assert _origin(helper_of=None).session_thread_id == "1.2"
    assert matching_turn([helper], tenant_id=helper.tenant_id, agent_id=agent, thread_id=key)
    assert not matching_turn(
        [helper], tenant_id=helper.tenant_id, agent_id=agent, thread_id="1.2"
    ), "a helper's turn never answers for the asking thread's own session"


async def test_a_helpers_provider_prompts_go_to_the_asking_thread() -> None:
    seen: list[ProviderActionPrompt] = []

    async def hook(prompt: ProviderActionPrompt) -> NativeActionResponse | None:
        seen.append(prompt)
        return None

    requester = ProviderActionRequester("slack", helper_thread_key("1.2", "ag_argon"), "U1")
    context = ProviderActionContext(cast(Any, None), cast(Any, None), "root", requester, NOW)
    placed = _in_thread(hook, "1.2")

    assert placed is not None and _in_thread(None, "1.2") is None
    await placed(ProviderActionPrompt(cast(Any, None), context, cast(Any, None)))
    [prompt] = seen
    assert prompt.context.requester == ProviderActionRequester("slack", "1.2", "U1")
