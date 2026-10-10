"""Slack adapter tests for turn-continuity rendering.

Covers `_run_thread_turn`'s continuity-facing branches: a `SessionPreparationFailed`
must render `render_preparation_failed` and never run a turn; a `SessionAgentMismatch`
must render `render_responder_changed_without_handoff`, not the generic error copy,
and never run a turn; `is_setup` on the turn origin must come from
`admission.config.thread_binding_kind == "setup"`, not merely "a binding exists" (a
handoff binding must NOT count as setup); and each non-"continued" `ContinuityOutcome`
state must produce its documented pre/post-answer notice.

`bind_session` (and, for the notice-copy tests, `run_prepared_turn`) are mocked at the
`daimon.adapters.slack.app` boundary -- the same precedent test_app.py's ceiling tests
already use to isolate `_run_thread_turn`'s branching from the internals of
`daimon.core.session_preparation` / `daimon.core.turn.run`, which are unit-tested in
`packages/core/tests/` on their own terms.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from cryptography.fernet import Fernet
from daimon.adapters.slack.app import SlackApp
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.continuity.messages import (
    render_current_work_must_finish,
    render_preparation_failed,
    render_replacement_summary,
    render_responder_changed_without_handoff,
    render_unexpected_loss,
)
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.github_credentials import build_multifernet, encrypt_token
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.slack_bot_tokens import upsert_slack_bot_token
from daimon.core.stores.thread_agent_bindings import create_binding
from daimon.core.stores.thread_sessions import (
    create_thread_session,
    get_thread_session_by_id,
    mark_turn_active,
)
from daimon.core.turn.admission import Admission
from daimon.core.turn.deps import build_turn_deps
from daimon.core.turn.errors import (
    SessionAgentMismatch,
    SessionBusyError,
    SessionPreparationFailed,
)
from daimon.core.turn.prepare import ContinuityOutcome, PreparedTurn
from daimon.core.turn.run import RunOutcome
from daimon.core.turn.state import TextBlock, TurnState
from daimon.core.turn_origin import turn_origin as real_turn_origin
from daimon.testing import build_fake_anthropic, ma_agent, ma_environment, resolved_agent_env_router
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from yarl import URL

_AGENT_ID = "agent_continuity_test"
_ENV_ID = "env_continuity_test"


def _make_app(sessionmaker: async_sessionmaker[AsyncSession], *, tenant_id_str: str) -> SlackApp:
    settings = MagicMock()
    settings.crypto.keys = ()
    settings.slack.max_concurrent_turns_per_tenant = 3
    settings.slack.bot_display_name = "daimon"
    settings.mcp.public_url = None
    settings.mcp.app_root_url = None
    settings.defaults_root = MagicMock()
    settings.billing.markup = Decimal("1.0")

    anthropic_client = build_fake_anthropic(
        resolved_agent_env_router(
            ma_agent(id=_AGENT_ID, name="uat-agent", tenant_id=tenant_id_str),
            ma_environment(id=_ENV_ID, name="test-env", tenant_id=tenant_id_str),
        ).dispatch
    )
    deployment_default = DeploymentDefault(agent_name="uat-agent", environment_name="test-env")
    resolver_cache = new_resolver_cache()
    turn_deps = build_turn_deps(
        settings,
        anthropic_client,
        sessionmaker,
        deployment_default=deployment_default,
        resolver_cache=resolver_cache,
        billing_config=None,
    )
    runtime = SlackRuntime(
        settings=settings,
        anthropic=anthropic_client,
        sessionmaker=sessionmaker,
        billing_config=None,
        http_client=MagicMock(spec=httpx.AsyncClient),
        resolver_cache=resolver_cache,
        turn_deps=turn_deps,
        deployment_default=deployment_default,
    )
    return SlackApp(runtime=runtime)


def _event(*, channel: str, thread_ts: str, user: str) -> dict[str, Any]:
    return {
        "type": "app_mention",
        "channel": channel,
        "event_ts": thread_ts,
        "ts": thread_ts,
        "thread_ts": thread_ts,
        "user": user,
        "text": "<@U_BOT> hello",
    }


async def _record_noop(*, event: Any) -> None:
    return None


#: The text a driven turn "answers" with. Short enough that a prepended
#: notice never pushes the first chunk past Slack's per-block ceiling.
_ANSWER = "Here is the answer you asked for."


def _run_turn_revealing_answer(outcome: RunOutcome, *, answer: str = _ANSWER) -> Any:
    """A `run_prepared_turn` stand-in that actually reveals an answer.

    The real driver ends a turn by calling `on_terminal_success`, which is
    where the adapter's continuity notices are folded into the answer text.
    A mock that only returns a `RunOutcome` never reaches that code, so these
    tests drive the lifecycle exactly as the driver would before returning.
    """

    async def _run(*_args: Any, **kwargs: Any) -> RunOutcome:
        lifecycle = kwargs["lifecycle"]
        await lifecycle.on_terminal_success(
            TurnState(content=[TextBlock(kind="text", text=answer)])
        )
        return outcome

    return _run


def _final_answer_text(fake_slack_web_client: Any) -> str:
    """The markdown body of the last chat.update -- the card the answer landed on."""
    updates = [
        req
        for (_method, url), reqs in fake_slack_web_client.mock.requests.items()
        if url == URL("https://slack.com/api/chat.update")
        for req in reqs
    ]
    assert updates, "the answer must have replaced the status card"
    blocks = updates[-1].kwargs["json"]["blocks"]
    return str(blocks[0]["text"])


def _posted_texts(fake_slack_web_client: Any) -> list[str]:
    return [
        req.kwargs["json"]["text"]
        for (_method, url), reqs in fake_slack_web_client.mock.requests.items()
        if url == URL("https://slack.com/api/chat.postMessage")
        for req in reqs
    ]


def _prepared_turn(
    *, continuity: ContinuityOutcome, mapping_id: uuid.UUID | None = None
) -> PreparedTurn:
    """A `PreparedTurn` whose only load-bearing field for these tests is
    `continuity` -- `admission` is never read by real code once
    `run_prepared_turn` itself is mocked, so a bare placeholder is enough to
    satisfy the frozen dataclass's constructor."""
    return PreparedTurn(
        admission=MagicMock(spec=Admission),
        ma_session_id="sess_continuity_test",
        mapping_id=mapping_id if mapping_id is not None else uuid.uuid4(),
        watermark=None,
        reused=True,
        session_account_id=uuid.uuid4(),
        _record=_record_noop,
        continuity=continuity,
    )


async def test_session_preparation_failed_renders_copy_and_does_not_run_turn(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    team_id = "T_PREP_FAILED"
    channel = "C_TEST"
    thread_ts = "9100000001.000001"
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
    fernet_key = Fernet.generate_key().decode()

    await provision_tenant(db_session_factory, platform="slack", workspace_id=team_id)
    await upsert_slack_bot_token(
        db_session,
        team_id=team_id,
        encrypted_token=encrypt_token(build_multifernet((fernet_key,)), "xoxb-prep-failed"),
    )
    await db_session.commit()

    app = _make_app(db_session_factory, tenant_id_str=str(tenant_id))
    app.runtime.settings.crypto.keys = (SecretStr(fernet_key),)

    event = _event(channel=channel, thread_ts=thread_ts, user="U_PREP_FAILED")

    with (
        patch(
            "daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock
        ) as mock_resolve_agent,
        patch(
            "daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock
        ) as mock_resolve_env,
        patch(
            "daimon.core.turn.admission.is_over_balance", new_callable=AsyncMock
        ) as mock_over_balance,
        patch("daimon.core.turn.admission.is_over_cap", new_callable=AsyncMock) as mock_over_cap,
        patch(
            "daimon.adapters.slack.app.bind_session", new_callable=AsyncMock
        ) as mock_bind_session,
        patch(
            "daimon.adapters.slack.app.run_prepared_turn", new_callable=AsyncMock
        ) as mock_run_prepared_turn,
    ):
        mock_resolve_agent.return_value = _AGENT_ID
        mock_resolve_env.return_value = _ENV_ID
        mock_over_balance.return_value = False
        mock_over_cap.return_value = False
        mock_bind_session.side_effect = SessionPreparationFailed(
            reasons=("model",),
            stage="checkpointed",
            retry_after=datetime.now(UTC),
            preserved=True,
        )

        await app._handle_app_mention(event, team_id=team_id)  # pyright: ignore[reportPrivateUsage]

    mock_run_prepared_turn.assert_not_called()
    updates = [
        req
        for (_method, url), reqs in fake_slack_web_client.mock.requests.items()
        if url == URL("https://slack.com/api/chat.update")
        for req in reqs
    ]
    assert len(updates) == 1, "the status card must be edited in place with the failure copy"
    body = updates[0].kwargs["json"]
    assert body["text"] == render_preparation_failed("uat-agent")


async def test_responder_changed_without_handoff_renders_offer_and_does_not_run_turn(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    team_id = "T_RESPONDER_MISMATCH"
    channel = "C_TEST"
    thread_ts = "9100000002.000001"
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
    fernet_key = Fernet.generate_key().decode()

    await provision_tenant(db_session_factory, platform="slack", workspace_id=team_id)
    await upsert_slack_bot_token(
        db_session,
        team_id=team_id,
        encrypted_token=encrypt_token(build_multifernet((fernet_key,)), "xoxb-mismatch"),
    )
    await db_session.commit()

    app = _make_app(db_session_factory, tenant_id_str=str(tenant_id))
    app.runtime.settings.crypto.keys = (SecretStr(fernet_key),)

    event = _event(channel=channel, thread_ts=thread_ts, user="U_MISMATCH")

    with (
        patch(
            "daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock
        ) as mock_resolve_agent,
        patch(
            "daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock
        ) as mock_resolve_env,
        patch(
            "daimon.core.turn.admission.is_over_balance", new_callable=AsyncMock
        ) as mock_over_balance,
        patch("daimon.core.turn.admission.is_over_cap", new_callable=AsyncMock) as mock_over_cap,
        patch(
            "daimon.adapters.slack.app.bind_session", new_callable=AsyncMock
        ) as mock_bind_session,
    ):
        mock_resolve_agent.return_value = _AGENT_ID
        mock_resolve_env.return_value = _ENV_ID
        mock_over_balance.return_value = False
        mock_over_cap.return_value = False
        mock_bind_session.side_effect = SessionAgentMismatch(
            mapping_id=uuid.uuid4(),
            session_id="sess_owned_by_other_agent",
            source_agent_id=_AGENT_ID,
            destination_agent_id=_AGENT_ID,
        )

        await app._handle_app_mention(event, team_id=team_id)  # pyright: ignore[reportPrivateUsage]

    updates = [
        req
        for (_method, url), reqs in fake_slack_web_client.mock.requests.items()
        if url == URL("https://slack.com/api/chat.update")
        for req in reqs
    ]
    assert len(updates) == 1
    body = updates[0].kwargs["json"]
    # source_agent_id == the admitted agent's own id here, so the "owner"
    # lookup resolves the SAME fake agent ("uat-agent") -- this pins the
    # renderer call shape, not a distinct owner name (there is only one
    # agent in this fake).
    assert body["text"] == render_responder_changed_without_handoff(
        new_responder="uat-agent", owner="uat-agent", channel=f"<#{channel}>", offer_button=True
    )
    buttons = [
        element
        for block in body["blocks"]
        if block["type"] == "actions"
        for element in block["elements"]
    ]
    assert [(b["action_id"], b["value"]) for b in buttons] == [("thread_hand_over", _AGENT_ID)], (
        "the notice carries the Hand over button for the agent the channel answers with"
    )


async def test_is_setup_is_false_for_a_handoff_binding(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """A thread whose task was handed to another agent (`kind='handoff'`) must
    NOT be treated as a setup conversation -- `is_setup` must come from
    `thread_binding_kind == "setup"`, not `thread_binding_id is not None`.
    """
    team_id = "T_HANDOFF_NOT_SETUP"
    channel = "C_TEST"
    thread_ts = "9100000003.000001"
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
    fernet_key = Fernet.generate_key().decode()

    await provision_tenant(db_session_factory, platform="slack", workspace_id=team_id)
    await upsert_slack_bot_token(
        db_session,
        team_id=team_id,
        encrypted_token=encrypt_token(build_multifernet((fernet_key,)), "xoxb-handoff"),
    )
    await create_binding(
        db_session,
        tenant_id=tenant_id,
        platform="slack",
        parent_channel_id=channel,
        thread_id=thread_ts,
        responder_ma_agent_id=_AGENT_ID,
        responder_name="uat-agent",
        kind="handoff",
    )
    await db_session.commit()

    app = _make_app(db_session_factory, tenant_id_str=str(tenant_id))
    app.runtime.settings.crypto.keys = (SecretStr(fernet_key),)

    event = _event(channel=channel, thread_ts=thread_ts, user="U_HANDOFF")

    captured: dict[str, Any] = {}

    def _capturing_turn_origin(sessionmaker: Any, **kwargs: Any) -> Any:
        captured["is_setup"] = kwargs.get("is_setup")
        return real_turn_origin(sessionmaker, **kwargs)

    prepared = _prepared_turn(continuity=ContinuityOutcome())

    with (
        patch(
            "daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock
        ) as mock_resolve_env,
        patch(
            "daimon.core.turn.admission.is_over_balance", new_callable=AsyncMock
        ) as mock_over_balance,
        patch("daimon.core.turn.admission.is_over_cap", new_callable=AsyncMock) as mock_over_cap,
        patch(
            "daimon.adapters.slack.app.bind_session", new_callable=AsyncMock
        ) as mock_bind_session,
        patch("daimon.adapters.slack.app.turn_origin", new=_capturing_turn_origin),
        patch(
            "daimon.adapters.slack.app.run_prepared_turn", new_callable=AsyncMock
        ) as mock_run_prepared_turn,
    ):
        mock_resolve_env.return_value = _ENV_ID
        mock_over_balance.return_value = False
        mock_over_cap.return_value = False
        mock_bind_session.return_value = prepared
        mock_run_prepared_turn.return_value = RunOutcome(
            state=TurnState(),
            ma_session_id=prepared.ma_session_id,
            mapping_id=prepared.mapping_id,
            recovered=False,
        )
        await app._handle_app_mention(event, team_id=team_id)  # pyright: ignore[reportPrivateUsage]

    assert captured.get("is_setup") is False, (
        "a handoff binding must not be treated as a setup conversation"
    )


async def test_replaced_makes_the_replacement_summary_the_answers_first_paragraph(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """The summary explains the answer, so it has to be readable above it.

    The answer is an in-place edit of the status card posted at mention time
    and Slack orders by the original ts, so a summary posted as its own
    message sorts BELOW the answer no matter when it is sent -- the reader
    saw "the file vanished" before the explanation. It rides the answer text
    instead."""
    team_id = "T_REPLACED"
    channel = "C_TEST"
    thread_ts = "9100000004.000001"
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
    fernet_key = Fernet.generate_key().decode()

    await provision_tenant(db_session_factory, platform="slack", workspace_id=team_id)
    await upsert_slack_bot_token(
        db_session,
        team_id=team_id,
        encrypted_token=encrypt_token(build_multifernet((fernet_key,)), "xoxb-replaced"),
    )
    await db_session.commit()

    app = _make_app(db_session_factory, tenant_id_str=str(tenant_id))
    app.runtime.settings.crypto.keys = (SecretStr(fernet_key),)

    event = _event(channel=channel, thread_ts=thread_ts, user="U_REPLACED")

    prepared = _prepared_turn(
        continuity=ContinuityOutcome(state="replaced", applied=("model",), transfer_kind="full")
    )

    with (
        patch(
            "daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock
        ) as mock_resolve_agent,
        patch(
            "daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock
        ) as mock_resolve_env,
        patch(
            "daimon.core.turn.admission.is_over_balance", new_callable=AsyncMock
        ) as mock_over_balance,
        patch("daimon.core.turn.admission.is_over_cap", new_callable=AsyncMock) as mock_over_cap,
        patch(
            "daimon.adapters.slack.app.bind_session", new_callable=AsyncMock
        ) as mock_bind_session,
        patch(
            "daimon.adapters.slack.app.run_prepared_turn", new_callable=AsyncMock
        ) as mock_run_prepared_turn,
    ):
        mock_resolve_agent.return_value = _AGENT_ID
        mock_resolve_env.return_value = _ENV_ID
        mock_over_balance.return_value = False
        mock_over_cap.return_value = False
        mock_bind_session.return_value = prepared
        mock_run_prepared_turn.side_effect = _run_turn_revealing_answer(
            RunOutcome(
                state=TurnState(),
                ma_session_id=prepared.ma_session_id,
                mapping_id=prepared.mapping_id,
                recovered=False,
                continuity=prepared.continuity,
            )
        )

        await app._handle_app_mention(event, team_id=team_id)  # pyright: ignore[reportPrivateUsage]

    summary = render_replacement_summary("full", lost=[])
    assert _final_answer_text(fake_slack_web_client) == f"{summary}\n\n{_ANSWER}", (
        "the replacement summary must be the answer's first paragraph"
    )
    assert summary not in _posted_texts(fake_slack_web_client), (
        "the summary must not also arrive as its own message below the answer"
    )


async def test_replaced_with_no_transfer_kind_renders_no_summary_prefix(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """A fresh start (`transfer_kind=None`) is a `replaced` bind with nothing
    carried across -- not a transfer whose contents need summarizing. The old
    `transfer_kind or "full"` fallback rendered the FULL-transfer summary
    ("Your conversation, decisions and working files came across.") on a
    fresh start, contradicting the fresh-start confirmation already posted.
    No prefix should be rendered at all."""
    team_id = "T_REPLACED_FRESH_START"
    channel = "C_TEST"
    thread_ts = "9100000006.000001"
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
    fernet_key = Fernet.generate_key().decode()

    await provision_tenant(db_session_factory, platform="slack", workspace_id=team_id)
    await upsert_slack_bot_token(
        db_session,
        team_id=team_id,
        encrypted_token=encrypt_token(build_multifernet((fernet_key,)), "xoxb-fresh-start"),
    )
    await db_session.commit()

    app = _make_app(db_session_factory, tenant_id_str=str(tenant_id))
    app.runtime.settings.crypto.keys = (SecretStr(fernet_key),)

    event = _event(channel=channel, thread_ts=thread_ts, user="U_REPLACED_FRESH_START")

    prepared = _prepared_turn(continuity=ContinuityOutcome(state="replaced", transfer_kind=None))

    with (
        patch(
            "daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock
        ) as mock_resolve_agent,
        patch(
            "daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock
        ) as mock_resolve_env,
        patch(
            "daimon.core.turn.admission.is_over_balance", new_callable=AsyncMock
        ) as mock_over_balance,
        patch("daimon.core.turn.admission.is_over_cap", new_callable=AsyncMock) as mock_over_cap,
        patch(
            "daimon.adapters.slack.app.bind_session", new_callable=AsyncMock
        ) as mock_bind_session,
        patch(
            "daimon.adapters.slack.app.run_prepared_turn", new_callable=AsyncMock
        ) as mock_run_prepared_turn,
    ):
        mock_resolve_agent.return_value = _AGENT_ID
        mock_resolve_env.return_value = _ENV_ID
        mock_over_balance.return_value = False
        mock_over_cap.return_value = False
        mock_bind_session.return_value = prepared
        mock_run_prepared_turn.side_effect = _run_turn_revealing_answer(
            RunOutcome(
                state=TurnState(),
                ma_session_id=prepared.ma_session_id,
                mapping_id=prepared.mapping_id,
                recovered=False,
                continuity=prepared.continuity,
            )
        )

        await app._handle_app_mention(event, team_id=team_id)  # pyright: ignore[reportPrivateUsage]

    assert _final_answer_text(fake_slack_web_client) == _ANSWER, (
        "a fresh start must not prefix the answer with a replacement summary"
    )
    posted = _posted_texts(fake_slack_web_client)
    assert render_replacement_summary("full", lost=[]) not in posted
    assert render_replacement_summary("transcript", lost=[]) not in posted
    assert render_replacement_summary("history", lost=[]) not in posted


async def test_replaced_falls_back_to_its_own_message_when_no_answer_is_revealed(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """A tool-only, cancelled or failed turn reveals no answer to carry the
    summary. The person still has to be told what the replacement carried
    across, so it goes out on its own rather than being dropped."""
    team_id = "T_REPLACED_NO_ANSWER"
    channel = "C_TEST"
    thread_ts = "9100000004.000009"
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
    fernet_key = Fernet.generate_key().decode()

    await provision_tenant(db_session_factory, platform="slack", workspace_id=team_id)
    await upsert_slack_bot_token(
        db_session,
        team_id=team_id,
        encrypted_token=encrypt_token(build_multifernet((fernet_key,)), "xoxb-replaced-noans"),
    )
    await db_session.commit()

    app = _make_app(db_session_factory, tenant_id_str=str(tenant_id))
    app.runtime.settings.crypto.keys = (SecretStr(fernet_key),)

    event = _event(channel=channel, thread_ts=thread_ts, user="U_REPLACED_NO_ANSWER")
    prepared = _prepared_turn(
        continuity=ContinuityOutcome(state="replaced", transfer_kind="transcript")
    )

    with (
        patch(
            "daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock
        ) as mock_resolve_agent,
        patch(
            "daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock
        ) as mock_resolve_env,
        patch(
            "daimon.core.turn.admission.is_over_balance", new_callable=AsyncMock
        ) as mock_over_balance,
        patch("daimon.core.turn.admission.is_over_cap", new_callable=AsyncMock) as mock_over_cap,
        patch(
            "daimon.adapters.slack.app.bind_session", new_callable=AsyncMock
        ) as mock_bind_session,
        patch(
            "daimon.adapters.slack.app.run_prepared_turn", new_callable=AsyncMock
        ) as mock_run_prepared_turn,
    ):
        mock_resolve_agent.return_value = _AGENT_ID
        mock_resolve_env.return_value = _ENV_ID
        mock_over_balance.return_value = False
        mock_over_cap.return_value = False
        mock_bind_session.return_value = prepared
        mock_run_prepared_turn.return_value = RunOutcome(
            state=TurnState(),
            ma_session_id=prepared.ma_session_id,
            mapping_id=prepared.mapping_id,
            recovered=False,
            continuity=prepared.continuity,
        )

        await app._handle_app_mention(event, team_id=team_id)  # pyright: ignore[reportPrivateUsage]

    assert render_replacement_summary("transcript", lost=[]) in _posted_texts(
        fake_slack_web_client
    ), "with no answer to carry it, the summary must still reach the person"


@pytest.mark.parametrize(
    "transfer_kind",
    ["transcript", "history", None],
    ids=["transcript_variant", "history_variant", "history_variant_default"],
)
async def test_outcome_replaced_after_loss_prepends_exactly_one_loss_notice_to_the_answer(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
    transfer_kind: Literal["transcript", "history"] | None,
) -> None:
    """A dead-session recovery must tell the person their workspace was lost.
    `run_prepared_turn` only learns this mid-call, so the notice is keyed off
    `outcome.continuity` (the post-turn `RunOutcome`), never
    `prepared.continuity` (the pre-turn bind decision, which can never
    actually carry "replaced_after_loss"). By then the answer has already
    replaced the status card, so the card is edited once more to put the
    notice above the answer instead of sending it underneath."""
    team_id = f"T_REPLACED_AFTER_LOSS_{transfer_kind}"
    channel = "C_TEST"
    thread_ts = "9100000005.000001"
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
    fernet_key = Fernet.generate_key().decode()

    await provision_tenant(db_session_factory, platform="slack", workspace_id=team_id)
    await upsert_slack_bot_token(
        db_session,
        team_id=team_id,
        encrypted_token=encrypt_token(build_multifernet((fernet_key,)), "xoxb-loss"),
    )
    await db_session.commit()

    app = _make_app(db_session_factory, tenant_id_str=str(tenant_id))
    app.runtime.settings.crypto.keys = (SecretStr(fernet_key),)

    event = _event(channel=channel, thread_ts=thread_ts, user="U_LOSS")

    # The pre-turn bind saw nothing unusual; the loss is discovered only once
    # run_prepared_turn's recovery cycle recreates the session mid-call.
    prepared = _prepared_turn(continuity=ContinuityOutcome())

    with (
        patch(
            "daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock
        ) as mock_resolve_agent,
        patch(
            "daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock
        ) as mock_resolve_env,
        patch(
            "daimon.core.turn.admission.is_over_balance", new_callable=AsyncMock
        ) as mock_over_balance,
        patch("daimon.core.turn.admission.is_over_cap", new_callable=AsyncMock) as mock_over_cap,
        patch(
            "daimon.adapters.slack.app.bind_session", new_callable=AsyncMock
        ) as mock_bind_session,
        patch(
            "daimon.adapters.slack.app.run_prepared_turn", new_callable=AsyncMock
        ) as mock_run_prepared_turn,
    ):
        mock_resolve_agent.return_value = _AGENT_ID
        mock_resolve_env.return_value = _ENV_ID
        mock_over_balance.return_value = False
        mock_over_cap.return_value = False
        mock_bind_session.return_value = prepared
        mock_run_prepared_turn.side_effect = _run_turn_revealing_answer(
            RunOutcome(
                state=TurnState(),
                ma_session_id="sess_recovered",
                mapping_id=prepared.mapping_id,
                recovered=True,
                continuity=ContinuityOutcome(
                    state="replaced_after_loss", transfer_kind=transfer_kind
                ),
            )
        )

        await app._handle_app_mention(event, team_id=team_id)  # pyright: ignore[reportPrivateUsage]

    expected = render_unexpected_loss("transcript" if transfer_kind == "transcript" else "history")
    assert _final_answer_text(fake_slack_web_client) == f"{expected}\n\n{_ANSWER}", (
        "the loss notice must be the answer's first paragraph, exactly once"
    )
    assert expected not in _posted_texts(fake_slack_web_client), (
        "the loss notice must not also arrive as its own message below the answer"
    )


async def test_bind_replaced_after_loss_never_fires_before_the_turn(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """`bind_session`'s own `PreparedTurn.continuity` can never be
    "replaced_after_loss" in real code -- this is a regression guard for the
    dead pre-turn branch removed from `_run_thread_turn`: even if
    `prepared.continuity` somehow carried that state, no loss notice is
    posted from it, only from `outcome.continuity`."""
    team_id = "T_BIND_NEVER_LOSS"
    channel = "C_TEST"
    thread_ts = "9100000008.000001"
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
    fernet_key = Fernet.generate_key().decode()

    await provision_tenant(db_session_factory, platform="slack", workspace_id=team_id)
    await upsert_slack_bot_token(
        db_session,
        team_id=team_id,
        encrypted_token=encrypt_token(build_multifernet((fernet_key,)), "xoxb-bind-never-loss"),
    )
    await db_session.commit()

    app = _make_app(db_session_factory, tenant_id_str=str(tenant_id))
    app.runtime.settings.crypto.keys = (SecretStr(fernet_key),)

    event = _event(channel=channel, thread_ts=thread_ts, user="U_BIND_NEVER_LOSS")

    prepared = _prepared_turn(
        continuity=ContinuityOutcome(state="replaced_after_loss", transfer_kind="transcript")
    )

    with (
        patch(
            "daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock
        ) as mock_resolve_agent,
        patch(
            "daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock
        ) as mock_resolve_env,
        patch(
            "daimon.core.turn.admission.is_over_balance", new_callable=AsyncMock
        ) as mock_over_balance,
        patch("daimon.core.turn.admission.is_over_cap", new_callable=AsyncMock) as mock_over_cap,
        patch(
            "daimon.adapters.slack.app.bind_session", new_callable=AsyncMock
        ) as mock_bind_session,
        patch(
            "daimon.adapters.slack.app.run_prepared_turn", new_callable=AsyncMock
        ) as mock_run_prepared_turn,
    ):
        mock_resolve_agent.return_value = _AGENT_ID
        mock_resolve_env.return_value = _ENV_ID
        mock_over_balance.return_value = False
        mock_over_cap.return_value = False
        mock_bind_session.return_value = prepared
        # The real run_prepared_turn always restates a mid-call recovery
        # through outcome.continuity -- this fakes an ordinary completed turn,
        # exactly what happens if prepared.continuity were (wrongly) trusted.
        mock_run_prepared_turn.return_value = RunOutcome(
            state=TurnState(),
            ma_session_id=prepared.ma_session_id,
            mapping_id=prepared.mapping_id,
            recovered=False,
            continuity=ContinuityOutcome(),
        )

        await app._handle_app_mention(event, team_id=team_id)  # pyright: ignore[reportPrivateUsage]

    posts = [
        req
        for (_method, url), reqs in fake_slack_web_client.mock.requests.items()
        if url == URL("https://slack.com/api/chat.postMessage")
        for req in reqs
    ]
    texts = [p.kwargs["json"]["text"] for p in posts]
    assert render_unexpected_loss("transcript") not in texts
    assert render_unexpected_loss("history") not in texts


async def test_pending_posts_must_finish_copy_after_the_answer(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    team_id = "T_PENDING"
    channel = "C_TEST"
    thread_ts = "9100000006.000001"
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
    fernet_key = Fernet.generate_key().decode()

    await provision_tenant(db_session_factory, platform="slack", workspace_id=team_id)
    await upsert_slack_bot_token(
        db_session,
        team_id=team_id,
        encrypted_token=encrypt_token(build_multifernet((fernet_key,)), "xoxb-pending"),
    )
    await db_session.commit()

    app = _make_app(db_session_factory, tenant_id_str=str(tenant_id))
    app.runtime.settings.crypto.keys = (SecretStr(fernet_key),)

    event = _event(channel=channel, thread_ts=thread_ts, user="U_PENDING")

    prepared = _prepared_turn(continuity=ContinuityOutcome(state="continued", pending=("model",)))

    with (
        patch(
            "daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock
        ) as mock_resolve_agent,
        patch(
            "daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock
        ) as mock_resolve_env,
        patch(
            "daimon.core.turn.admission.is_over_balance", new_callable=AsyncMock
        ) as mock_over_balance,
        patch("daimon.core.turn.admission.is_over_cap", new_callable=AsyncMock) as mock_over_cap,
        patch(
            "daimon.adapters.slack.app.bind_session", new_callable=AsyncMock
        ) as mock_bind_session,
        patch(
            "daimon.adapters.slack.app.run_prepared_turn", new_callable=AsyncMock
        ) as mock_run_prepared_turn,
    ):
        mock_resolve_agent.return_value = _AGENT_ID
        mock_resolve_env.return_value = _ENV_ID
        mock_over_balance.return_value = False
        mock_over_cap.return_value = False
        mock_bind_session.return_value = prepared
        mock_run_prepared_turn.return_value = RunOutcome(
            state=TurnState(),
            ma_session_id=prepared.ma_session_id,
            mapping_id=prepared.mapping_id,
            recovered=False,
            continuity=prepared.continuity,
        )

        await app._handle_app_mention(event, team_id=team_id)  # pyright: ignore[reportPrivateUsage]

    posts = [
        req
        for (_method, url), reqs in fake_slack_web_client.mock.requests.items()
        if url == URL("https://slack.com/api/chat.postMessage")
        for req in reqs
    ]
    texts = [p.kwargs["json"]["text"] for p in posts]
    assert render_current_work_must_finish("uat-agent", handoff=False) in texts


async def test_session_busy_renders_must_finish_copy_and_does_not_run_turn(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """A responder change that arrives while the previous turn is still
    running must not be made *around* that turn: the session still in flight
    belongs to the outgoing responder, so running this turn on it would answer
    as one agent inside another agent's workspace. No turn runs; the person is
    told the in-flight message finishes first."""
    team_id = "T_SESSION_BUSY"
    channel = "C_TEST"
    thread_ts = "9100000007.000001"
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
    fernet_key = Fernet.generate_key().decode()

    await provision_tenant(db_session_factory, platform="slack", workspace_id=team_id)
    await upsert_slack_bot_token(
        db_session,
        team_id=team_id,
        encrypted_token=encrypt_token(build_multifernet((fernet_key,)), "xoxb-busy"),
    )
    await db_session.commit()

    app = _make_app(db_session_factory, tenant_id_str=str(tenant_id))
    app.runtime.settings.crypto.keys = (SecretStr(fernet_key),)

    event = _event(channel=channel, thread_ts=thread_ts, user="U_BUSY")

    with (
        patch(
            "daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock
        ) as mock_resolve_agent,
        patch(
            "daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock
        ) as mock_resolve_env,
        patch(
            "daimon.core.turn.admission.is_over_balance", new_callable=AsyncMock
        ) as mock_over_balance,
        patch("daimon.core.turn.admission.is_over_cap", new_callable=AsyncMock) as mock_over_cap,
        patch(
            "daimon.adapters.slack.app.bind_session", new_callable=AsyncMock
        ) as mock_bind_session,
        patch(
            "daimon.adapters.slack.app.run_prepared_turn", new_callable=AsyncMock
        ) as mock_run_prepared_turn,
    ):
        mock_resolve_agent.return_value = _AGENT_ID
        mock_resolve_env.return_value = _ENV_ID
        mock_over_balance.return_value = False
        mock_over_cap.return_value = False
        mock_bind_session.side_effect = SessionBusyError(
            pending_reasons=("agent_identity",), retry_after=datetime.now(UTC)
        )

        await app._handle_app_mention(event, team_id=team_id)  # pyright: ignore[reportPrivateUsage]

    mock_run_prepared_turn.assert_not_called()
    updates = [
        req
        for (_method, url), reqs in fake_slack_web_client.mock.requests.items()
        if url == URL("https://slack.com/api/chat.update")
        for req in reqs
    ]
    assert len(updates) == 1, "the status card must be edited in place with the busy copy"
    assert updates[0].kwargs["json"]["text"] == render_current_work_must_finish(
        "uat-agent", handoff=True
    )


async def test_the_turn_marker_is_cleared_before_continuations_are_dispatched(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """Dispatching first made the handoff answer as the wrong agent.

    A continuation's own `bind_session` reads the thread's live row: a marker
    still standing there means "a turn is running", and a responder change
    that cannot be made yet is deferred onto the session that is running --
    the OUTGOING agent's. So this turn's marker has to be gone before
    anything is dispatched."""
    team_id = "T_MARKER_ORDER"
    channel = "C_TEST"
    thread_ts = "9100000008.000001"
    user = "U_MARKER_ORDER"
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
    fernet_key = Fernet.generate_key().decode()

    await provision_tenant(db_session_factory, platform="slack", workspace_id=team_id)
    await upsert_slack_bot_token(
        db_session,
        team_id=team_id,
        encrypted_token=encrypt_token(build_multifernet((fernet_key,)), "xoxb-marker-order"),
    )
    principal = await get_or_create_platform_principal(
        db_session, tenant_id=tenant_id, platform="slack", external_id=user
    )
    row = await create_thread_session(
        db_session,
        tenant_id=tenant_id,
        platform="slack",
        thread_id=thread_ts,
        account_id=principal.account_id,
        ma_session_id="sess_marker_order",
        ma_agent_id=_AGENT_ID,
    )
    await db_session.commit()

    app = _make_app(db_session_factory, tenant_id_str=str(tenant_id))
    app.runtime.settings.crypto.keys = (SecretStr(fernet_key),)

    event = _event(channel=channel, thread_ts=thread_ts, user=user)
    prepared = _prepared_turn(continuity=ContinuityOutcome(), mapping_id=row.id)

    observed: dict[str, Any] = {}

    async def _recording_dispatch(*_args: Any, **kwargs: Any) -> None:
        async with db_session_factory() as s:
            at_dispatch = await get_thread_session_by_id(s, id=row.id)
        observed["marker_at_dispatch"] = (
            None if at_dispatch is None else at_dispatch.active_turn_message_id
        )
        observed["active_turn"] = kwargs["active_turn"]

    async def _recording_mark_turn_active(*args: Any, **kwargs: Any) -> None:
        # Non-vacuity: a marker that was never written would make the
        # "cleared before dispatch" assertion below pass for free.
        observed["marked"] = kwargs["id"]
        await mark_turn_active(*args, **kwargs)

    with (
        patch(
            "daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock
        ) as mock_resolve_agent,
        patch(
            "daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock
        ) as mock_resolve_env,
        patch(
            "daimon.core.turn.admission.is_over_balance", new_callable=AsyncMock
        ) as mock_over_balance,
        patch("daimon.core.turn.admission.is_over_cap", new_callable=AsyncMock) as mock_over_cap,
        patch(
            "daimon.adapters.slack.app.bind_session", new_callable=AsyncMock
        ) as mock_bind_session,
        patch(
            "daimon.adapters.slack.app.run_prepared_turn", new_callable=AsyncMock
        ) as mock_run_prepared_turn,
        patch(
            "daimon.adapters.slack.app.dispatch_pending_continuations",
            new=_recording_dispatch,
        ),
        patch(
            "daimon.adapters.slack.app.mark_turn_active",
            new=_recording_mark_turn_active,
        ),
    ):
        mock_resolve_agent.return_value = _AGENT_ID
        mock_resolve_env.return_value = _ENV_ID
        mock_over_balance.return_value = False
        mock_over_cap.return_value = False
        mock_bind_session.return_value = prepared
        mock_run_prepared_turn.side_effect = _run_turn_revealing_answer(
            RunOutcome(
                state=TurnState(),
                ma_session_id=prepared.ma_session_id,
                mapping_id=row.id,
                recovered=False,
                continuity=prepared.continuity,
            )
        )

        await app._handle_app_mention(event, team_id=team_id)  # pyright: ignore[reportPrivateUsage]

    assert observed.get("marked") == row.id, "the turn must have marked this row active"
    assert "marker_at_dispatch" in observed, "the dispatcher must have been reached"
    assert observed["marker_at_dispatch"] is None, (
        "this turn's active-turn marker must be cleared BEFORE the dispatcher runs"
    )
    assert observed["active_turn"] is False, (
        "with the marker cleared, the dispatcher must be told no turn is running"
    )


async def test_the_dispatcher_is_told_a_turn_is_running_when_the_row_still_says_so(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """`active_turn` comes from the thread's live row, not from a constant.

    This turn tracks -- and clears -- an older mapping row, while a newer live
    row for the same caller still carries a marker (a turn that landed on a
    row this caller never tracked). The dispatcher must see that, because
    `decide_continuation` skips on it rather than queueing a second turn into
    a thread that is already busy."""
    team_id = "T_MARKER_LIVE"
    channel = "C_TEST"
    thread_ts = "9100000009.000001"
    user = "U_MARKER_LIVE"
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
    fernet_key = Fernet.generate_key().decode()

    await provision_tenant(db_session_factory, platform="slack", workspace_id=team_id)
    await upsert_slack_bot_token(
        db_session,
        team_id=team_id,
        encrypted_token=encrypt_token(build_multifernet((fernet_key,)), "xoxb-marker-live"),
    )
    principal = await get_or_create_platform_principal(
        db_session, tenant_id=tenant_id, platform="slack", external_id=user
    )
    older = await create_thread_session(
        db_session,
        tenant_id=tenant_id,
        platform="slack",
        thread_id=thread_ts,
        account_id=principal.account_id,
        ma_session_id="sess_marker_live_old",
        ma_agent_id=_AGENT_ID,
        created_at=datetime(2026, 9, 13, 10, 0, tzinfo=UTC),
    )
    newer = await create_thread_session(
        db_session,
        tenant_id=tenant_id,
        platform="slack",
        thread_id=thread_ts,
        account_id=principal.account_id,
        ma_session_id="sess_marker_live_new",
        ma_agent_id=_AGENT_ID,
        created_at=datetime(2026, 9, 13, 11, 0, tzinfo=UTC),
    )
    await mark_turn_active(
        db_session,
        id=newer.id,
        active_turn_message_id="9100000009.000002",
        active_turn_channel_id=channel,
        now=datetime.now(UTC),
    )
    await db_session.commit()

    app = _make_app(db_session_factory, tenant_id_str=str(tenant_id))
    app.runtime.settings.crypto.keys = (SecretStr(fernet_key),)

    event = _event(channel=channel, thread_ts=thread_ts, user=user)
    prepared = _prepared_turn(continuity=ContinuityOutcome(), mapping_id=older.id)

    observed: dict[str, Any] = {}

    async def _recording_dispatch(*_args: Any, **kwargs: Any) -> None:
        observed["active_turn"] = kwargs["active_turn"]

    with (
        patch(
            "daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock
        ) as mock_resolve_agent,
        patch(
            "daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock
        ) as mock_resolve_env,
        patch(
            "daimon.core.turn.admission.is_over_balance", new_callable=AsyncMock
        ) as mock_over_balance,
        patch("daimon.core.turn.admission.is_over_cap", new_callable=AsyncMock) as mock_over_cap,
        patch(
            "daimon.adapters.slack.app.bind_session", new_callable=AsyncMock
        ) as mock_bind_session,
        patch(
            "daimon.adapters.slack.app.run_prepared_turn", new_callable=AsyncMock
        ) as mock_run_prepared_turn,
        patch(
            "daimon.adapters.slack.app.dispatch_pending_continuations",
            new=_recording_dispatch,
        ),
    ):
        mock_resolve_agent.return_value = _AGENT_ID
        mock_resolve_env.return_value = _ENV_ID
        mock_over_balance.return_value = False
        mock_over_cap.return_value = False
        mock_bind_session.return_value = prepared
        mock_run_prepared_turn.side_effect = _run_turn_revealing_answer(
            RunOutcome(
                state=TurnState(),
                ma_session_id=prepared.ma_session_id,
                mapping_id=older.id,
                recovered=False,
                continuity=prepared.continuity,
            )
        )

        await app._handle_app_mention(event, team_id=team_id)  # pyright: ignore[reportPrivateUsage]

    assert observed["active_turn"] is True, (
        "active_turn must be read back from the caller's live row, not assumed False"
    )
