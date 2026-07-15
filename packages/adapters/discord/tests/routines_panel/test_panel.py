"""View-shape tests for RoutinesPanelView (LayoutView): picker, buttons, gates, container colors."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

# pyright: reportPrivateUsage=false
from daimon.adapters.discord import theme
from daimon.adapters.discord.routines_panel.embeds import build_panel_container
from daimon.adapters.discord.routines_panel.panel import (
    RoutinesPanelView,
    _PauseButton,
    _RoutinePicker,
    _ViewOutputButton,
)
from daimon.adapters.discord.routines_panel.state import (
    RoutineEntry,
    RoutinesPanelState,
    derive_state,
)
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.domain import RoutineRow
from sqlalchemy.exc import SQLAlchemyError


def _make_row(**overrides: Any) -> RoutineRow:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "tenant_id": uuid.uuid4(),
        "created_by_user_id": None,
        "agent_id": "agent_a",
        "agent_name": "daimon",
        "cron_expr": "0 9 * * 1-5",
        "timezone": "UTC",
        "trigger_message": "summarize",
        "enabled": True,
        "next_fire_at": None,
        "last_fired_at": None,
        "last_error": None,
        "last_result_tail": None,
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "updated_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    base.update(overrides)
    return RoutineRow.model_validate(base)


def _make_entry(**overrides: Any) -> RoutineEntry:
    row = _make_row(**overrides)
    glyph, color = derive_state(row)
    return RoutineEntry(
        routine=row,
        agent_name="agent",
        glyph=glyph,
        color=color,
        label=row.trigger_message[:60] or row.id.hex[:8],
    )


def _make_state(entries: list[RoutineEntry], *, over_cap: int = 0) -> RoutinesPanelState:
    return RoutinesPanelState.initial(rows=entries, over_cap_count=over_cap, agent_name_map={})


def _make_runtime() -> DiscordRuntime:
    return MagicMock(spec=DiscordRuntime)


def _find_button(view: discord.ui.LayoutView, label: str) -> discord.ui.Button[Any]:
    """Walk all nested children of a LayoutView to find a button by label."""
    for child in view.walk_children():
        if isinstance(child, discord.ui.Button) and child.label == label:
            return child
    raise AssertionError(f"No button labeled {label!r}")


def _find_select(view: discord.ui.LayoutView) -> discord.ui.Select[Any]:
    """Walk all nested children to find the first Select component."""
    for child in view.walk_children():
        if isinstance(child, discord.ui.Select):
            return child
    raise AssertionError("No Select component found in view")


def test_picker_renders_one_option_per_entry() -> None:
    entries = [_make_entry(trigger_message=f"task-{i}") for i in range(3)]
    state = _make_state(entries)
    view = RoutinesPanelView(state, runtime=_make_runtime(), allowed_user_id=42)
    picker = _find_select(view)
    assert len(picker.options) == 3, "picker must show one option per RoutineEntry"
    values = {opt.value for opt in picker.options}
    assert values == {str(e.routine.id) for e in entries}, (
        "option values must round-trip routine ids"
    )


def test_picker_description_format() -> None:
    """Picker option description must be '{glyph} {state} · {agent}' shape."""
    entry = _make_entry(trigger_message="x", cron_expr="0 9 * * 1-5")
    view = RoutinesPanelView(_make_state([entry]), runtime=_make_runtime(), allowed_user_id=42)
    picker = _find_select(view)
    desc = picker.options[0].description
    assert desc is not None
    assert desc.startswith(entry.glyph), "description must lead with the glyph"
    # R3 spec: '{glyph} {state} · {agent}' — cron no longer in picker description
    assert "agent" in desc, "description must include the agent name"
    assert len(desc) <= 100, "description must respect the 100-char Discord ceiling"


def test_picker_description_does_not_contain_cron() -> None:
    """R3 change: cron demoted to panel subtext; picker description is '{glyph} {state} · {agent}'."""
    entry = _make_entry(trigger_message="x", cron_expr="0 9 * * 1-5")
    view = RoutinesPanelView(_make_state([entry]), runtime=_make_runtime(), allowed_user_id=42)
    picker = _find_select(view)
    desc = picker.options[0].description or ""
    assert "0 9 * * 1-5" not in desc, (
        "cron expression must not appear in picker description (demoted to subtext in R3)"
    )


def test_picker_label_uses_trigger_message_truncated() -> None:
    entry = _make_entry(trigger_message="x" * 80)
    view = RoutinesPanelView(_make_state([entry]), runtime=_make_runtime(), allowed_user_id=42)
    picker = _find_select(view)
    assert picker.options[0].label == entry.label, (
        "picker label must come from RoutineEntry.label (already truncated to 60)"
    )
    assert len(picker.options[0].label) <= 60


def test_picker_caps_at_25() -> None:
    entries = [_make_entry(trigger_message=f"t-{i:02d}") for i in range(30)]
    state = _make_state(entries)
    view = RoutinesPanelView(state, runtime=_make_runtime(), allowed_user_id=42)
    picker = _find_select(view)
    assert len(picker.options) <= 25, "Discord StringSelect hard limit is 25 options"


def test_picker_disabled_when_empty_roster() -> None:
    state = _make_state([])
    view = RoutinesPanelView(state, runtime=_make_runtime(), allowed_user_id=42)
    picker = _find_select(view)
    assert picker.disabled is True, "empty roster must disable the picker"
    # Empty roster sentinel option
    assert picker.options[0].value == "__none__", "empty roster must use __none__ sentinel"


def test_accent_colour_per_state() -> None:
    """Container accent: non-blurple states carry color bar; blurple-pending has None."""
    paused = _make_entry(enabled=False)
    container_paused = build_panel_container(
        _make_state([paused]), now=datetime(2026, 5, 14, 12, 0, tzinfo=UTC)
    )
    assert container_paused.accent_colour == theme.COLOR_PAUSED, (
        "paused-state container must carry COLOR_PAUSED as accent"
    )

    blurple = _make_entry(enabled=True, last_fired_at=None)
    container_blurple = build_panel_container(
        _make_state([blurple]), now=datetime(2026, 5, 14, 12, 0, tzinfo=UTC)
    )
    assert container_blurple.accent_colour is None, (
        "blurple-pending container must have no accent (F5 default = no accent)"
    )

    error = _make_entry(
        enabled=True,
        last_fired_at=datetime(2026, 5, 1, tzinfo=UTC),
        last_error="boom",
    )
    container_error = build_panel_container(
        _make_state([error]), now=datetime(2026, 5, 14, 12, 0, tzinfo=UTC)
    )
    assert container_error.accent_colour == theme.COLOR_RED, (
        "errored-state container must carry COLOR_RED as accent"
    )

    success = _make_entry(enabled=True, last_fired_at=datetime(2026, 5, 1, tzinfo=UTC))
    container_success = build_panel_container(
        _make_state([success]), now=datetime(2026, 5, 14, 12, 0, tzinfo=UTC)
    )
    assert container_success.accent_colour == theme.COLOR_GREEN, (
        "success-state container must carry COLOR_GREEN as accent"
    )


def test_view_output_disabled_for_never_run() -> None:
    entry = _make_entry(last_fired_at=None)
    state = _make_state([entry])
    view = RoutinesPanelView(state, runtime=_make_runtime(), allowed_user_id=42)
    btn = next(b for b in view.walk_children() if isinstance(b, _ViewOutputButton))
    assert btn.disabled is True, "View-output must be disabled on a never-run routine"


def test_view_output_enabled_for_succeeded() -> None:
    entry = _make_entry(last_fired_at=datetime(2026, 5, 1, tzinfo=UTC), last_error=None)
    state = _make_state([entry])
    view = RoutinesPanelView(state, runtime=_make_runtime(), allowed_user_id=42)
    btn = next(b for b in view.walk_children() if isinstance(b, _ViewOutputButton))
    assert btn.disabled is False, "View-output must be enabled on a succeeded routine"


def test_pause_button_relabels_on_pause_state() -> None:
    running = _make_entry(enabled=True)
    state = _make_state([running])
    view = RoutinesPanelView(state, runtime=_make_runtime(), allowed_user_id=42)
    pause_btn = next(b for b in view.walk_children() if isinstance(b, _PauseButton))
    assert pause_btn.label == "⏸ Pause"
    assert pause_btn.style == discord.ButtonStyle.danger

    paused = _make_entry(enabled=False)
    state2 = _make_state([paused])
    view2 = RoutinesPanelView(state2, runtime=_make_runtime(), allowed_user_id=42)
    resume_btn = next(b for b in view2.walk_children() if isinstance(b, _PauseButton))
    assert resume_btn.label == "▶ Resume"
    assert resume_btn.style == discord.ButtonStyle.success


def test_empty_roster_disables_all_but_done_and_refresh() -> None:
    state = _make_state([])
    view = RoutinesPanelView(state, runtime=_make_runtime(), allowed_user_id=42)
    picker = _find_select(view)
    assert picker.disabled is True
    pause_btn = next(b for b in view.walk_children() if isinstance(b, _PauseButton))
    assert pause_btn.disabled is True
    view_output = next(b for b in view.walk_children() if isinstance(b, _ViewOutputButton))
    assert view_output.disabled is True
    done = _find_button(view, "Done")
    assert done.disabled is False, "Done must always be enabled"

    # Empty container shows the no-routines hint
    container = build_panel_container(state, now=datetime(2026, 5, 14, 12, 0, tzinfo=UTC))
    all_text = " ".join(
        child.content for child in container.children if isinstance(child, discord.ui.TextDisplay)
    )
    assert "No routines yet" in all_text or "No routines" in all_text, (
        "empty routines container must show the hint"
    )


@pytest.mark.asyncio
async def test_interaction_check_rejects_wrong_user() -> None:
    entry = _make_entry()
    view = RoutinesPanelView(_make_state([entry]), runtime=_make_runtime(), allowed_user_id=42)
    interaction = MagicMock()
    interaction.user.id = 999
    interaction.response.send_message = AsyncMock()
    ok = await view.interaction_check(interaction)
    assert ok is False, "non-invoker must be rejected by the view's interaction_check"
    interaction.response.send_message.assert_called_once()


class _SessionCM:
    """Minimal async context manager that doubles as a transaction context."""

    def __init__(self, session: Any) -> None:
        self._session = session

    async def __aenter__(self) -> Any:
        return self._session

    async def __aexit__(self, *_: Any) -> None:
        return None


def _runtime_with_session(session: Any) -> MagicMock:
    runtime = MagicMock(spec=DiscordRuntime)
    runtime.sessionmaker = MagicMock(return_value=_SessionCM(session))
    return runtime


class _TxCM:
    async def __aenter__(self) -> _TxCM:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None


def _mock_session_with_row(row: RoutineRow | None) -> MagicMock:
    """Return a session-like MagicMock whose session.begin() yields a transaction CM."""
    session = MagicMock()
    session.begin = MagicMock(return_value=_TxCM())
    return session


@pytest.mark.asyncio
async def test_unauthorized_pause_click_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    row = _make_row(
        created_by_user_id="999",
        enabled=True,
        tenant_id=derive_tenant_uuid(platform="discord", workspace_id="G1"),
    )
    entry = _make_entry()
    entry = RoutineEntry(
        routine=row,
        agent_name="a",
        glyph=entry.glyph,
        color=entry.color,
        label="t",
    )

    session = _mock_session_with_row(row)
    runtime = _runtime_with_session(session)
    view = RoutinesPanelView(_make_state([entry]), runtime=runtime, allowed_user_id=42)
    pause_btn = next(b for b in view.walk_children() if isinstance(b, _PauseButton))
    pause_btn._view = view  # type: ignore[attr-defined]  # discord.py sets _view on attach; test re-binds

    pause_calls: list[Any] = []

    async def _fake_get_routine(_s: Any, _id: Any) -> RoutineRow:
        return row

    async def _fake_pause(_s: Any, _id: Any) -> RoutineRow:
        pause_calls.append((_s, _id))
        return row

    monkeypatch.setattr(
        "daimon.adapters.discord.routines_panel.panel.get_routine", _fake_get_routine
    )
    monkeypatch.setattr(
        "daimon.adapters.discord.routines_panel.panel.pause_routine_via_panel",
        _fake_pause,
    )

    interaction = MagicMock()
    interaction.user = MagicMock()
    interaction.user.id = 42  # not the creator (999)
    interaction.user.guild_permissions.manage_guild = False
    interaction.guild_id = "G1"
    interaction.response.send_message = AsyncMock()
    interaction.response.is_done = MagicMock(return_value=True)
    interaction.edit_original_response = AsyncMock()

    await pause_btn.callback(interaction)

    interaction.response.send_message.assert_called_once()
    args, kwargs = interaction.response.send_message.call_args
    text = args[0] if args else kwargs.get("content", "")
    assert "creator or a guild admin" in text, (
        "non-creator non-admin must see the explicit gate message"
    )
    assert pause_calls == [], "no UPDATE must be issued on rejected click"


@pytest.mark.asyncio
async def test_creator_can_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    row = _make_row(
        created_by_user_id="42",
        enabled=True,
        tenant_id=derive_tenant_uuid(platform="discord", workspace_id="G1"),
    )
    entry = _make_entry()
    entry = RoutineEntry(
        routine=row,
        agent_name="a",
        glyph=entry.glyph,
        color=entry.color,
        label="t",
    )
    session = _mock_session_with_row(row)
    runtime = _runtime_with_session(session)
    view = RoutinesPanelView(_make_state([entry]), runtime=runtime, allowed_user_id=42)
    pause_btn = next(b for b in view.walk_children() if isinstance(b, _PauseButton))
    pause_btn._view = view  # type: ignore[attr-defined]  # test re-binding

    invoked: list[Any] = []

    async def _fake_get_routine(_s: Any, _id: Any) -> RoutineRow:
        return row

    async def _fake_pause(_s: Any, _id: Any) -> RoutineRow:
        invoked.append((_s, _id))
        return row

    async def _fake_rerender(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(
        "daimon.adapters.discord.routines_panel.panel.get_routine", _fake_get_routine
    )
    monkeypatch.setattr(
        "daimon.adapters.discord.routines_panel.panel.pause_routine_via_panel",
        _fake_pause,
    )
    monkeypatch.setattr("daimon.adapters.discord.routines_panel.panel._rerender", _fake_rerender)

    interaction = MagicMock()
    interaction.user = MagicMock()
    interaction.user.id = 42  # matches created_by_user_id="42"
    interaction.user.guild_permissions.manage_guild = False
    interaction.guild_id = "G1"
    interaction.response.send_message = AsyncMock()

    await pause_btn.callback(interaction)
    assert len(invoked) == 1, "creator click must invoke pause_routine_via_panel"


@pytest.mark.asyncio
async def test_pause_callback_db_failure_sends_ephemeral_error_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLB-02 (panel): a store failure inside the button callback must post an
    ephemeral error to the clicking user instead of raising out of the
    callback (T-95-07/T-95-08)."""
    row = _make_row(
        created_by_user_id="42",
        enabled=True,
        tenant_id=derive_tenant_uuid(platform="discord", workspace_id="G1"),
    )
    entry = _make_entry()
    entry = RoutineEntry(
        routine=row,
        agent_name="a",
        glyph=entry.glyph,
        color=entry.color,
        label="t",
    )
    session = _mock_session_with_row(row)
    runtime = _runtime_with_session(session)
    view = RoutinesPanelView(_make_state([entry]), runtime=runtime, allowed_user_id=42)
    pause_btn = next(b for b in view.walk_children() if isinstance(b, _PauseButton))
    pause_btn._view = view  # type: ignore[attr-defined]  # test re-binding

    async def _fake_get_routine(_s: Any, _id: Any) -> RoutineRow:
        return row

    async def _fake_pause_raises(_s: Any, _id: Any) -> RoutineRow:
        raise SQLAlchemyError("db down")

    monkeypatch.setattr(
        "daimon.adapters.discord.routines_panel.panel.get_routine", _fake_get_routine
    )
    monkeypatch.setattr(
        "daimon.adapters.discord.routines_panel.panel.pause_routine_via_panel",
        _fake_pause_raises,
    )

    interaction = MagicMock()
    interaction.user = MagicMock()
    interaction.user.id = 42  # matches created_by_user_id="42"
    interaction.user.guild_permissions.manage_guild = False
    interaction.guild_id = "G1"
    interaction.response.send_message = AsyncMock()
    interaction.response.is_done = MagicMock(return_value=False)

    await pause_btn.callback(interaction)  # must not raise

    interaction.response.send_message.assert_called_once()
    _args, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True, "callback failure must post an ephemeral error"


@pytest.mark.asyncio
async def test_pause_callback_unexpected_error_sends_ephemeral_error_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The catch-all clause covers any exception, not just the typed tuple."""
    row = _make_row(
        created_by_user_id="42",
        enabled=True,
        tenant_id=derive_tenant_uuid(platform="discord", workspace_id="G1"),
    )
    entry = _make_entry()
    entry = RoutineEntry(
        routine=row,
        agent_name="a",
        glyph=entry.glyph,
        color=entry.color,
        label="t",
    )
    session = _mock_session_with_row(row)
    runtime = _runtime_with_session(session)
    view = RoutinesPanelView(_make_state([entry]), runtime=runtime, allowed_user_id=42)
    pause_btn = next(b for b in view.walk_children() if isinstance(b, _PauseButton))
    pause_btn._view = view  # type: ignore[attr-defined]  # test re-binding

    async def _fake_get_routine(_s: Any, _id: Any) -> RoutineRow:
        return row

    async def _fake_pause_raises(_s: Any, _id: Any) -> RoutineRow:
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "daimon.adapters.discord.routines_panel.panel.get_routine", _fake_get_routine
    )
    monkeypatch.setattr(
        "daimon.adapters.discord.routines_panel.panel.pause_routine_via_panel",
        _fake_pause_raises,
    )

    interaction = MagicMock()
    interaction.user = MagicMock()
    interaction.user.id = 42  # matches created_by_user_id="42"
    interaction.user.guild_permissions.manage_guild = False
    interaction.guild_id = "G1"
    interaction.response.send_message = AsyncMock()
    interaction.response.is_done = MagicMock(return_value=False)

    await pause_btn.callback(interaction)  # must not raise

    interaction.response.send_message.assert_called_once()
    _args, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True, "callback failure must post an ephemeral error"


@pytest.mark.asyncio
async def test_admin_can_pause_anyone(monkeypatch: pytest.MonkeyPatch) -> None:
    row = _make_row(
        created_by_user_id="999",
        enabled=True,
        tenant_id=derive_tenant_uuid(platform="discord", workspace_id="G1"),
    )
    entry = _make_entry()
    entry = RoutineEntry(
        routine=row,
        agent_name="a",
        glyph=entry.glyph,
        color=entry.color,
        label="t",
    )
    session = _mock_session_with_row(row)
    runtime = _runtime_with_session(session)
    view = RoutinesPanelView(_make_state([entry]), runtime=runtime, allowed_user_id=42)
    pause_btn = next(b for b in view.walk_children() if isinstance(b, _PauseButton))
    pause_btn._view = view  # type: ignore[attr-defined]  # test re-binding

    invoked: list[Any] = []

    async def _fake_get_routine(_s: Any, _id: Any) -> RoutineRow:
        return row

    async def _fake_pause(_s: Any, _id: Any) -> RoutineRow:
        invoked.append((_s, _id))
        return row

    async def _fake_rerender(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(
        "daimon.adapters.discord.routines_panel.panel.get_routine", _fake_get_routine
    )
    monkeypatch.setattr(
        "daimon.adapters.discord.routines_panel.panel.pause_routine_via_panel",
        _fake_pause,
    )
    monkeypatch.setattr("daimon.adapters.discord.routines_panel.panel._rerender", _fake_rerender)

    # Admin user is a real discord.Member-ish mock with manage_guild=True
    admin_member = MagicMock(spec=discord.Member)
    admin_member.id = 42
    admin_member.guild_permissions.manage_guild = True

    interaction = MagicMock()
    interaction.user = admin_member
    interaction.guild_id = "G1"
    interaction.response.send_message = AsyncMock()

    await pause_btn.callback(interaction)
    assert len(invoked) == 1, "admin click must invoke pause_routine_via_panel"


@pytest.mark.asyncio
async def test_picker_callback_value_outside_guild_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row_local = _make_row(
        tenant_id=derive_tenant_uuid(platform="discord", workspace_id="A"),
        enabled=True,
        created_by_user_id="42",
    )
    row_mismatched = _make_row(
        tenant_id=derive_tenant_uuid(platform="discord", workspace_id="B"),
        enabled=True,
        created_by_user_id="42",
    )
    entry = _make_entry()
    entry = RoutineEntry(
        routine=row_local,
        agent_name="a",
        glyph=entry.glyph,
        color=entry.color,
        label="t",
    )
    session = _mock_session_with_row(row_mismatched)
    runtime = _runtime_with_session(session)
    view = RoutinesPanelView(_make_state([entry]), runtime=runtime, allowed_user_id=42)
    pause_btn = next(b for b in view.walk_children() if isinstance(b, _PauseButton))
    pause_btn._view = view  # type: ignore[attr-defined]  # test re-binding

    invoked: list[Any] = []

    async def _fake_get_routine(_s: Any, _id: Any) -> RoutineRow:
        return row_mismatched

    async def _fake_pause(_s: Any, _id: Any) -> RoutineRow:
        invoked.append(_id)
        return row_mismatched

    monkeypatch.setattr(
        "daimon.adapters.discord.routines_panel.panel.get_routine", _fake_get_routine
    )
    monkeypatch.setattr(
        "daimon.adapters.discord.routines_panel.panel.pause_routine_via_panel",
        _fake_pause,
    )

    interaction = MagicMock()
    interaction.user = MagicMock()
    interaction.user.id = 42
    interaction.user.guild_permissions.manage_guild = False
    interaction.guild_id = "A"  # local view's guild
    interaction.response.send_message = AsyncMock()

    await pause_btn.callback(interaction)
    interaction.response.send_message.assert_called_once()
    args, kwargs = interaction.response.send_message.call_args
    text = args[0] if args else kwargs.get("content", "")
    assert "does not belong to this guild" in text, (
        "cross-guild routine ids must be rejected with an explicit message"
    )
    assert invoked == [], "no UPDATE must be issued for a cross-guild row"


def test_picker_is_attached_to_view() -> None:
    """Smoke: the _RoutinePicker class is actually the picker the view uses."""
    state = _make_state([_make_entry()])
    view = RoutinesPanelView(state, runtime=_make_runtime(), allowed_user_id=42)
    pickers = [c for c in view.walk_children() if isinstance(c, _RoutinePicker)]
    assert len(pickers) == 1, "view must attach exactly one _RoutinePicker instance"


# ---------------------------------------------------------------------------
# R3 V2 container builder tests (build_panel_container)
# ---------------------------------------------------------------------------


def _collect_text_display_content(container: discord.ui.Container[Any]) -> list[str]:
    """Walk container children and return .content for every TextDisplay."""
    result: list[str] = []
    for child in container.children:
        if isinstance(child, discord.ui.TextDisplay):
            result.append(child.content)
    return result


_NOW = datetime(2026, 5, 14, 12, 0, tzinfo=UTC)


def test_panel_container_header_contains_trigger_message() -> None:
    entry = _make_entry(trigger_message="daily standup")
    state = _make_state([entry])
    container = build_panel_container(state, now=_NOW)
    texts = _collect_text_display_content(container)
    assert any("daily standup" in t for t in texts), (
        "header TextDisplay must contain the trigger message"
    )


def test_panel_container_subtext_contains_cron_and_agent() -> None:
    entry = _make_entry(trigger_message="standup", cron_expr="0 9 * * 1-5")
    entry_with_agent = RoutineEntry(
        routine=entry.routine,
        agent_name="my-agent",
        glyph=entry.glyph,
        color=entry.color,
        label=entry.label,
    )
    state = _make_state([entry_with_agent])
    container = build_panel_container(state, now=_NOW)
    texts = _collect_text_display_content(container)
    header_text = texts[0]
    assert "0 9 * * 1-5" in header_text, "subtext line must contain the cron expression"
    assert "my-agent" in header_text, "subtext line must contain the agent name"
    # Cron/agent must NOT appear as separate body groups
    body_texts = texts[1:]
    for body in body_texts:
        assert "**Agent**" not in body, "agent must be demoted to subtext, not a body group"
        assert "**Schedule**" not in body, "schedule must be demoted to subtext, not a body group"


def test_panel_container_body_contains_next_run_line() -> None:
    next_fire = datetime(2026, 5, 14, 13, 0, tzinfo=UTC)  # 1h from _NOW
    entry = _make_entry(next_fire_at=next_fire)
    state = _make_state([entry])
    container = build_panel_container(state, now=_NOW)
    texts = _collect_text_display_content(container)
    body_texts = texts[1:]
    assert any("⏱ **Next run in " in t for t in body_texts), (
        "body must contain '⏱ **Next run in …' timeline line"
    )


def test_panel_container_body_contains_last_run_dim_line_for_prior_run() -> None:
    next_fire = datetime(2026, 5, 14, 13, 0, tzinfo=UTC)
    last_fire = datetime(2026, 5, 14, 11, 0, tzinfo=UTC)  # 1h before _NOW
    entry = _make_entry(next_fire_at=next_fire, last_fired_at=last_fire, last_error=None)
    state = _make_state([entry])
    container = build_panel_container(state, now=_NOW)
    texts = _collect_text_display_content(container)
    body_texts = texts[1:]
    assert any("-# last run " in t for t in body_texts), (
        "body must contain '-# last run …' dim line for a routine with a prior run"
    )


def test_panel_container_no_last_run_dim_line_for_never_run() -> None:
    entry = _make_entry(last_fired_at=None)
    state = _make_state([entry])
    container = build_panel_container(state, now=_NOW)
    texts = _collect_text_display_content(container)
    body_texts = texts[1:]
    assert not any("-# last run" in t for t in body_texts), (
        "never-run routine must not render a '-# last run' dim line"
    )


def test_panel_container_paused_state_has_accent_colour() -> None:
    entry = _make_entry(enabled=False)  # paused
    state = _make_state([entry])
    container = build_panel_container(state, now=_NOW)
    assert container.accent_colour == theme.COLOR_PAUSED, (
        "paused-state container must carry the paused color as accent"
    )


def test_panel_container_pending_first_run_has_no_accent() -> None:
    entry = _make_entry(enabled=True, last_fired_at=None)  # blurple / never run
    state = _make_state([entry])
    container = build_panel_container(state, now=_NOW)
    assert container.accent_colour is None, (
        "blurple-pending container must have no accent (F5 default = no accent)"
    )


def test_panel_container_empty_roster_returns_container_with_hint() -> None:
    state = _make_state([])
    container = build_panel_container(state, now=_NOW)
    texts = _collect_text_display_content(container)
    assert any("No routines yet" in t for t in texts), (
        "empty-roster container must show the no-routines hint"
    )
    assert container.accent_colour is None, "empty-roster container must have no accent"
