"""Slack's routine result poster (FEAT-085)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from daimon.adapters.slack import routine_delivery as poster_mod
from daimon.adapters.slack.routine_delivery import make_slack_routine_poster
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.agent_identity import AgentIdentity
from daimon.core.scope import ChannelScopeRef, DeploymentDefault
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.domain import RoutineRow
from daimon.core.stores.routines import create_routine
from daimon.core.stores.scoped_config_write import set_fields
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def _routine(
    db_session: AsyncSession, *, kind: str, destination_id: str, agent_name: str = "daimon"
) -> RoutineRow:
    tenant = await make_tenant(db_session, platform="slack", workspace_id="T_ROUTINES")
    from daimon.core.stores.direct_messages import set_dm_enabled

    await set_dm_enabled(db_session, tenant_id=tenant.id, enabled=True)
    row = await create_routine(
        db_session,
        tenant_id=tenant.id,
        created_by_user_id="U1",
        agent_id="ag",
        agent_name=agent_name,
        cron_expr="0 9 * * 1",
        timezone_="UTC",
        trigger_message="go",
        destination_kind=kind,  # type: ignore[arg-type]
        destination_id=destination_id,
    )
    await db_session.commit()
    text = "Done <!channel> ping <@U7>."
    return row.model_copy(update={"last_result_tail": text, "delivery_payload": text})


def _poster(
    sm: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    *,
    dm_policies: dict | None = None,
) -> tuple[Any, MagicMock]:
    client = MagicMock()
    client.chat_postMessage = AsyncMock()
    client.users_info = AsyncMock(return_value={"user": {"id": "U1", "team_id": "T_ROUTINES"}})
    client.conversations_open = AsyncMock(return_value={"channel": {"id": "D_CREATOR"}})
    client.conversations_info = AsyncMock(
        side_effect=lambda channel: {"channel": {"id": channel, "is_private": False}}
    )
    client.conversations_members = AsyncMock(return_value={"members": ["U1"]})
    client.conversations_replies = AsyncMock(return_value={"messages": [{"ts": "1717.5"}]})

    async def fake_resolve(runtime: object, *, team_id: str) -> object:
        assert team_id == "T_ROUTINES", "the client is built for the routine's own workspace"
        return client

    monkeypatch.setattr(poster_mod, "resolve_web_client", fake_resolve)
    runtime = cast(
        SlackRuntime,
        SimpleNamespace(
            sessionmaker=sm,
            anthropic=MagicMock(),
            settings=SimpleNamespace(
                direct_message_policies=dm_policies if dm_policies is not None else {}
            ),
            deployment_default=DeploymentDefault(),
        ),
    )
    return make_slack_routine_poster(runtime), client


async def test_posts_into_a_thread_without_broadcasting(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = await _routine(db_session, kind="thread", destination_id="C1:1717.5")
    post, client = _poster(db_session_factory, monkeypatch)

    outcome = await post(row)

    assert outcome.status == "delivered"
    kwargs = client.chat_postMessage.await_args.kwargs
    assert (kwargs["channel"], kwargs["thread_ts"]) == ("C1", "1717.5")
    client.conversations_replies.assert_awaited_once_with(channel="C1", ts="1717.5", limit=1)
    assert "<!channel>" not in kwargs["text"], "a routine never broadcasts"
    assert "<@U7>" in kwargs["text"], "mentions of people survive"


def _with_identity(monkeypatch: pytest.MonkeyPatch, identity: AgentIdentity) -> None:
    async def resolve(*args: object, **kwargs: Any) -> AgentIdentity:
        assert kwargs["platform"] == "slack" and kwargs["workspace_id"] == "T_ROUTINES"
        return identity

    monkeypatch.setattr(poster_mod, "resolve_routine_identity", resolve)


async def test_with_identity_the_result_posts_as_the_agent_and_still_names_it(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = await _routine(db_session, kind="channel", destination_id="C1", agent_name="research")
    post, client = _poster(db_session_factory, monkeypatch)
    client.token = "xoxb-routine-identity"
    _with_identity(
        monkeypatch, AgentIdentity(name="research", avatar_url="https://app/a.png", builtin=False)
    )

    assert (await post(row)).status == "delivered"

    kwargs = client.chat_postMessage.await_args.kwargs
    assert (kwargs["username"], kwargs["icon_url"]) == ("research", "https://app/a.png")
    assert kwargs["text"].startswith(
        "Result from research's Monday routine at 09:00 UTC:\n\nDone"
    ), "Slack can drop the header silently, so the text keeps the agent's name"
    assert "<!channel>" not in kwargs["text"], "a routine never broadcasts"


async def test_without_customize_scope_the_plain_post_keeps_the_agents_name(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from slack_sdk.errors import SlackApiError
    from slack_sdk.web.async_slack_response import AsyncSlackResponse

    row = await _routine(db_session, kind="channel", destination_id="C1", agent_name="research")
    post, client = _poster(db_session_factory, monkeypatch)
    client.token = "xoxb-routine-no-customize"
    response = MagicMock(spec=AsyncSlackResponse)
    response.data = {"ok": False, "error": "missing_scope", "needed": "chat:write.customize"}
    client.chat_postMessage = AsyncMock(side_effect=[SlackApiError("scope", response), None])
    _with_identity(monkeypatch, AgentIdentity(name="research", avatar_url=None, builtin=False))

    assert (await post(row)).status == "delivered"

    retry = client.chat_postMessage.await_args_list[-1].kwargs
    assert "username" not in retry
    assert retry["text"].startswith("Result from research's Monday routine at 09:00 UTC:")


async def test_the_built_in_agent_keeps_todays_text(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = await _routine(db_session, kind="channel", destination_id="C1")
    post, client = _poster(db_session_factory, monkeypatch)
    _with_identity(monkeypatch, AgentIdentity(name="daimon", avatar_url=None, builtin=True))

    assert (await post(row)).status == "delivered"

    kwargs = client.chat_postMessage.await_args.kwargs
    assert "username" not in kwargs
    assert kwargs["text"].startswith("Result from daimon's Monday routine at 09:00 UTC:")


async def test_a_protected_channel_is_refused(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = await _routine(db_session, kind="channel", destination_id="C_ANN")
    await set_access_policy(
        db_session,
        tenant_id=row.tenant_id,
        policy=TenantAccessPolicy(protected_channel_ids=("C_ANN",)),
    )
    await db_session.commit()
    post, client = _poster(db_session_factory, monkeypatch)

    outcome = await post(row)

    assert (outcome.status, outcome.note) == ("delivered", "dm_fallback:protected_channel")
    (call,) = client.chat_postMessage.await_args_list
    assert call.kwargs["channel"] == "D_CREATOR", "the result went to the creator, not C_ANN"
    assert "posting is blocked there" in call.kwargs["text"]


async def test_a_malformed_thread_destination_is_skipped(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = await _routine(db_session, kind="thread", destination_id="C1")
    post, client = _poster(db_session_factory, monkeypatch)

    outcome = await post(row)

    assert (outcome.status, outcome.note) == ("delivered", "dm_fallback:destination_unavailable")
    (call,) = client.chat_postMessage.await_args_list
    assert call.kwargs["channel"] == "D_CREATOR"


async def test_slack_refusing_the_channel_falls_back_to_a_dm(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from slack_sdk.errors import SlackApiError
    from slack_sdk.web.async_slack_response import AsyncSlackResponse

    row = await _routine(db_session, kind="channel", destination_id="C1")
    post, client = _poster(db_session_factory, monkeypatch)
    response = MagicMock(spec=AsyncSlackResponse)
    response.data = {"ok": False, "error": "not_in_channel"}

    async def post_message(**kwargs: object) -> object:
        if kwargs["channel"] == "C1":
            raise SlackApiError("not_in_channel", response)
        return {"ts": "1.0"}

    client.chat_postMessage = AsyncMock(side_effect=post_message)

    outcome = await post(row)

    assert (outcome.status, outcome.note) == ("delivered", "dm_fallback:destination_unavailable")


@pytest.mark.parametrize(
    "agent", ["daimon", "local"], ids=["outside-agent-posting-in", "own-agent"]
)
async def test_an_isolated_channels_routine_never_leaves_it(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    agent: str,
) -> None:
    from slack_sdk.errors import SlackApiError
    from slack_sdk.web.async_slack_response import AsyncSlackResponse

    row = await _routine(db_session, kind="channel", destination_id="C1")
    row = row.model_copy(update={"agent_name": agent})
    await set_fields(
        db_session,
        scope=ChannelScopeRef(tenant_id=row.tenant_id, channel_id="C1"),
        tenant_id=row.tenant_id,
        agent_name="local",
        mode="agent",
    )
    await set_access_policy(
        db_session,
        tenant_id=row.tenant_id,
        policy=TenantAccessPolicy(
            sealed_channel_ids=("C1",),
            isolated_channel_ids=("C1",),
            agent_channel_pins={"local": ("C1",)},
        ),
    )
    await db_session.commit()
    post, client = _poster(db_session_factory, monkeypatch)
    response = MagicMock(spec=AsyncSlackResponse)
    response.data = {"ok": False, "error": "not_in_channel"}
    client.chat_postMessage = AsyncMock(side_effect=SlackApiError("not_in_channel", response))

    outcome = await post(row)

    assert (outcome.status, outcome.note) == ("skipped", "destination_unavailable")
    client.conversations_open.assert_not_awaited()


async def test_no_dm_for_a_creator_the_dm_policy_excludes(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from daimon.core.config import DirectMessagePolicy

    row = await _routine(db_session, kind="thread", destination_id="C1")  # malformed thread
    _post, client = _poster(db_session_factory, monkeypatch)
    runtime = cast(
        SlackRuntime,
        SimpleNamespace(
            sessionmaker=db_session_factory,
            anthropic=MagicMock(),
            settings=SimpleNamespace(
                direct_message_policies={row.tenant_id: DirectMessagePolicy(mode="disabled")}
            ),
            deployment_default=DeploymentDefault(),
        ),
    )

    outcome = await make_slack_routine_poster(runtime)(row)

    assert (outcome.status, outcome.note) == ("skipped", "destination_unavailable")
    client.chat_postMessage.assert_not_awaited()


@pytest.mark.parametrize(
    ("destination_id", "kind", "policy", "unreadable", "note"),
    [
        (
            "C_ANN",
            "channel",
            TenantAccessPolicy(protected_channel_ids=("C_ANN",), invoker_user_ids=("OTHER",)),
            False,
            "invoker_not_allowed",
        ),
        (
            "C1",
            "thread",
            TenantAccessPolicy(invoker_user_ids=("OTHER",)),
            False,
            "invoker_not_allowed",
        ),
        ("C1", "thread", None, True, "access_policy_unreadable"),
    ],
)
async def test_a_creator_who_is_not_cleared_gets_nothing_not_even_a_dm(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    destination_id: str,
    kind: str,
    policy: TenantAccessPolicy | None,
    unreadable: bool,
    note: str,
) -> None:
    """Review regression (round 2): protected/unavailable (a malformed thread
    here) must not become a DM for a creator the policy no longer clears."""
    import daimon.core.routine_delivery as delivery_mod
    from daimon.core.stores.access_policy import AccessPolicyUnreadable

    row = await _routine(db_session, kind=kind, destination_id=destination_id)
    if policy is not None:
        await set_access_policy(db_session, tenant_id=row.tenant_id, policy=policy)
        await db_session.commit()
    if unreadable:

        async def unreadable_policy(*args: object, **kwargs: object) -> TenantAccessPolicy:
            raise AccessPolicyUnreadable(tenant_id=row.tenant_id)

        monkeypatch.setattr(delivery_mod, "load_access_policy", unreadable_policy)
    post, client = _poster(db_session_factory, monkeypatch)

    outcome = await post(row)

    assert (outcome.status, outcome.note) == ("skipped", note)
    client.chat_postMessage.assert_not_awaited()
    client.conversations_open.assert_not_awaited()


async def test_a_private_channel_the_creator_is_not_in_gets_their_dm_not_a_post(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review regression (round 3): daimon is in the private channel, the
    creator is not — the routine must not post there for them."""
    row = await _routine(db_session, kind="channel", destination_id="C_PRIV")
    post, client = _poster(db_session_factory, monkeypatch)
    client.conversations_info = AsyncMock(
        return_value={"channel": {"id": "C_PRIV", "is_private": True}}
    )
    client.conversations_members = AsyncMock(return_value={"members": ["U_SOMEONE_ELSE"]})

    outcome = await post(row)

    assert outcome.note == "dm_fallback:creator_cannot_post"
    (call,) = client.chat_postMessage.await_args_list
    assert call.kwargs["channel"] == "D_CREATOR", "only the creator's own DM, never C_PRIV"


@pytest.mark.parametrize("deleted_as", ["error", "empty"])
async def test_a_deleted_thread_is_not_posted_at_the_channel_root(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    deleted_as: str,
) -> None:
    """Review regression (round 3): chat.postMessage accepts a missing
    thread_ts and posts at the channel root; the thread is checked first."""
    from slack_sdk.errors import SlackApiError
    from slack_sdk.web.async_slack_response import AsyncSlackResponse

    row = await _routine(db_session, kind="thread", destination_id="C1:1717.5")
    post, client = _poster(db_session_factory, monkeypatch)
    if deleted_as == "error":
        response = MagicMock(spec=AsyncSlackResponse)
        response.data = {"ok": False, "error": "thread_not_found"}
        client.conversations_replies = AsyncMock(
            side_effect=SlackApiError("thread_not_found", response)
        )
    else:
        client.conversations_replies = AsyncMock(return_value={"messages": []})

    outcome = await post(row)

    assert outcome.note == "dm_fallback:destination_unavailable"
    channels = [c.kwargs["channel"] for c in client.chat_postMessage.await_args_list]
    assert channels == ["D_CREATOR"], "nothing posted to C1"


@pytest.mark.parametrize("fallback", [False, True])
async def test_long_routine_delivers_every_word_to_thread_or_creator(
    db_session, db_session_factory, monkeypatch, fallback
):
    row = await _routine(db_session, kind="thread", destination_id="C1:1717.5")
    result = " ".join(f"digestword{i}" for i in range(3000)) + " FINAL-COMPLETE"
    row = row.model_copy(update={"delivery_payload": result})
    post, client = _poster(db_session_factory, monkeypatch)
    if fallback:
        client.conversations_replies = AsyncMock(return_value={"messages": []})
    outcome = await post(row)
    assert outcome.status == "delivered"
    calls = client.chat_postMessage.await_args_list
    assert len(calls) > 1 and all(len(call.kwargs["text"]) <= 11800 for call in calls)
    assert "".join(call.kwargs["text"] for call in calls).split("\n\n")[-1] == result
    expected_channel = "D_CREATOR" if fallback else "C1"
    assert all(call.kwargs["channel"] == expected_channel for call in calls)
    if not fallback:
        assert all(call.kwargs["thread_ts"] == "1717.5" for call in calls)


async def test_dm_row_is_explicitly_refused_without_channel_or_dm_io(
    db_session, db_session_factory, monkeypatch
):
    row = await _routine(db_session, kind="channel", destination_id="C1")
    row = row.model_copy(update={"destination_kind": "dm", "destination_id": "U1"})
    post, client = _poster(db_session_factory, monkeypatch)
    result = await post(row)
    assert result.status == "skipped" and "not supported here" in result.note
    client.chat_postMessage.assert_not_awaited()
    client.conversations_open.assert_not_awaited()


@pytest.mark.parametrize("fallback", [False, True])
async def test_generated_files_use_existing_slack_upload_path(
    db_session, db_session_factory, monkeypatch, fallback
):
    import httpx
    from daimon.core.routine_files import OUTBOX_FILES, PREFIX, RoutineFile, RoutinePayload
    from daimon.testing.ma import MARouter
    from mux.testing.drivers.anthropic.ma import build_fake_anthropic

    row = await _routine(db_session, kind="thread", destination_id="C1:1717.5")
    row = row.model_copy(
        update={
            "delivery_note": OUTBOX_FILES,
            "delivery_payload": PREFIX
            + RoutinePayload(
                text="Report",
                files=[
                    RoutineFile(
                        file_id="file_report",
                        filename="report.csv",
                        mime_type="text/csv",
                        size_bytes=4,
                    )
                ],
            ).model_dump_json(),
        }
    )
    router = MARouter()
    router.add(
        "GET", r"/v1/files/([^/]+)/content", lambda r, m: httpx.Response(200, content=b"data")
    )
    provider = build_fake_anthropic(router.dispatch)
    original = poster_mod.deliver_routine_files

    async def deliver(_client, *args, **kwargs):
        await original(provider, *args, **kwargs)

    monkeypatch.setattr(poster_mod, "deliver_routine_files", deliver)
    post, client = _poster(db_session_factory, monkeypatch)
    client.files_upload_v2 = AsyncMock()
    if fallback:
        client.conversations_replies = AsyncMock(return_value={"messages": []})
    outcome = await post(row)
    assert outcome.status == "delivered"
    client.files_upload_v2.assert_awaited_once()
    upload = client.files_upload_v2.await_args.kwargs
    assert upload["content"] == b"data"
    assert upload["channel"] == ("D_CREATOR" if fallback else "C1")
    assert upload["thread_ts"] == (None if fallback else "1717.5")


@pytest.mark.parametrize(
    "revocation", ["dm_disabled", "isolation", "dm_policy", "recipient", "signed_error"]
)
async def test_fallback_files_recheck_privacy_after_download(
    db_session, db_session_factory, monkeypatch, revocation
):
    from datetime import UTC, datetime

    from daimon.core.access_policy import ChannelRule
    from daimon.core.config import DirectMessagePolicy
    from daimon.core.routine_delivery import poll_deliveries_once
    from daimon.core.routine_files import PREFIX, RoutineFile, RoutinePayload
    from daimon.core.stores.direct_messages import set_dm_enabled
    from daimon.core.stores.routines import get_routine, record_result
    from slack_sdk.errors import SlackApiError
    from structlog.testing import capture_logs

    row = await _routine(db_session, kind="thread", destination_id="C1:1717.5")
    payload = (
        PREFIX
        + RoutinePayload(
            text="Report",
            files=[
                RoutineFile(
                    file_id="file_report", filename="report.csv", mime_type="text/csv", size_bytes=4
                )
            ],
        ).model_dump_json()
    )
    async with db_session_factory.begin() as session:
        await record_result(
            session, row.id, tail="Report", error=None, delivery="pending", delivery_payload=payload
        )
    dm_policies = {}
    post, client = _poster(db_session_factory, monkeypatch, dm_policies=dm_policies)
    client.conversations_replies = AsyncMock(return_value={"messages": []})
    client.files_upload_v2 = AsyncMock()
    downloads = []
    completed = []
    sentinel = "https://upload.invalid/signed?token=secret-sentinel"

    async def download(*args, **kwargs):
        downloads.append(True)
        async with db_session_factory.begin() as session:
            if revocation == "dm_disabled":
                await set_dm_enabled(session, tenant_id=row.tenant_id, enabled=False)
            elif revocation == "isolation":
                await set_access_policy(
                    session,
                    tenant_id=row.tenant_id,
                    policy=TenantAccessPolicy(
                        channel_rules={"C1": ChannelRule(readers="own", writers="own")}
                    ),
                )
        if revocation == "dm_policy":
            dm_policies[row.tenant_id] = DirectMessagePolicy(mode="disabled")
        if revocation == "recipient":
            client.users_info = AsyncMock(
                return_value={"user": {"team_id": "T_ROUTINES", "deleted": True}}
            )
        if revocation == "signed_error":
            raise SlackApiError(sentinel, {"ok": False, "error": sentinel})
        completed.append(True)
        return b"data"

    monkeypatch.setattr("daimon.core.routine_files.download_output_file", download)
    with capture_logs() as logs:
        await poll_deliveries_once(
            db_session_factory, platform="slack", post=post, now=datetime.now(UTC)
        )
    assert downloads == [True]
    client.files_upload_v2.assert_not_awaited()
    assert completed == ([] if revocation == "signed_error" else [True])
    assert sentinel not in str(logs)
    async with db_session_factory() as session:
        after = await get_routine(session, row.id, tenant_id=row.tenant_id)
    assert after.delivery_status == "skipped"


@pytest.mark.parametrize("fallback", [False, True])
async def test_upload_failure_does_not_log_signed_url(
    db_session, db_session_factory, monkeypatch, fallback
):
    from datetime import UTC, datetime

    from daimon.core.routine_delivery import poll_deliveries_once
    from daimon.core.routine_files import PREFIX, RoutineFile, RoutinePayload
    from daimon.core.stores.routines import get_routine, record_result
    from structlog.testing import capture_logs

    row = await _routine(db_session, kind="thread", destination_id="C1:1717.5")
    payload = (
        PREFIX
        + RoutinePayload(
            text="Report",
            files=[
                RoutineFile(
                    file_id="file_report", filename="report.csv", mime_type="text/csv", size_bytes=4
                )
            ],
        ).model_dump_json()
    )
    async with db_session_factory.begin() as session:
        await record_result(
            session, row.id, tail="Report", error=None, delivery="pending", delivery_payload=payload
        )
    post, client = _poster(db_session_factory, monkeypatch)
    if fallback:
        client.conversations_replies = AsyncMock(return_value={"messages": []})
    sentinel = "https://upload.invalid/signed?token=secret-sentinel"
    client.files_upload_v2 = AsyncMock(side_effect=RuntimeError(sentinel))
    monkeypatch.setattr(
        "daimon.core.routine_files.download_output_file", AsyncMock(return_value=b"data")
    )
    with capture_logs() as logs:
        await poll_deliveries_once(
            db_session_factory, platform="slack", post=post, now=datetime.now(UTC)
        )
    client.files_upload_v2.assert_awaited_once()
    assert sentinel not in str(logs)
    failures = [entry for entry in logs if entry["event"] == "routine.delivery_failed"]
    assert failures[0]["error_type"] == "RuntimeError"
    async with db_session_factory() as session:
        after = await get_routine(session, row.id, tenant_id=row.tenant_id)
    assert after.delivery_status == "skipped"


async def test_a_shared_channels_routine_never_leaves_it_by_dm(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Its fire ran held to the channel, so its result goes there or nowhere."""
    from daimon.core.stores.shared_channels import mark_channel_shared
    from slack_sdk.errors import SlackApiError
    from slack_sdk.web.async_slack_response import AsyncSlackResponse

    row = await _routine(db_session, kind="channel", destination_id="C1")
    await mark_channel_shared(
        db_session, tenant_id=row.tenant_id, platform="slack", channel_id="C1", source="platform"
    )
    await db_session.commit()
    post, client = _poster(db_session_factory, monkeypatch)
    response = MagicMock(spec=AsyncSlackResponse)
    response.data = {"ok": False, "error": "not_in_channel"}
    client.chat_postMessage = AsyncMock(side_effect=SlackApiError("not_in_channel", response))

    outcome = await post(row)

    assert (outcome.status, outcome.note) == ("skipped", "destination_unavailable")
    client.conversations_open.assert_not_awaited()
