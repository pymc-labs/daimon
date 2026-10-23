"""Replay the tool-rule approval card races against the live rule store."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest
from daimon.core._models import AgentToolRuleHistoryRecord, Tenant
from daimon.core.authz import Subject
from daimon.core.confirmation import ConfirmationPrompt, ToolRuleCardReference
from daimon.core.posted_controls.confirmation import (
    ConfirmationHow,
    build_confirmation_blocks,
    build_confirmation_card,
    confirmation_custom_id,
    parse_always_custom_id,
    parse_confirmation_custom_id,
)
from daimon.core.posted_controls.lifecycle import PostedConfirmations
from daimon.core.posted_controls.tool_rule_confirmation import (
    SAVE_FAILED_MESSAGE,
    allowed_rule_scope,
    save_always_allow,
    snapshot_tool_rule,
)
from daimon.core.stores import agent_tool_rules as store
from daimon.testing.db import build_test_engine
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql.asyncpg import AsyncAdapt_asyncpg_connection
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _prompt(tenant_id: uuid.UUID, *, channel: str | None = "home") -> ConfirmationPrompt:
    return ConfirmationPrompt(
        title="Append note?",
        requester_platform_user_id="requester",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        tool_rules_enabled=True,
        tool_rule=ToolRuleCardReference(
            tenant_id=tenant_id,
            agent_id="ma-1",
            agent_name="Helper",
            tool_key="remote/append_note",
            server_url="https://example.test/mcp",
            channel_id=channel,
        ),
    )


def test_save_failure_notice_copy() -> None:
    assert SAVE_FAILED_MESSAGE == (
        "Approved this time only.\nAlways allow wasn't saved. Try again on the next card."
    )


def test_flag_off_card_stays_legacy_and_flag_on_card_names_the_actor() -> None:
    prompt = _prompt(uuid.uuid4())
    legacy = prompt.model_copy(update={"tool_rule": None, "tool_rules_enabled": False})
    card = build_confirmation_card(legacy, state="pending", token="tok_abcdefgh")
    assert card.model_dump_json() == (
        '{"state":"pending","headline":"Append note?","detail_lines":[],"consequence":null,'
        '"body":null,"footer":"Only {requester} can approve or deny\\nExpires {expires}",'
        '"token":"tok_abcdefgh"}'
    )
    assert "allow_always" not in card.model_dump()
    assert [
        b["action_id"]
        for block in build_confirmation_blocks(card, prompt=legacy)
        if block["type"] == "actions"
        for b in block["elements"]
    ] == [
        "dcf:tok_abcdefgh:approve",
        "dcf:tok_abcdefgh:deny",
        "dcf:tok_abcdefgh:details",
    ]
    assert build_confirmation_card(legacy, state="approved").footer == "by {requester}"
    enabled = build_confirmation_card(
        prompt, state="pending", token="tok_abcdefgh", allow_always=True
    )
    assert [
        b["text"]["text"]
        for block in build_confirmation_blocks(enabled, prompt=prompt)
        if block["type"] == "actions"
        for b in block["elements"]
    ] == ["Approve", "Deny", "Always allow", "Details"]
    always = confirmation_custom_id("tok_abcdefgh", "always")
    assert parse_always_custom_id(always) == "tok_abcdefgh"
    assert parse_confirmation_custom_id(always) is None
    footers: tuple[tuple[ConfirmationHow, str], ...] = (
        ("once", "Approved once by <@admin>"),
        ("always", "Always allowed by <@admin>"),
        ("channel", "Always allowed in this channel by <@admin>"),
    )
    for how, expected in footers:
        settled = build_confirmation_card(
            prompt, state="approved", answered_by_platform_user_id="admin", how=how
        )
        assert (
            build_confirmation_blocks(settled, prompt=prompt)[-1]["elements"][0]["text"] == expected
        )
    denied = build_confirmation_card(
        prompt, state="denied", answered_by_platform_user_id="admin", how="once"
    )
    assert build_confirmation_blocks(denied, prompt=prompt)[-1]["elements"][0]["text"] == (
        "Denied by <@admin>"
    )


@pytest.mark.parametrize("loser", ["deny", "expiry"])
async def test_save_if_lost_admin_check_returns_after_settlement(loser: str) -> None:
    """ToolRulesSaveIfLost: the role lookup awaits while the card settles."""
    prompt = _prompt(uuid.uuid4())
    controls: PostedConfirmations[object] = PostedConfirmations()  # type: ignore[type-var]
    token, future = controls.pending.open()
    controls.cards[token] = type("Card", (), {"prompt": prompt})()
    started = asyncio.Event()
    release = asyncio.Event()
    saved = False

    async def click() -> str | None:
        nonlocal saved
        started.set()
        await release.wait()
        refusal = controls.claim_admin(token)
        if refusal is None:
            saved = True
        return refusal

    task = asyncio.create_task(click())
    await started.wait()
    if loser == "deny":
        assert controls.claim(token, "requester", "denied") is None
        assert future.result() == "denied"
    else:
        controls.cards[token] = type(
            "Card", (), {"prompt": prompt.model_copy(update={"expires_at": datetime.now(UTC)})}
        )()
    release.set()
    assert await task == (
        "This request was already answered."
        if loser == "deny"
        else "This request expired, so nothing ran. Ask again to run it."
    )
    assert not saved


async def test_approved_click_without_save_has_no_rule_to_replay(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A restart after settlement does not synthesize a rule write."""
    tenant = Tenant(platform="discord", external_id=str(uuid.uuid4()))
    db_session.add(tenant)
    await db_session.flush()
    await db_session.commit()
    prompt = _prompt(tenant.id)
    controls: PostedConfirmations[object] = PostedConfirmations()  # type: ignore[type-var]
    token, future = controls.pending.open()
    controls.cards[token] = type("Card", (), {"prompt": prompt})()
    assert controls.claim_admin(token) is None
    assert future.result() == "approved"
    restarted: PostedConfirmations[object] = PostedConfirmations()  # type: ignore[type-var]
    assert restarted.claim_admin(token) == (
        "This request is no longer active. Ask again if it still needs doing."
    ), "a restarted process never saw this token: it claims neither an answer nor a non-run"
    async with db_session_factory() as session:
        assert (
            await store.get_rule(
                session, tenant_id=tenant.id, agent_id="ma-1", tool_key="remote/append_note"
            )
            is None
        )


async def test_committed_save_waits_for_ack_before_reporting_saved(
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    db_schema: str,
) -> None:
    tenant = Tenant(platform="discord", external_id=str(uuid.uuid4()))
    db_session.add(tenant)
    await db_session.commit()
    prompt = await snapshot_tool_rule(_prompt(tenant.id), db_session_factory)
    assert prompt.tool_rule is not None
    real_commit = AsyncAdapt_asyncpg_connection._commit_and_discard
    committed = asyncio.Event()
    release = asyncio.Event()

    async def delayed_ack(connection: AsyncAdapt_asyncpg_connection) -> None:
        await real_commit(connection)
        committed.set()
        await release.wait()

    monkeypatch.setattr(AsyncAdapt_asyncpg_connection, "_commit_and_discard", delayed_ack)
    save = asyncio.create_task(
        save_always_allow(
            db_session_factory,
            reference=prompt.tool_rule,
            scope="agent",
            subject=Subject(is_admin=True, platform_user_id="admin"),
            clicker="admin",
        )
    )
    independent_engine = build_test_engine(os.environ["DAIMON_DATABASE__TEST_URL"], db_schema)
    try:
        await asyncio.wait_for(committed.wait(), 1)
        assert not save.done(), "the commit landed but its acknowledgement is delayed"
        async with async_sessionmaker(independent_engine)() as session:
            row = await store.get_rule(
                session, tenant_id=tenant.id, agent_id="ma-1", tool_key="remote/append_note"
            )
            count = await session.scalar(
                select(func.count())
                .select_from(AgentToolRuleHistoryRecord)
                .where(AgentToolRuleHistoryRecord.tenant_id == tenant.id)
            )
            assert row is not None and row.rule == "allow" and count == 1
    finally:
        try:
            await independent_engine.dispose()
        finally:
            release.set()
    assert await asyncio.wait_for(save, 1) == "saved"


def test_any_clicker_cannot_claim_a_rule_scope() -> None:
    reference = _prompt(uuid.uuid4()).tool_rule
    assert reference is not None
    assert allowed_rule_scope(reference, Subject(platform_user_id="requester")) is None
    assert allowed_rule_scope(reference, Subject(platform_user_id="other")) is None
    assert (
        allowed_rule_scope(
            reference,
            Subject(platform_user_id="member"),
        )
        is None
    )
    assert (
        allowed_rule_scope(reference, Subject(platform_user_id="admin", is_admin=True)) == "agent"
    )


@pytest.mark.parametrize("intervening_rule", [None, "ask"])
async def test_blind_upsert_and_scoped_saves(
    intervening_rule: Literal["ask"] | None,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """ToolRulesBlindUpsert: a clear or set after posting makes the click's CAS stale."""
    tenant = Tenant(platform="discord", external_id=str(uuid.uuid4()))
    db_session.add(tenant)
    await db_session.flush()
    await db_session.commit()
    prompt = await snapshot_tool_rule(_prompt(tenant.id), db_session_factory)
    reference = prompt.tool_rule
    assert reference is not None
    assert reference.agent_revision == reference.channel_revision == 0
    admin = Subject(is_admin=True, platform_user_id="admin")
    async with db_session_factory() as session:
        await store.set_rule(
            session,
            tenant_id=tenant.id,
            agent_id="ma-1",
            agent_name="Helper",
            tool_key="remote/append_note",
            server_url=reference.server_url,
            rule=intervening_rule,
            set_by="other-admin",
            actor=admin,
        )
        await session.commit()
    assert (
        await save_always_allow(
            db_session_factory, reference=reference, scope="agent", subject=admin, clicker="admin"
        )
        == "stale"
    )
    async with db_session_factory() as session:
        row = await store.get_rule(
            session, tenant_id=tenant.id, agent_id="ma-1", tool_key="remote/append_note"
        )
        assert row is not None and row.rule == intervening_rule and row.revision == 1
    assert (
        await save_always_allow(
            db_session_factory,
            reference=reference,
            scope="channel",
            subject=admin,
            clicker="admin",
        )
        == "saved"
    )
    async with db_session_factory() as session:
        scoped = await store.get_rule(
            session,
            tenant_id=tenant.id,
            agent_id="ma-1",
            tool_key="remote/append_note",
            channel_id="home",
        )
        assert scoped is not None and scoped.rule == "allow" and scoped.scope == "chat"
        assert scoped.set_by == "admin"
    fresh = await snapshot_tool_rule(_prompt(tenant.id), db_session_factory)
    assert fresh.tool_rule is not None
    assert (
        await save_always_allow(
            db_session_factory,
            reference=fresh.tool_rule,
            scope="agent",
            subject=admin,
            clicker="admin",
        )
        == "saved"
    )
    async with db_session_factory() as session:
        global_rule = await store.get_rule(
            session, tenant_id=tenant.id, agent_id="ma-1", tool_key="remote/append_note"
        )
        assert global_rule is not None and global_rule.rule == "allow"
        assert global_rule.set_by == "admin" and global_rule.revision == 2
