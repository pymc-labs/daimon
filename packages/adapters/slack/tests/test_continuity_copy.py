"""Slack adapter tests for turn-continuity rendering.

Covers `_run_thread_turn`'s continuity-facing branches: a `SessionPreparationFailed`
must render `render_preparation_failed` and never run a turn; a `SessionAgentMismatch`
must render `render_responder_changed_without_handoff`, not the generic error copy,
and never run a turn; `is_setup` on the turn origin must come from
`admission.config.thread_binding_kind == "setup"`, not merely "a binding exists" (a
handoff binding must NOT count as setup); and each non-"continued" `ContinuityOutcome`
state must leave the reply as the agent's own text, with no system line.

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
    render_responder_changed_without_handoff,
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
from daimon.testing.ma import resolved_agent_env_router
from daimon.testing.ma_models import ma_agent, ma_environment
from mux.testing.drivers.anthropic.ma import build_fake_anthropic
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from yarl import URL

from .conftest import shown_text

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
            "daimon.core.spend_gate.is_over_balance", new_callable=AsyncMock
        ) as mock_over_balance,
        patch("daimon.core.spend_gate.is_over_cap", new_callable=AsyncMock) as mock_over_cap,
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
    assert shown_text(body) == render_preparation_failed("uat-agent")


@pytest.mark.parametrize("selfhosted", [False, True])
async def test_responder_changed_without_handoff_renders_offer_and_does_not_run_turn(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
    selfhosted: bool,
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

    from dataclasses import replace

    from daimon.core.turn.admission import admit as real_admit
    from mux.contracts.config import BackendConfig, ConfigRevision, resolve_default
    from mux.contracts.ids import ChannelRef

    async def admitted(*args: Any, **kwargs: Any) -> Admission:
        original = await real_admit(*args, **kwargs)
        if not selfhosted:
            return original
        revision = ConfigRevision.create(
            ChannelRef(tenant_id=str(tenant_id), platform="slack", channel_id=channel),
            1,
            resolve_default(
                BackendConfig(backend="selfhosted", profile="selfhosted.loop", model="gpt-6-luna")
            ),
        )
        return replace(original, backend_revision=revision, backend_revision_origin="channel")

    with (
        patch("daimon.adapters.slack.app.admit", side_effect=admitted),
        patch(
            "daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock
        ) as mock_resolve_agent,
        patch(
            "daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock
        ) as mock_resolve_env,
        patch(
            "daimon.core.spend_gate.is_over_balance", new_callable=AsyncMock
        ) as mock_over_balance,
        patch("daimon.core.spend_gate.is_over_cap", new_callable=AsyncMock) as mock_over_cap,
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
    assert shown_text(body) == render_responder_changed_without_handoff(
        new_responder="uat-agent",
        owner="the previous agent" if selfhosted else "uat-agent",
        channel=f"<#{channel}>",
        offer_button=not selfhosted,
    )
    buttons = [
        element
        for block in (body.get("blocks") or body["attachments"][0]["blocks"])
        if block["type"] == "actions"
        for element in block["elements"]
    ]
    assert [(b["action_id"], b["value"]) for b in buttons] == (
        [] if selfhosted else [("thread_hand_over", _AGENT_ID)]
    ), "the notice carries the Hand over button for the agent the channel answers with"


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
            "daimon.core.spend_gate.is_over_balance", new_callable=AsyncMock
        ) as mock_over_balance,
        patch("daimon.core.spend_gate.is_over_cap", new_callable=AsyncMock) as mock_over_cap,
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


async def test_replaced_leaves_the_reply_as_the_agents_text(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """A planned replacement adds no summary to the reply; the agent says it."""
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
            "daimon.core.spend_gate.is_over_balance", new_callable=AsyncMock
        ) as mock_over_balance,
        patch("daimon.core.spend_gate.is_over_cap", new_callable=AsyncMock) as mock_over_cap,
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
        "the reply must be exactly the agent's text, with no replacement summary"
    )
    assert _posted_texts(fake_slack_web_client) == ["Working on it…"], (
        "only the status card is posted, no summary"
    )


async def test_replaced_with_no_answer_posts_no_summary_message(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """A tool-only, cancelled or failed turn has no answer to hang a summary on,
    and none is posted on its own."""
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
            "daimon.core.spend_gate.is_over_balance", new_callable=AsyncMock
        ) as mock_over_balance,
        patch("daimon.core.spend_gate.is_over_cap", new_callable=AsyncMock) as mock_over_cap,
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

    assert _posted_texts(fake_slack_web_client) == ["Working on it…"], (
        "only the status card is posted, no summary"
    )


@pytest.mark.parametrize(
    "transfer_kind",
    ["transcript", "history", None],
    ids=["transcript_variant", "history_variant", "history_variant_default"],
)
async def test_outcome_replaced_after_loss_leaves_the_reply_as_the_agents_text(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
    transfer_kind: Literal["transcript", "history"] | None,
) -> None:
    """A dead-session recovery adds no system line of its own: what was lost is
    the agent's to say, so the reply is exactly the agent's text."""
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
            "daimon.core.spend_gate.is_over_balance", new_callable=AsyncMock
        ) as mock_over_balance,
        patch("daimon.core.spend_gate.is_over_cap", new_callable=AsyncMock) as mock_over_cap,
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

    assert _final_answer_text(fake_slack_web_client) == _ANSWER, (
        "the reply must be exactly the agent's text, with no system line"
    )
    assert _posted_texts(fake_slack_web_client) == ["Working on it…"], (
        "only the status card is posted, no system message"
    )


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
            "daimon.core.spend_gate.is_over_balance", new_callable=AsyncMock
        ) as mock_over_balance,
        patch("daimon.core.spend_gate.is_over_cap", new_callable=AsyncMock) as mock_over_cap,
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
    notices = [attachment for p in posts for attachment in p.kwargs["json"].get("attachments", [])]
    assert [(a["color"], a["blocks"][0]["text"]["text"]) for a in notices] == [
        ("#5865F2", render_current_work_must_finish("uat-agent", handoff=False))
    ], "the must-finish line is a neutral-barred notice after the answer, not a red error"


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
            "daimon.core.spend_gate.is_over_balance", new_callable=AsyncMock
        ) as mock_over_balance,
        patch("daimon.core.spend_gate.is_over_cap", new_callable=AsyncMock) as mock_over_cap,
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
    assert shown_text(updates[0].kwargs["json"]) == render_current_work_must_finish(
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
            "daimon.core.spend_gate.is_over_balance", new_callable=AsyncMock
        ) as mock_over_balance,
        patch("daimon.core.spend_gate.is_over_cap", new_callable=AsyncMock) as mock_over_cap,
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
            "daimon.core.spend_gate.is_over_balance", new_callable=AsyncMock
        ) as mock_over_balance,
        patch("daimon.core.spend_gate.is_over_cap", new_callable=AsyncMock) as mock_over_cap,
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


async def test_mention_turn_backend_switch_card_captures_through_its_own_admission(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ordinary mention turn's Confirm card reads the switched agent's source as
    that turn's own admitted caller. Built without the admission, every
    backend-switch card on this call site refuses `source_unavailable`
    (packages/core/daimon/core/turn/admission.py:capture_admitted_backend_source).
    """
    from daimon.core import confirmation
    from daimon.core.config import TurnSettings

    team_id = "T_MENTION_ADMISSION"
    channel = "C_TEST"
    thread_ts = "9100000099.000001"
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
    fernet_key = Fernet.generate_key().decode()

    await provision_tenant(db_session_factory, platform="slack", workspace_id=team_id)
    await upsert_slack_bot_token(
        db_session,
        team_id=team_id,
        encrypted_token=encrypt_token(build_multifernet((fernet_key,)), "xoxb-mention-admission"),
    )
    await db_session.commit()

    app = _make_app(db_session_factory, tenant_id_str=str(tenant_id))
    app.runtime.settings.crypto.keys = (SecretStr(fernet_key),)
    app.runtime.settings.turn = TurnSettings(path="mux", channel_backends=True)

    event = _event(channel=channel, thread_ts=thread_ts, user="U_MENTION_ADMISSION")
    prepared = _prepared_turn(continuity=ContinuityOutcome())

    services: list[Any] = []
    real_callbacks = confirmation.backend_switch_callbacks

    def spy_callbacks(service: Any, **kwargs: Any) -> Any:
        services.append(service)
        return real_callbacks(service, **kwargs)

    monkeypatch.setattr(confirmation, "backend_switch_callbacks", spy_callbacks)
    captured = MagicMock(name="captured_source")
    capture_backend_source = AsyncMock(return_value=captured)
    monkeypatch.setattr("daimon.core.turn.admission.capture_backend_source", capture_backend_source)

    with (
        patch(
            "daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock
        ) as mock_resolve_env,
        patch(
            "daimon.core.spend_gate.is_over_balance", new_callable=AsyncMock
        ) as mock_over_balance,
        patch("daimon.core.spend_gate.is_over_cap", new_callable=AsyncMock) as mock_over_cap,
        patch(
            "daimon.adapters.slack.app.bind_session", new_callable=AsyncMock
        ) as mock_bind_session,
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

    (service,) = services
    source = MagicMock(name="source")
    capture = service._capture_source  # pyright: ignore[reportPrivateUsage]
    assert await capture(source) is captured
    capture_backend_source.assert_awaited_once()
    args, kwargs = capture_backend_source.await_args
    assert args[1] is source
    context = kwargs["context"]
    assert (context.platform, context.channel_id, context.thread_id) == (
        "slack",
        channel,
        thread_ts,
    )


def test_selfhosted_tool_turn_skips_output_sweep(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from daimon.core.turn.state import ToolUseBlock

    app = _make_app(db_session_factory, tenant_id_str=str(uuid.uuid4()))
    outcome = RunOutcome(
        TurnState(
            content=[
                ToolUseBlock(
                    kind="tool_use", id="tool", type="agent.tool_use", name="bash", input={}
                )
            ]
        ),
        "sh_session_test",
        None,
        False,
    )
    with patch.object(app, "_spawn", side_effect=AssertionError("no Anthropic output sweep")):
        app._schedule_output_sweep(  # pyright: ignore[reportPrivateUsage]
            outcome,
            MagicMock(),
            channel="C_TEST",
            thread_id="1.2",
            team_id="T_TEST",
            requester_user_id="U_TEST",
            selfhosted=True,
        )
