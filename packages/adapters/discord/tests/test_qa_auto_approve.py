"""Staging QA auto-approve: only a QA bot's own cards, only on staging, same checks."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.discord import qa_auto_approve as qa
from daimon.adapters.discord.tool_confirmation import discord_confirmation_hook
from daimon.core.confirmation import (
    ApprovedConfirmation,
    ConfirmationPrompt,
    ToolCall,
    prompt_for_tool_call,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

QA, HUMAN = "111", "222"


def _prompt(requester: str = QA, *, admin: bool = False, expires_in=timedelta(minutes=10)):
    call = ToolCall(
        tool_use_id="tu_1", server_name="linear", tool_name="create_issue", input={"title": "Bug"}
    )
    prompt = prompt_for_tool_call(call, requester_platform_user_id=requester, now=datetime.now(UTC))
    return prompt.model_copy(update={"expires_at": datetime.now(UTC) + expires_in, "admin": admin})


def _settings(*qa_ids: str) -> Any:
    return SimpleNamespace(discord=SimpleNamespace(qa_bot_user_ids=qa_ids))


def _write(directory: Path, body: object, *, read_only: bool = True) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    path = directory / qa.CONTROL_FILE_NAME
    if path.exists():
        path.chmod(0o600)
    path.write_text(json.dumps(body))
    if read_only:
        path.chmod(0o400)
        directory.chmod(0o500)
    return path


@pytest.fixture
def mount(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """The confirm-click control dir, switched on, on a staging host."""
    directory = tmp_path / "mount"
    _write(directory, {"enabled": True})
    monkeypatch.setenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE", str(directory / "runtime.json"))
    monkeypatch.setattr(qa, "_project_id", None)
    lookup = AsyncMock(return_value="example-daimon-staging")
    monkeypatch.setattr(qa, "gcp_project_id", lookup)
    yield directory
    directory.chmod(0o700)


async def test_staging_qa_requester_with_the_flag_is_enabled(mount: Path) -> None:
    assert await qa.enabled_for(_settings(QA), QA) is True


@pytest.mark.parametrize("project", [None, "example-daimon-prod", "staging-but-not-suffix"])
async def test_a_host_that_is_not_staging_is_off(
    mount: Path, monkeypatch: pytest.MonkeyPatch, project: str | None
) -> None:
    monkeypatch.setattr(qa, "gcp_project_id", AsyncMock(return_value=project))
    assert await qa.enabled_for(_settings(QA), QA) is False


async def test_a_requester_who_is_not_a_qa_bot_is_off_without_a_metadata_lookup(
    mount: Path,
) -> None:
    assert await qa.enabled_for(_settings(QA), HUMAN) is False
    assert await qa.enabled_for(_settings(), QA) is False
    assert await qa.enabled_for(None, QA) is False
    qa.gcp_project_id.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "body",
    [{"enabled": False}, {"enabled": "true"}, {"enabled": True, "extra": 1}, [True], "on"],
)
async def test_anything_but_exactly_enabled_true_is_off(mount: Path, body: object) -> None:
    _write(mount, body)
    assert await qa.enabled_for(_settings(QA), QA) is False
    qa.gcp_project_id.assert_not_awaited()  # type: ignore[attr-defined]


async def test_a_writable_or_missing_flag_is_off(
    mount: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(mount, {"enabled": True}, read_only=False)
    assert await qa.enabled_for(_settings(QA), QA) is False
    (mount / qa.CONTROL_FILE_NAME).unlink()
    assert await qa.enabled_for(_settings(QA), QA) is False
    monkeypatch.delenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE")
    assert await qa.enabled_for(_settings(QA), QA) is False


def _channel() -> MagicMock:
    channel = MagicMock()
    channel.id = 77
    message = MagicMock(spec=discord.Message)
    message.id = 900
    message.edit = AsyncMock()
    channel.send = AsyncMock(return_value=message)
    return channel


def _texts(view: discord.ui.LayoutView) -> list[str]:
    return [i.content for i in view.walk_children() if isinstance(i, discord.ui.TextDisplay)]


def _buttons(view: discord.ui.LayoutView) -> list[str]:
    return [str(i.label) for i in view.walk_children() if isinstance(i, discord.ui.Button)]


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> tuple[AsyncMock, AsyncMock]:
    enabled = AsyncMock(return_value=True)
    audit = AsyncMock()
    monkeypatch.setattr(qa, "enabled_for", enabled)
    monkeypatch.setattr(qa, "audit", audit)
    return enabled, audit


async def _pending_card(channel: MagicMock, waiting: asyncio.Task[Any]) -> discord.ui.LayoutView:
    async with asyncio.timeout(2):
        while not channel.send.await_count:
            await asyncio.sleep(0)
    assert not waiting.done()
    return channel.send.await_args.kwargs["view"]


async def test_a_qa_card_is_approved_at_once_and_audited(gate) -> None:
    enabled, audit = gate
    channel = _channel()
    prompt = _prompt()
    result = await discord_confirmation_hook(channel)(prompt)

    assert isinstance(result, ApprovedConfirmation) and result.answer == "approved"
    enabled.assert_awaited_once_with(None, QA)
    (call,) = channel.send.await_args_list
    view = call.kwargs["view"]
    assert _buttons(view) == []
    assert any(qa.NOTE in text for text in _texts(view))
    assert audit.await_args.kwargs["outcome"] == "allowed"
    assert audit.await_args.kwargs["prompt"] == prompt
    # A turn that never sends the allow retires the card, as for a real click.
    await result.retire_unsent()
    channel.send.return_value.edit.assert_awaited()


async def test_a_card_that_is_not_enabled_waits_for_a_real_click(gate) -> None:
    enabled, audit = gate
    enabled.return_value = False
    channel = _channel()
    waiting = asyncio.create_task(discord_confirmation_hook(channel)(_prompt(HUMAN)))
    try:
        view = await _pending_card(channel, waiting)
        assert "Approve" in _buttons(view)
        audit.assert_not_awaited()
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)


async def test_an_admin_card_a_non_admin_qa_bot_asked_for_is_refused(gate) -> None:
    _, audit = gate
    channel = _channel()
    prompt = _prompt(admin=True)
    check = AsyncMock(return_value="This change needs a current admin.")
    waiting = asyncio.create_task(
        discord_confirmation_hook(
            channel, prepare_admin=AsyncMock(return_value=prompt), check_admin=check
        )(prompt)
    )
    try:
        view = await _pending_card(channel, waiting)
        # The live admin re-check ran for the requester, and nothing was answered.
        check.assert_awaited_once_with(prompt, QA)
        assert _buttons(view)[0] == "Confirm"
        assert audit.await_args.kwargs["outcome"] == "denied"
        assert "needs a current admin" in audit.await_args.kwargs["reason"]
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)


async def test_an_admin_card_a_qa_admin_asked_for_passes_the_same_recheck(gate) -> None:
    _, audit = gate
    channel = _channel()
    prompt = _prompt(admin=True)
    check = AsyncMock(return_value=None)
    result = await discord_confirmation_hook(
        channel, prepare_admin=AsyncMock(return_value=prompt), check_admin=check
    )(prompt)
    assert isinstance(result, ApprovedConfirmation)
    check.assert_awaited_once_with(prompt, QA)
    assert audit.await_args.kwargs["outcome"] == "allowed"


async def test_an_expired_card_is_not_approved(gate) -> None:
    _, audit = gate
    channel = _channel()
    result = await discord_confirmation_hook(channel)(_prompt(expires_in=timedelta(seconds=-1)))
    assert result == "expired"
    assert audit.await_args.kwargs["outcome"] == "denied"


async def test_the_audit_row_names_kind_requester_and_digest(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from daimon.core.stores.security_audit import list_events
    from daimon.testing.factories import make_tenant

    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session)
    prompt: ConfirmationPrompt = _prompt()
    await qa.audit(
        db_session_factory,
        tenant_id=tenant.id,
        prompt=prompt,
        outcome="allowed",
        reason="auto-approved for QA",
        channel_id=77,
    )
    async with db_session_factory() as session:
        (row,) = await list_events(session, tenant_id=tenant.id)
    assert row.tool_name == qa.AUDIT_TOOL and row.operation == "confirmation"
    assert row.platform_user_id == QA and row.outcome == "allowed"
    assert f"digest={qa.prompt_digest(prompt)}" in row.reason
    assert row.target_channel_id == "77"
