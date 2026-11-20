"""Script actions beyond buttons: forms, credential submission, the simulated requester."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from daimon.core.credential_requests import mint_request_token
from daimon.core.evals.chat.events import ChatLog
from daimon.core.evals.chat.forms import CredentialPresses
from daimon.core.evals.chat.script import FAKE_SECRET, HIDDEN, ScriptPlayer
from daimon.core.evals.format import CardButton, NeutralCard
from daimon.core.stores import credential_requests as store
from daimon.core.stores.agent_files import list_agent_files
from daimon.testing.eval_world import eval_case
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_BUTTONS = [
    CardButton(id="go", label="Save", action="save"),
    CardButton(id="no", label="Skip", action="skip"),
]


class _Presses:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    async def submit_credential(self, token: str, value: str) -> bool:
        self.calls.append((token, value))
        return True

    async def skip_credential(self, token: str) -> bool:
        self.calls.append((token, None))
        return True


def _player(script: dict[str, Any], presses: _Presses | None = None, **kw: Any) -> ScriptPlayer:
    case = eval_case({}, script=script)
    return ScriptPlayer(case, ChatLog(), presses=presses, **kw)


def _credential(kind: str = "credential_request") -> NeutralCard:
    return NeutralCard.model_validate({
        "id": "tok1", "kind": kind, "buttons": _BUTTONS,
        "fields": [{"id": "token", "label": "Token", "kind": "secret"},
                   {"id": "note", "label": "Note", "kind": "text"}],
    })  # fmt: skip


async def test_submit_form_when_card_is_a_credential_request_then_the_press_gets_the_value() -> (
    None
):
    presses = _Presses()
    step = {"id": "s", "on": {"event": "credential_request"},
            "do": [{"action": "submit_form", "fields": {"token": "abc", "note": "hi"}}]}  # fmt: skip
    script = _player({"steps": [step]}, presses)

    pressed = await script.answer(_credential())

    assert pressed is not None and pressed.action == "save"
    assert presses.calls == [("tok1", "abc")]
    shown = script.chat.events[-1].card
    assert shown is not None and shown.answer is not None
    assert shown.answer.fields == {"token": HIDDEN, "note": "hi"}, "the chat never shows a secret"


async def test_credential_defaults_when_submit_fake_then_a_fake_value_is_saved() -> None:
    presses = _Presses()
    script = _player({"defaults": {"credential_request": "submit_fake"}}, presses)

    await script.answer(_credential())

    assert presses.calls == [("tok1", FAKE_SECRET)]


async def test_credential_defaults_when_skip_then_the_request_is_skipped() -> None:
    presses = _Presses()

    await _player({}, presses).answer(_credential())

    assert presses.calls == [("tok1", None)]


async def test_submit_form_when_card_is_a_form_then_the_values_are_the_next_message() -> None:
    step = {"id": "s", "on": {"event": "card", "card_kind": "form"},
            "do": [{"action": "submit_form", "fields": {"note": "blue"}}]}  # fmt: skip
    script = _player({"steps": [step]})

    await script.answer(_credential("form"))

    assert script.replies == ["note: blue"]


async def test_question_when_simulate_then_the_requester_replies_else_nothing() -> None:
    asked: list[str] = []

    async def reply(question: str) -> str | None:
        asked.append(question)
        return "The staging one."

    simulated = _player({"defaults": {"question": "simulate"}}, simulate_reply=reply)
    await simulated.question("Which environment?")
    ended = _player({}, simulate_reply=reply)
    await ended.question("Which environment?")

    assert simulated.replies == ["The staging one."] and asked == ["Which environment?"]
    assert simulated.chat.events[-1].actor == "u1" and ended.replies == []


async def test_credential_presses_when_submitted_then_the_key_is_saved_once_and_skip_spends_it(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    agent_id = uuid.uuid4()

    async def request() -> str:
        row = await store.create_credential_request(
            db_session, token=mint_request_token(), kind="env", tenant_id=tenant.id,
            agent_id=agent_id, account_id=account.id, target="NOTES_TOKEN", mcp_server_url=None,
            requester_platform_user_id="u1", channel_id="c", idempotency_key=uuid.uuid4(),
            expires_at=datetime.now(UTC) + timedelta(minutes=30), target_ma_agent_id="ag",
            target_name="acme", requested_work=None,
        )  # fmt: skip
        await db_session.commit()
        return row.token

    presses = CredentialPresses(db_session_factory)
    saved, skipped = await request(), await request()

    assert await presses.submit_credential(saved, "v1")
    assert not await presses.submit_credential(saved, "v2"), "single use"
    assert await presses.skip_credential(skipped)
    assert not await presses.skip_credential(skipped)
    async with db_session_factory() as session:
        files = await list_agent_files(session, tenant_id=tenant.id, agent_id=agent_id)
        spent = await store.peek_credential_request(session, token=skipped)
    assert [f.key for f in files] == ["NOTES_TOKEN"]
    assert spent is not None and spent.outcome == "declined"
