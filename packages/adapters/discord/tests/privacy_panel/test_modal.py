"""DeleteConfirmModal tests: typed-username branching, purge call, log shape."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.discord.privacy_panel.modal import DeleteConfirmModal
from daimon.core.errors import DaimonError
from daimon.core.ma import SessionDeletionReport
from daimon.core.purge import AccountPurgeResult, PurgeReport

from .conftest import _make_runtime


def _make_modal(*, user_name: str = "carlos") -> DeleteConfirmModal:
    return DeleteConfirmModal(
        runtime=_make_runtime(),
        account_id=uuid.uuid4(),
        user_name=user_name,
    )


def _make_interaction_with_response() -> MagicMock:
    interaction = MagicMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    return interaction


def _set_typed(modal: DeleteConfirmModal, value: str) -> None:
    """Bypass discord.py's TextInput.value read-only property in tests.

    `discord.ui.TextInput.value` is property-only (no setter); the underlying
    `_value` attribute is the test-harness handle when no real Discord
    interaction is firing.
    """
    modal.name_in._value = value  # type: ignore[attr-defined]


def _make_purge_result(
    *,
    accounts: int = 1,
    platform_principals: int = 0,
    cli_principals: int = 0,
    routines: int = 0,
    principal_links: int = 0,
    user_configs: int = 0,
    sessions_deleted: int = 0,
    sessions_failed: int = 0,
) -> AccountPurgeResult:
    return AccountPurgeResult(
        db=PurgeReport(
            accounts=accounts,
            platform_principals=platform_principals,
            cli_principals=cli_principals,
            routines=routines,
            principal_links=principal_links,
            user_configs=user_configs,
        ),
        sessions=SessionDeletionReport(deleted=sessions_deleted, failed=sessions_failed),
    )


def test_modal_label_and_placeholder_both_show_expected_username() -> None:
    """D-CONFIRM-01: label AND placeholder show the verbatim user_name."""
    modal = _make_modal(user_name="carlos")
    label = modal.name_in.label
    assert label is not None and "carlos" in label, (
        f"Modal TextInput label must show user_name 'carlos'; got {label!r}"
    )
    assert modal.name_in.placeholder == "carlos", (
        "Modal TextInput placeholder must equal user_name 'carlos'; "
        f"got {modal.name_in.placeholder!r}"
    )


@pytest.mark.parametrize(
    "typed_value",
    [
        "Carlos",  # case-different
        "someone_else",  # clearly different
        "",  # empty
        "carlos extra",  # extra content
    ],
    ids=["case_mismatch", "different_name", "empty", "trailing_content"],
)
async def test_on_submit_with_mismatched_username_does_not_call_purge(
    typed_value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-29-12: mismatch MUST NOT call purge_account."""
    modal = _make_modal(user_name="carlos")
    _set_typed(modal, typed_value)
    purge_mock = AsyncMock()
    monkeypatch.setattr("daimon.adapters.discord.privacy_panel.modal.purge_account", purge_mock)
    interaction = _make_interaction_with_response()
    await modal.on_submit(interaction)
    assert purge_mock.await_count == 0, (
        f"purge_account MUST NOT be called when typed value {typed_value!r} != user_name 'carlos'"
    )
    interaction.response.send_message.assert_awaited_once()
    assert interaction.response.send_message.call_args.kwargs.get("ephemeral") is True, (
        "Mismatch error message must be ephemeral"
    )
    interaction.response.defer.assert_not_awaited()


async def test_on_submit_with_matching_username_calls_purge_account_with_anthropic_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Match branch: purge_account called with account_id AND anthropic=runtime.anthropic."""
    modal = _make_modal(user_name="carlos")
    _set_typed(modal, "carlos")
    purge_mock = AsyncMock(return_value=_make_purge_result(accounts=1))
    monkeypatch.setattr("daimon.adapters.discord.privacy_panel.modal.purge_account", purge_mock)
    interaction = _make_interaction_with_response()
    await modal.on_submit(interaction)
    purge_mock.assert_awaited_once()
    kwargs = purge_mock.call_args.kwargs
    assert kwargs["sm"] is modal.runtime.sessionmaker, (
        "purge_account must be called with the runtime's sessionmaker"
    )
    assert kwargs["account_id"] == modal.account_id, (
        "purge_account must be called with the modal's account_id "
        f"({modal.account_id}); got {kwargs['account_id']}"
    )
    assert "anthropic" in kwargs, (
        "purge_account must be called with the 'anthropic' kwarg so upstream deletion runs"
    )
    assert kwargs["anthropic"] is modal.runtime.anthropic, (
        "purge_account must receive the runtime's AsyncAnthropic client"
    )


async def test_on_submit_matching_username_with_leading_trailing_whitespace_still_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Input is stripped before comparison; surrounding whitespace still matches."""
    modal = _make_modal(user_name="carlos")
    _set_typed(modal, "  carlos  ")
    purge_mock = AsyncMock(return_value=_make_purge_result(accounts=1))
    monkeypatch.setattr("daimon.adapters.discord.privacy_panel.modal.purge_account", purge_mock)
    interaction = _make_interaction_with_response()
    await modal.on_submit(interaction)
    purge_mock.assert_awaited_once()


async def test_on_submit_defers_interaction_before_running_purge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Match branch acks within Discord's 3s window BEFORE the slow purge runs.

    purge_account does a DB transaction plus per-session upstream HTTP deletes,
    which can exceed the interaction window — defer() must come first or the
    user sees 'This interaction failed' on a committed, irreversible erasure.
    """
    modal = _make_modal(user_name="carlos")
    _set_typed(modal, "carlos")
    interaction = _make_interaction_with_response()

    async def purge_after_defer(**_kwargs: object) -> AccountPurgeResult:
        assert interaction.response.defer.await_count == 1, (
            "interaction.response.defer must be awaited BEFORE purge_account runs"
        )
        return _make_purge_result(accounts=1)

    monkeypatch.setattr(
        "daimon.adapters.discord.privacy_panel.modal.purge_account",
        AsyncMock(side_effect=purge_after_defer),
    )
    await modal.on_submit(interaction)
    interaction.response.defer.assert_awaited_once()
    interaction.edit_original_response.assert_awaited_once()


async def test_on_submit_purge_failure_sends_ephemeral_followup_and_no_completion_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Error branch after defer: ephemeral followup, no edit, no completed log."""
    modal = _make_modal(user_name="carlos")
    _set_typed(modal, "carlos")
    monkeypatch.setattr(
        "daimon.adapters.discord.privacy_panel.modal.purge_account",
        AsyncMock(side_effect=DaimonError("db exploded")),
    )
    log_mock = MagicMock()
    monkeypatch.setattr("daimon.adapters.discord.privacy_panel.modal.log", log_mock)
    interaction = _make_interaction_with_response()
    await modal.on_submit(interaction)
    interaction.followup.send.assert_awaited_once()
    assert interaction.followup.send.call_args.kwargs.get("ephemeral") is True, (
        "Post-defer error message must be an ephemeral followup"
    )
    interaction.edit_original_response.assert_not_awaited()
    info_calls = [
        c
        for c in log_mock.info.call_args_list
        if c.args and c.args[0] == "privacy.delete.completed"
    ]
    assert info_calls == [], "Failed purge must NOT emit privacy.delete.completed"


async def test_on_submit_success_edits_message_into_controls_less_post_delete_view(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Success branch re-renders a controls-less LayoutView (never view=None on V2)."""
    modal = _make_modal(user_name="carlos")
    _set_typed(modal, "carlos")
    monkeypatch.setattr(
        "daimon.adapters.discord.privacy_panel.modal.purge_account",
        AsyncMock(
            return_value=_make_purge_result(accounts=1, platform_principals=1, sessions_deleted=2)
        ),
    )
    interaction = _make_interaction_with_response()
    await modal.on_submit(interaction)
    interaction.edit_original_response.assert_awaited_once()
    edit_kwargs = interaction.edit_original_response.call_args.kwargs
    view = edit_kwargs.get("view")
    assert isinstance(view, discord.ui.LayoutView), (
        f"Success branch must re-render a controls-less LayoutView; got {view!r}"
    )
    controls = [
        c for c in view.walk_children() if isinstance(c, (discord.ui.Button, discord.ui.Select))
    ]
    assert controls == [], f"Post-delete view must carry no controls; got {controls}"
    assert "embed" not in edit_kwargs, "V2 edit must not carry an embed kwarg"


async def test_on_submit_success_view_reflects_upstream_session_deletion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The post-delete view passed to edit_message includes session deletion counts."""
    modal = _make_modal(user_name="carlos")
    _set_typed(modal, "carlos")
    monkeypatch.setattr(
        "daimon.adapters.discord.privacy_panel.modal.purge_account",
        AsyncMock(return_value=_make_purge_result(accounts=1, sessions_deleted=3)),
    )
    interaction = _make_interaction_with_response()
    await modal.on_submit(interaction)
    edit_kwargs = interaction.edit_original_response.call_args.kwargs
    view = edit_kwargs["view"]
    view_text = "\n".join(
        item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay)
    )
    assert "3 session transcript(s) deleted" in view_text, (
        f"Post-delete view must report session transcript deletion count; got {view_text!r}"
    )


async def test_on_submit_logs_purge_completion_with_upstream_counts_and_without_pii(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T-29-13 / D-AUDIT-02: log.info('privacy.delete.completed', ...) has counts + upstream, no PII."""
    modal = _make_modal(user_name="carlos")
    _set_typed(modal, "carlos")
    monkeypatch.setattr(
        "daimon.adapters.discord.privacy_panel.modal.purge_account",
        AsyncMock(
            return_value=_make_purge_result(
                accounts=1,
                platform_principals=1,
                cli_principals=0,
                routines=2,
                principal_links=1,
                user_configs=1,
                sessions_deleted=3,
                sessions_failed=1,
            )
        ),
    )
    log_mock = MagicMock()
    monkeypatch.setattr("daimon.adapters.discord.privacy_panel.modal.log", log_mock)
    interaction = _make_interaction_with_response()
    await modal.on_submit(interaction)
    # Find the .info('privacy.delete.completed', ...) call among log_mock's calls.
    info_calls = [
        c
        for c in log_mock.info.call_args_list
        if c.args and c.args[0] == "privacy.delete.completed"
    ]
    assert len(info_calls) == 1, (
        "Expected exactly one log.info('privacy.delete.completed', ...) call; "
        f"got {len(info_calls)} from calls {log_mock.info.call_args_list!r}"
    )
    log_kwargs = info_calls[0].kwargs
    log_keys = set(log_kwargs.keys())
    assert log_keys == {
        "account_id",
        "principals",
        "routines",
        "links",
        "user_skills",
        "github_credentials",
        "oauth_states",
        "mcp_tokens",
        "agent_github_binding",
        "sessions_deleted",
        "sessions_failed",
    }, (
        "D-AUDIT-02: log kwargs must be EXACTLY "
        "{account_id, principals, routines, links, user_skills, github_credentials, oauth_states, "
        "mcp_tokens, agent_github_binding, sessions_deleted, sessions_failed}; "
        f"got {log_keys}. Extra/missing keys break the no-PII contract."
    )
    # Explicit PII-key absence checks:
    for forbidden_key in ("platform_user_id", "user_name", "guild_id"):
        assert forbidden_key not in log_kwargs, (
            f"D-AUDIT-02 violation: log kwargs MUST NOT contain {forbidden_key!r}; "
            f"got {log_kwargs!r}"
        )
    assert log_kwargs["account_id"] == str(modal.account_id), (
        "log account_id must be the str(uuid) of the deleted account"
    )
    assert log_kwargs["principals"] == 1, "log principals = cli_principals + platform_principals"
    assert log_kwargs["routines"] == 2
    assert log_kwargs["links"] == 1
    assert log_kwargs["sessions_deleted"] == 3, "log sessions_deleted must be the upstream count"
    assert log_kwargs["sessions_failed"] == 1, "log sessions_failed must be the upstream count"


async def test_on_submit_mismatch_does_not_emit_completion_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mismatch path: no purge, and crucially no privacy.delete.completed log line."""
    modal = _make_modal(user_name="carlos")
    _set_typed(modal, "wrong")
    monkeypatch.setattr("daimon.adapters.discord.privacy_panel.modal.purge_account", AsyncMock())
    log_mock = MagicMock()
    monkeypatch.setattr("daimon.adapters.discord.privacy_panel.modal.log", log_mock)
    interaction = _make_interaction_with_response()
    await modal.on_submit(interaction)
    info_calls = [
        c
        for c in log_mock.info.call_args_list
        if c.args and c.args[0] == "privacy.delete.completed"
    ]
    assert len(info_calls) == 0, (
        "Mismatch branch must NOT emit privacy.delete.completed — the user did not delete anything"
    )
