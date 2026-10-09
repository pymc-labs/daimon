"""One pending GitHub access request per asker, agent and thread."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from daimon.core._models import Account, GitHubAccessRequest, Tenant, TenantGitHubRepo
from daimon.core.github_request_cards import admin_card, requester_card
from daimon.core.github_request_expiry import poll_expired_requests_once
from daimon.core.stores import github_access, github_app_installations
from daimon.core.stores.github_access_requests import (
    cancel_request,
    claim_due_expiry_group,
    dismiss_delivery,
    get_delivery,
    get_request,
    list_asker_requests,
    list_waiting,
    mark_expiry_notice_sent,
    ready_and_continue,
    record_delivery,
    record_reposted_requester_card,
    record_shared_card,
    request_access,
    set_status,
    shared_card_mention_allowed,
)
from daimon.core.stores.github_request_actions import (
    approve_connected_request,
    approve_connection_request,
    finish_confirmed_requests,
)
from daimon.core.stores.task_continuations import get_continuation
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.mark.asyncio
async def test_shared_request_cards_cap_mentions_per_thread(db_session: AsyncSession) -> None:
    tenant_id, agent_id = uuid.uuid4(), uuid.uuid4()
    db_session.add(Tenant(id=tenant_id, platform="slack", external_id="T1"))
    await db_session.flush()
    requests = []
    for index in range(4):
        account_id = uuid.uuid4()
        db_session.add(Account(id=account_id, tenant_id=tenant_id, role="user"))
        await db_session.flush()
        requests.append(
            await request_access(
                db_session,
                tenant_id=tenant_id,
                requester_account_id=account_id,
                requester_platform_user_id=f"U{index}",
                platform="slack",
                parent_channel_id="C1",
                thread_id="123.456",
                agent_id=agent_id,
                ma_agent_id="ma-agent",
                agent_name="Helper",
                repo_name="private/repo",
                requested_work="Continue this task",
                is_admin=False,
            )
        )
    for request in requests[:3]:
        assert await shared_card_mention_allowed(db_session, request=request)
        await record_shared_card(
            db_session, request=request, message_id=str(request.id), mentioned=True
        )
        await db_session.flush()
    assert not await shared_card_mention_allowed(db_session, request=requests[3])
    await record_shared_card(db_session, request=requests[0], message_id="updated", mentioned=False)
    await db_session.flush()
    assert not await shared_card_mention_allowed(db_session, request=requests[3])


@pytest.mark.asyncio
async def test_declined_slack_requester_repost_rebinds_buttons(db_session: AsyncSession) -> None:
    tenant_id, account_id = uuid.uuid4(), uuid.uuid4()
    db_session.add(Tenant(id=tenant_id, platform="slack", external_id="T2"))
    await db_session.flush()
    db_session.add(Account(id=account_id, tenant_id=tenant_id, role="user"))
    await db_session.flush()
    request = await request_access(
        db_session,
        tenant_id=tenant_id,
        requester_account_id=account_id,
        requester_platform_user_id="U2",
        platform="slack",
        parent_channel_id="C2",
        thread_id="123.1",
        agent_id=uuid.uuid4(),
        ma_agent_id="agent",
        agent_name="Helper",
        repo_name="private/repo",
        requested_work="Continue task",
        is_admin=False,
    )
    assert await record_delivery(
        db_session,
        tenant_id=tenant_id,
        request_id=request.id,
        recipient_account_id=account_id,
        platform_user_id="U2",
        message_id="123.2",
    )
    assert await set_status(
        db_session,
        tenant_id=tenant_id,
        request_id=request.id,
        expected="open",
        status="declined",
    )
    assert await record_reposted_requester_card(
        db_session,
        tenant_id=tenant_id,
        request_id=request.id,
        recipient_account_id=account_id,
        message_id="123.3",
    )
    delivery = await get_delivery(
        db_session,
        tenant_id=tenant_id,
        request_id=request.id,
        recipient_account_id=account_id,
    )
    assert delivery is not None and delivery.message_id == "123.3"


@pytest.mark.asyncio
async def test_request_collects_repos_and_is_asker_bound(db_session: AsyncSession) -> None:
    tenant_id, asker_id, other_id, agent_id = (uuid.uuid4() for _ in range(4))
    db_session.add(Tenant(id=tenant_id, platform="discord", external_id=str(tenant_id)))
    await db_session.flush()
    db_session.add(Account(id=asker_id, tenant_id=tenant_id, role="user"))
    db_session.add(Account(id=other_id, tenant_id=tenant_id, role="user"))
    await db_session.flush()
    now = datetime.now(UTC)
    arguments: dict[str, Any] = dict(
        tenant_id=tenant_id,
        requester_account_id=asker_id,
        requester_platform_user_id="person",
        platform="discord",
        parent_channel_id="channel",
        thread_id="thread",
        agent_id=agent_id,
        ma_agent_id="ag_helper",
        agent_name="Helper",
        requested_work="Continue the report",
        is_admin=False,
        now=now,
    )
    first = await request_access(
        db_session, repo_name="example/first", required_ability="write", **arguments
    )
    second = await request_access(db_session, repo_name="example/second", **arguments)
    repeated = await request_access(db_session, repo_name="example/first", **arguments)
    assert len({first.id, second.id, repeated.id}) == 3
    assert second.repo_names == ["example/second"]
    assert second.required_ability == "read"
    assert repeated.repo_names == ["example/first"]
    assert second.expires_at == now + timedelta(days=7)
    private = requester_card(
        second,
        connected_names=(),
        asker_is_admin=True,
        ability="write",
    )
    assert private.primary == "Connect and add"
    assert "example/first" not in private.text
    admin = admin_card(
        second,
        connected_names=(),
        channel_label="#work",
        requester_label="Someone",
        ability="write",
    )
    assert admin.primary == "Connect and add"
    assert "example/first" not in admin.text
    assert admin.secondary == ("Decline", "Hide for me")
    connected = requester_card(
        second,
        connected_names=("example/second",),
        asker_is_admin=True,
        ability="write",
    )
    assert connected.primary == "Add repo"
    assert "example/second" in connected.text
    assert (
        requester_card(
            second,
            connected_names=(),
            asker_is_admin=False,
            ability="read",
            admin_status="unseen",
        ).text
        == "No admin can see this channel. Ask an admin to open /github."
    )
    assert (
        requester_card(
            second,
            connected_names=(),
            asker_is_admin=False,
            ability="read",
            admin_status="recent",
        ).text
        == "Admins were notified recently."
    )
    assert await record_delivery(
        db_session,
        tenant_id=tenant_id,
        request_id=repeated.id,
        recipient_account_id=other_id,
        platform_user_id="admin",
        message_id="dm-1",
    )
    delivered = await get_delivery(
        db_session, tenant_id=tenant_id, request_id=repeated.id, recipient_account_id=other_id
    )
    assert delivered is not None and delivered.message_id == "dm-1"
    assert not await record_delivery(
        db_session,
        tenant_id=tenant_id,
        request_id=repeated.id,
        recipient_account_id=other_id,
        platform_user_id="admin",
        message_id="dm-2",
    )
    request_row = await db_session.get(GitHubAccessRequest, repeated.id)
    assert request_row is not None
    request_row.platform = "slack"
    assert await record_delivery(
        db_session,
        tenant_id=tenant_id,
        request_id=repeated.id,
        recipient_account_id=other_id,
        platform_user_id="admin",
        message_id="ephemeral-2",
    )
    delivered = await get_delivery(
        db_session, tenant_id=tenant_id, request_id=repeated.id, recipient_account_id=other_id
    )
    assert delivered is not None and delivered.message_id == "ephemeral-2"
    assert await dismiss_delivery(
        db_session, tenant_id=tenant_id, request_id=repeated.id, account_id=other_id
    )
    assert not await record_delivery(
        db_session,
        tenant_id=tenant_id,
        request_id=repeated.id,
        recipient_account_id=other_id,
        platform_user_id="admin",
        message_id="dm-1",
    )
    assert (
        len(
            await list_waiting(
                db_session, tenant_id=tenant_id, visible_agent_ids=frozenset({agent_id}), now=now
            )
        )
        == 1
    )
    assert (
        await list_waiting(db_session, tenant_id=tenant_id, visible_agent_ids=frozenset(), now=now)
        == []
    )
    assert len(await list_asker_requests(db_session, tenant_id=tenant_id, account_id=asker_id)) == 1
    assert not await cancel_request(
        db_session, tenant_id=tenant_id, request_id=first.id, account_id=other_id
    )
    assert await cancel_request(
        db_session, tenant_id=tenant_id, request_id=repeated.id, account_id=asker_id
    )
    assert not await set_status(
        db_session, tenant_id=tenant_id, request_id=first.id, expected="open", status="ready"
    )
    assert (
        await list_waiting(
            db_session, tenant_id=tenant_id, visible_agent_ids=frozenset({agent_id}), now=now
        )
        == []
    )
    # Cancelled requests still count against the 24-hour cap.
    with pytest.raises(ValueError, match="already have requests"):
        await request_access(db_session, repo_name="example/third", **arguments)
    assert not await cancel_request(
        db_session, tenant_id=tenant_id, request_id=first.id, account_id=asker_id
    )


@pytest.mark.asyncio
async def test_ability_change_replaces_waiting_request_without_promoting_old_one(
    db_session: AsyncSession,
) -> None:
    tenant_id, asker_id, admin_id, agent_id = (uuid.uuid4() for _ in range(4))
    db_session.add(Tenant(id=tenant_id, platform="discord", external_id=str(tenant_id)))
    await db_session.flush()
    db_session.add_all(
        [
            Account(id=asker_id, tenant_id=tenant_id, role="user"),
            Account(id=admin_id, tenant_id=tenant_id, role="admin"),
        ]
    )
    await db_session.flush()
    args: dict[str, Any] = dict(
        tenant_id=tenant_id,
        requester_account_id=asker_id,
        requester_platform_user_id="person",
        platform="discord",
        parent_channel_id="channel",
        thread_id="thread",
        agent_id=agent_id,
        ma_agent_id="ag_helper",
        agent_name="Helper",
        repo_name="example/repo",
        requested_work="Continue",
        is_admin=False,
    )
    old = await request_access(db_session, required_ability="read", **args)
    assert await approve_connection_request(
        db_session, tenant_id=tenant_id, request_id=old.id, account_id=admin_id
    )
    fresh = await request_access(db_session, required_ability="write", **args)
    assert fresh.id != old.id and fresh.required_ability == "write"
    old_row = await get_request(db_session, tenant_id=tenant_id, request_id=old.id)
    assert old_row is not None
    assert old_row.status == "cancelled" and old_row.required_ability == "read"


@pytest.mark.asyncio
async def test_only_tenant_admin_can_approve_connected_request(db_session: AsyncSession) -> None:
    tenant_id, asker_id, channel_admin_id, agent_id = (uuid.uuid4() for _ in range(4))
    db_session.add(Tenant(id=tenant_id, platform="discord", external_id=str(tenant_id)))
    await db_session.flush()
    db_session.add_all(
        [
            Account(id=asker_id, tenant_id=tenant_id, role="user"),
            Account(id=channel_admin_id, tenant_id=tenant_id, role="user"),
        ]
    )
    await db_session.flush()
    request = await request_access(
        db_session,
        tenant_id=tenant_id,
        requester_account_id=asker_id,
        requester_platform_user_id="person",
        platform="discord",
        parent_channel_id="channel",
        thread_id="thread",
        agent_id=agent_id,
        ma_agent_id="ag_helper",
        agent_name="Helper",
        repo_name="example/repo",
        requested_work="Continue",
        is_admin=False,
    )
    with pytest.raises(ValueError, match="Only a server or workspace admin"):
        await approve_connected_request(
            db_session,
            tenant_id=tenant_id,
            request_id=request.id,
            account_id=channel_admin_id,
        )
    assert (
        await get_request(db_session, tenant_id=tenant_id, request_id=request.id)
    ).status == "open"


@pytest.mark.asyncio
async def test_expiry_groups_one_thread_and_records_one_notice(db_session: AsyncSession) -> None:
    tenant_id, asker_id, first_agent, second_agent = (uuid.uuid4() for _ in range(4))
    db_session.add(Tenant(id=tenant_id, platform="discord", external_id=str(tenant_id)))
    await db_session.flush()
    db_session.add(Account(id=asker_id, tenant_id=tenant_id, role="user"))
    await db_session.flush()
    now = datetime.now(UTC)
    requests = []
    for agent_id in (first_agent, second_agent):
        requests.append(
            await request_access(
                db_session,
                tenant_id=tenant_id,
                requester_account_id=asker_id,
                requester_platform_user_id="person",
                platform="discord",
                parent_channel_id="channel",
                thread_id="one-thread",
                agent_id=agent_id,
                ma_agent_id=f"ag_{agent_id}",
                agent_name="Helper",
                repo_name="example/repo",
                requested_work="Continue the report",
                is_admin=True,
                now=now - timedelta(days=8),
            )
        )
    group = await claim_due_expiry_group(db_session, platform="discord", now=now)
    assert {row.id for row in group} == {row.id for row in requests}
    assert all(row.status == "expired" for row in group)
    await mark_expiry_notice_sent(db_session, request_ids=tuple(row.id for row in group), now=now)
    assert await claim_due_expiry_group(db_session, platform="discord", now=now) == []


@pytest.mark.asyncio
async def test_expiry_poller_posts_once_for_thread(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, asker_id, agent_id = (uuid.uuid4() for _ in range(3))
    db_session.add(Tenant(id=tenant_id, platform="discord", external_id=str(tenant_id)))
    await db_session.flush()
    db_session.add(Account(id=asker_id, tenant_id=tenant_id, role="user"))
    await db_session.flush()
    now = datetime.now(UTC)
    await request_access(
        db_session,
        tenant_id=tenant_id,
        requester_account_id=asker_id,
        requester_platform_user_id="person",
        platform="discord",
        parent_channel_id="channel",
        thread_id="thread",
        agent_id=agent_id,
        ma_agent_id="ag_helper",
        agent_name="Helper",
        repo_name="example/repo",
        requested_work="Continue the report",
        is_admin=True,
        now=now - timedelta(days=8),
    )
    posted: list[str] = []

    async def post(request: Any) -> bool:
        posted.append(request.thread_id)
        return True

    assert (
        await poll_expired_requests_once(db_session_factory, platform="discord", post=post, now=now)
        == 1
    )
    assert (
        await poll_expired_requests_once(db_session_factory, platform="discord", post=post, now=now)
        == 0
    )
    assert posted == ["thread"]


@pytest.mark.asyncio
async def test_ready_request_enqueues_one_unfinished_turn(db_session: AsyncSession) -> None:
    tenant_id, asker_id, agent_id = (uuid.uuid4() for _ in range(3))
    db_session.add(Tenant(id=tenant_id, platform="discord", external_id=str(tenant_id)))
    await db_session.flush()
    db_session.add(Account(id=asker_id, tenant_id=tenant_id, role="user"))
    await db_session.flush()
    request = await request_access(
        db_session,
        tenant_id=tenant_id,
        requester_account_id=asker_id,
        requester_platform_user_id="person",
        platform="discord",
        parent_channel_id="channel",
        thread_id="thread",
        agent_id=agent_id,
        ma_agent_id="ag_helper",
        agent_name="Helper",
        repo_name="example/repo",
        requested_work="Finish the remaining report steps",
        is_admin=False,
    )
    assert await ready_and_continue(db_session, tenant_id=tenant_id, request_id=request.id)
    assert not await ready_and_continue(db_session, tenant_id=tenant_id, request_id=request.id)
    wake = await get_continuation(db_session, idempotency_key=request.id)
    assert wake is not None
    assert wake.reason == "github_access_ready"
    assert wake.target_ma_agent_id == "ag_helper"
    assert wake.requested_work == "Finish the remaining report steps"


@pytest.mark.asyncio
async def test_confirmed_connection_applies_one_admin_decision(db_session: AsyncSession) -> None:
    tenant_id, asker_id, admin_id, agent_id = (uuid.uuid4() for _ in range(4))
    db_session.add(Tenant(id=tenant_id, platform="discord", external_id=str(tenant_id)))
    await db_session.flush()
    db_session.add(Account(id=asker_id, tenant_id=tenant_id, role="user"))
    db_session.add(Account(id=admin_id, tenant_id=tenant_id, role="admin"))
    await db_session.flush()
    request = await request_access(
        db_session,
        tenant_id=tenant_id,
        requester_account_id=asker_id,
        requester_platform_user_id="person",
        platform="discord",
        parent_channel_id="channel",
        thread_id="thread",
        agent_id=agent_id,
        ma_agent_id="ag_helper",
        agent_name="Helper",
        repo_name="example/repo",
        requested_work="Finish the remaining report steps",
        required_ability="write",
        is_admin=False,
    )
    assert await approve_connection_request(
        db_session, tenant_id=tenant_id, request_id=request.id, account_id=admin_id
    )
    await github_app_installations.upsert(
        db_session,
        installation_id=701,
        account_login="example",
        repo_full_names=["example/repo"],
    )
    db_session.add(
        TenantGitHubRepo(
            tenant_id=tenant_id,
            repo_id=701,
            owner_id=7,
            installation_id=701,
            repo_full_name="example/repo",
            max_access="write",
            authorized_by_github_user_id=7,
            status="active",
            version=1,
        )
    )
    await db_session.flush()
    assert (
        await finish_confirmed_requests(
            db_session, tenant_id=tenant_id, approved_by_account_id=admin_id
        )
        == 1
    )
    assert (
        await finish_confirmed_requests(
            db_session, tenant_id=tenant_id, approved_by_account_id=admin_id
        )
        == 0
    )
    grants = await github_access.list_agent_grants(
        db_session, tenant_id=tenant_id, agent_id=agent_id
    )
    assert len(grants) == 1
    assert grants[0].baseline_access == "write"
    assert not grants[0].staged
    assert await get_continuation(db_session, idempotency_key=request.id) is None
    grant_wake = await get_continuation(
        db_session, idempotency_key=uuid.uuid5(request.id, "agent-grant")
    )
    assert grant_wake is not None and grant_wake.reason == "github_access_ready"
    assert (
        await list_waiting(db_session, tenant_id=tenant_id, visible_agent_ids=frozenset({agent_id}))
        == []
    )
    assert [
        row.id
        for row in await list_asker_requests(db_session, tenant_id=tenant_id, account_id=asker_id)
    ] == [request.id]
    assert await ready_and_continue(db_session, tenant_id=tenant_id, request_id=request.id)
    wake = await get_continuation(db_session, idempotency_key=request.id)
    assert wake is not None and wake.reason == "github_access_ready"


@pytest.mark.asyncio
async def test_request_cap_and_external_refusal(db_session: AsyncSession) -> None:
    tenant_id, asker_id, agent_id = (uuid.uuid4() for _ in range(3))
    db_session.add(Tenant(id=tenant_id, platform="slack", external_id=str(tenant_id)))
    await db_session.flush()
    db_session.add(Account(id=asker_id, tenant_id=tenant_id, role="user"))
    await db_session.flush()
    args: dict[str, Any] = dict(
        tenant_id=tenant_id,
        requester_account_id=asker_id,
        requester_platform_user_id="U",
        platform="slack",
        parent_channel_id="C",
        agent_id=agent_id,
        ma_agent_id="ag_helper",
        agent_name="Helper",
        repo_name="example/repo",
        requested_work="Continue the report",
        is_admin=False,
    )
    for index in range(3):
        await request_access(db_session, thread_id=f"thread-{index}", **args)
    with pytest.raises(ValueError, match="already have requests"):
        await request_access(db_session, thread_id="thread-4", **args)
    account = await db_session.get(Account, asker_id)
    assert account is not None
    account.is_external = True
    await db_session.flush()
    with pytest.raises(ValueError, match="unavailable"):
        await request_access(db_session, thread_id="thread-5", **args)


@pytest.mark.asyncio
async def test_expired_card_cannot_decide_new_request(db_session: AsyncSession) -> None:
    tenant_id, asker_id, agent_id = (uuid.uuid4() for _ in range(3))
    db_session.add(Tenant(id=tenant_id, platform="discord", external_id=str(tenant_id)))
    await db_session.flush()
    db_session.add(Account(id=asker_id, tenant_id=tenant_id, role="user"))
    await db_session.flush()
    now = datetime.now(UTC)
    args: dict[str, Any] = dict(
        tenant_id=tenant_id,
        requester_account_id=asker_id,
        requester_platform_user_id="person",
        platform="discord",
        parent_channel_id="channel",
        thread_id="thread",
        agent_id=agent_id,
        ma_agent_id="ag_helper",
        agent_name="Helper",
        repo_name="example/repo",
        requested_work=None,
        is_admin=False,
    )
    old = await request_access(db_session, now=now, **args)
    later = now + timedelta(days=8)
    fresh = await request_access(db_session, now=later, **args)
    assert old.id != fresh.id
    assert not await set_status(
        db_session,
        tenant_id=tenant_id,
        request_id=old.id,
        expected="open",
        status="ready",
        now=later,
    )
    assert await set_status(
        db_session,
        tenant_id=tenant_id,
        request_id=fresh.id,
        expected="open",
        status="ready",
        now=later,
    )
