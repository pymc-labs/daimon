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


def _settings(*qa_ids: str, environment: str = "") -> Any:
    return SimpleNamespace(
        discord=SimpleNamespace(qa_bot_user_ids=qa_ids),
        environment=SimpleNamespace(name=environment),
    )


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


async def test_the_qa_lane_of_a_dev_project_is_enabled(
    mount: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(qa, "gcp_project_id", AsyncMock(return_value="example-daimon-dev"))
    assert await qa.enabled_for(_settings(QA, environment="lane-preview"), QA) is True


@pytest.mark.parametrize(
    ("project", "environment"),
    [
        ("example-daimon-dev", ""),
        ("example-daimon-dev", "lane-other"),
        ("example-daimon-prod", "lane-preview"),
        (None, "lane-preview"),
    ],
)
async def test_a_lane_name_without_a_dev_project_or_another_lane_is_off(
    mount: Path, monkeypatch: pytest.MonkeyPatch, project: str | None, environment: str
) -> None:
    monkeypatch.setattr(qa, "gcp_project_id", AsyncMock(return_value=project))
    assert await qa.enabled_for(_settings(QA, environment=environment), QA) is False


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


async def test_settings_without_discord_are_off(mount: Path) -> None:
    assert await qa.enabled_for(SimpleNamespace(), QA) is False  # type: ignore[arg-type]
    assert await qa.enabled_for(SimpleNamespace(discord=None), QA) is False  # type: ignore[arg-type]


async def test_a_writable_dir_or_a_symlinked_flag_is_off(mount: Path, tmp_path: Path) -> None:
    mount.chmod(0o700)  # the file stays 0400, but the dir is ours to swap it in
    assert await qa.enabled_for(_settings(QA), QA) is False
    real = _write(tmp_path / "elsewhere", {"enabled": True})
    (mount / qa.CONTROL_FILE_NAME).chmod(0o600)
    (mount / qa.CONTROL_FILE_NAME).unlink()
    (mount / qa.CONTROL_FILE_NAME).symlink_to(real)
    mount.chmod(0o500)
    assert await qa.enabled_for(_settings(QA), QA) is False
    (tmp_path / "elsewhere").chmod(0o700)


async def test_a_raising_gate_posts_the_normal_card(gate) -> None:
    enabled, audit = gate
    enabled.side_effect = RuntimeError("settings shape changed")
    channel = _channel()
    waiting = asyncio.create_task(discord_confirmation_hook(channel)(_prompt()))
    try:
        view = await _pending_card(channel, waiting)
        assert "Approve" in _buttons(view)
        audit.assert_not_awaited()
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)


def _publish(tool: str, **data: object) -> ConfirmationPrompt:
    from daimon.core.tool_safety import DAIMON_SERVER_NAME

    call = ToolCall(tool_use_id="tu_p", server_name=DAIMON_SERVER_NAME, tool_name=tool, input=data)
    return prompt_for_tool_call(call, requester_platform_user_id=QA, now=datetime.now(UTC))


@pytest.mark.parametrize(
    "prompt",
    [
        _publish("publish_report", title="Q3", recipients=["a@example.test"]),
        _publish("create_notebook_upload_url", slug="nb", permanent=True),
        _publish("create_notebook_upload_url", slug="nb", editable=True),
        # A re-upload to an existing (maybe permanent) notebook names only its slug.
        _publish("create_notebook_upload_url", slug="existing-permanent-blog"),
        _publish("create_notebook_upload_url", slug="nb", ttl_days=1),
        _publish("create_attachment_upload_url", slug="nb", name="data.csv"),
    ],
    ids=[
        "report",
        "permanent_notebook",
        "editable_notebook",
        "slug_only_reupload",
        "scratch_ttl_notebook",
        "attachment",
    ],
)
async def test_a_lasting_public_link_stays_manual(gate, prompt: ConfirmationPrompt) -> None:
    _, audit = gate
    assert prompt.lasting_public_link is True
    channel = _channel()
    waiting = asyncio.create_task(discord_confirmation_hook(channel)(prompt))
    try:
        view = await _pending_card(channel, waiting)
        assert "Approve" in _buttons(view)
        assert audit.await_args.kwargs["outcome"] == "denied"
        assert "lasting public link" in audit.await_args.kwargs["reason"]
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)


async def test_a_tool_rule_card_is_approved_once_without_saving_a_rule(
    gate, monkeypatch: pytest.MonkeyPatch
) -> None:
    import uuid

    from daimon.adapters.discord import tool_confirmation as confirmation_module
    from daimon.core.confirmation import ToolRuleCardReference

    save = AsyncMock(return_value="saved")
    monkeypatch.setattr(confirmation_module, "save_always_allow", save)
    prompt = _prompt().model_copy(
        update={
            "tool_rules_enabled": True,
            "tool_rule": ToolRuleCardReference(
                tenant_id=uuid.uuid4(),
                agent_id="ma-1",
                agent_name="Helper",
                tool_key="linear/create_issue",
                server_url="https://example.test/mcp",
            ),
        }
    )
    channel = _channel()
    assert isinstance(await discord_confirmation_hook(channel)(prompt), ApprovedConfirmation)
    save.assert_not_awaited()
    assert _buttons(channel.send.await_args.kwargs["view"]) == []


async def test_a_card_that_fails_to_post_after_approval_is_audited_as_an_error(gate) -> None:
    _, audit = gate
    channel = _channel()
    channel.send.side_effect = discord.HTTPException(MagicMock(status=503), "down")
    with pytest.raises(discord.HTTPException):
        await discord_confirmation_hook(channel)(_prompt())
    assert audit.await_args.kwargs["outcome"] == "error"


async def test_a_retired_auto_approved_card_keeps_its_qa_note(gate) -> None:
    channel = _channel()
    result = await discord_confirmation_hook(channel)(_prompt())
    assert isinstance(result, ApprovedConfirmation)
    await result.retire_unsent()
    from daimon.core.posted_controls.lifecycle import drain_card_edits

    await drain_card_edits(1)
    view = channel.send.return_value.edit.call_args.kwargs["view"]
    assert any(qa.NOTE in text for text in _texts(view))
