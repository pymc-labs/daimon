"""Behavioural tests for the direct New agent form.

The form is the one creation shortcut, so its contract is narrow and strict: it
validates against the real catalog, fires no turn and checks no admission, and
lands the reader on the created agent's Details carrying the honest next step.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import httpx
import pytest
from daimon.adapters.discord.agent_setup import new_agent as new_agent_mod
from daimon.adapters.discord.agent_setup import write as write_mod
from daimon.adapters.discord.agent_setup.details_view import DetailsView
from daimon.adapters.discord.agent_setup.new_agent import NewAgentModal
from daimon.adapters.discord.agent_setup.state import PanelState
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.constants import DEFAULT_AGENT_MODEL
from daimon.core.defaults.report import Action, ResourceOutcome
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.models_catalog import list_model_choices
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import DeploymentDefault
from daimon.core.specs import AgentSpec
from daimon.testing import ma_agent
from daimon.testing.factories import make_tenant
from daimon.testing.ma import build_stub_anthropic
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_CREATED_ID = "ag_churn"
_CREATED_NAME = "churn-explorer"
_GUILD_ID = 2001
# `hydrate` derives the tenant from the guild the panel was opened in, so the
# fixture id would not match what the Details read asks for.
_TENANT_ID = derive_tenant_uuid(platform="discord", workspace_id=str(_GUILD_ID))


def _runtime(anthropic: Any, *, sessionmaker: Any = None) -> DiscordRuntime:
    settings = MagicMock()
    settings.mcp.public_url = None
    settings.github.fallback_pat = None
    settings.github.app_id = None
    return DiscordRuntime(
        settings=settings,
        anthropic=anthropic,
        sessionmaker=sessionmaker if sessionmaker is not None else MagicMock(),
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
        turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]  # never runs a turn
    )


def _state(account_id: uuid.UUID, *, guild_account_id: uuid.UUID | None = None) -> PanelState:
    return PanelState(
        roster=[],
        selected=None,
        account_id=account_id,
        guild_account_id=guild_account_id or uuid.UUID("00000000-0000-0000-0000-000000004444"),
        is_admin=False,
        guild_id=_GUILD_ID,
        channel_id=900,
        channel_name="growth",
        deployment_default=DeploymentDefault(),
    )


def _modal(state: PanelState, *, runtime: DiscordRuntime) -> NewAgentModal:
    return NewAgentModal(state, runtime=runtime, allowed_user_id=42)


def _fill(
    modal: NewAgentModal, *, name: str = _CREATED_NAME, model: str = "claude-sonnet-4-6"
) -> None:
    name_field = modal.name_label.component
    assert isinstance(name_field, discord.ui.TextInput)
    name_field._value = name  # pyright: ignore[reportPrivateUsage]  # TextInput private value
    prompt_field = modal.prompt_label.component
    assert isinstance(prompt_field, discord.ui.TextInput)
    prompt_field._value = "looks at churn"  # pyright: ignore[reportPrivateUsage]
    model_field = modal.model_label.component
    assert isinstance(model_field, discord.ui.Select)
    model_field._values = [model]  # pyright: ignore[reportPrivateUsage]  # Select private values


def _interaction() -> MagicMock:
    interaction = MagicMock()
    interaction.user = MagicMock(spec=discord.Member)
    interaction.user.id = 42
    interaction.guild_id = _GUILD_ID
    interaction.response.is_done = MagicMock(return_value=False)
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.followup.send = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    return interaction


def _walk(item: Any) -> list[Any]:
    found = [item]
    for child in getattr(item, "children", []) or []:
        found.extend(_walk(child))
    accessory = getattr(item, "accessory", None)
    if accessory is not None:
        found.extend(_walk(accessory))
    return found


def _text(item: Any) -> str:
    return "\n".join(
        str(node.content) for node in _walk(item) if isinstance(node, discord.ui.TextDisplay)
    )


def _ma_handler(*, seen: list[str], list_after_call: int = 0) -> Any:
    """Serve the org listing and the created agent; refuse anything a turn would need.

    ``list_after_call`` keeps the org empty for that many `/v1/agents` listings,
    so a test driving the real `create_blank_agent` gets past its tenant-wide
    name-collision check before the agent appears.
    """
    created = ma_agent(
        id=_CREATED_ID,
        name=_CREATED_NAME,
        description="looks at churn",
        tenant_id=_TENANT_ID,
        model="claude-sonnet-4-6",
    )
    listings = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal listings
        seen.append(f"{request.method} {request.url.path}")
        if request.method == "GET" and request.url.path == "/v1/agents":
            listings += 1
            items = [] if listings <= list_after_call else [created.model_dump(mode="json")]
            return httpx.Response(200, json={"data": items, "next_page": None})
        if request.method == "GET" and request.url.path == f"/v1/agents/{_CREATED_ID}":
            return httpx.Response(200, json=created.model_dump(mode="json"))
        if request.method == "GET" and request.url.path == "/v1/skills":
            return httpx.Response(200, json={"data": [], "next_page": None})
        raise AssertionError(f"unexpected MA request: {request.method} {request.url.path}")

    return handler


async def _drive_submit(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    *,
    account_id: uuid.UUID,
) -> tuple[PanelState, MagicMock, list[str]]:
    """Run a successful submit end to end and hand back the state and the MA traffic."""
    await make_tenant(db_session, platform="discord", workspace_id=str(_GUILD_ID), id=_TENANT_ID)
    await db_session.commit()

    seen: list[str] = []
    runtime = _runtime(
        build_stub_anthropic(_ma_handler(seen=seen)), sessionmaker=db_session_factory
    )

    async def _fake_create(*_args: Any, **_kwargs: Any) -> ResourceOutcome:
        return ResourceOutcome(
            kind="agent", name=_CREATED_NAME, action=Action.CREATED, anthropic_id=_CREATED_ID
        )

    monkeypatch.setattr(new_agent_mod, "create_blank_agent", _fake_create)
    monkeypatch.setattr(
        new_agent_mod, "resolve_tenant_for_panel", AsyncMock(return_value=_TENANT_ID)
    )

    state = _state(account_id)
    modal = _modal(state, runtime=runtime)
    _fill(modal)
    interaction = _interaction()
    await modal.on_submit(interaction)
    return state, interaction, seen


# ---------------------------------------------------------------------------
# Modal shape
# ---------------------------------------------------------------------------


def test_new_agent_modal_has_three_children(account_id: uuid.UUID) -> None:
    """Name, purpose and model are the modal's only three top-level components."""
    modal = _modal(_state(account_id), runtime=_runtime(build_stub_anthropic()))
    assert len(modal.children) == 3, "NewAgentModal must have exactly 3 top-level components"


def test_new_agent_modal_model_select_options_match_catalog(account_id: uuid.UUID) -> None:
    modal = _modal(_state(account_id), runtime=_runtime(build_stub_anthropic()))
    model_field = modal.model_label.component
    assert isinstance(model_field, discord.ui.Select), "Model field must be a Select"

    expected = list_model_choices(default=DEFAULT_AGENT_MODEL)
    assert len(model_field.options) == len(expected), (
        "the Select must offer exactly the catalog's choices"
    )
    for option, choice in zip(model_field.options, expected, strict=True):
        assert option.value == choice.id, "option value must be the model id"
        assert option.label == choice.label, "option label must be the catalog display name"
        assert option.description == choice.description, (
            "option description must match the catalog entry"
        )
        assert option.default == choice.is_default, (
            "option default flag must match the catalog's is_default"
        )


def test_new_agent_modal_default_option_preselected(account_id: uuid.UUID) -> None:
    modal = _modal(_state(account_id), runtime=_runtime(build_stub_anthropic()))
    model_field = modal.model_label.component
    assert isinstance(model_field, discord.ui.Select), "Model field must be a Select"

    default_options = [option for option in model_field.options if option.default]
    assert len(default_options) == 1, "exactly one option must be preselected"
    assert default_options[0].value == DEFAULT_AGENT_MODEL, (
        "the preselected option must be the configured default model"
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


async def test_submit_rejects_a_retired_model_id_without_creating(
    monkeypatch: pytest.MonkeyPatch, account_id: uuid.UUID
) -> None:
    """A stale client can still submit a retired model id; validation must refuse it."""
    create_called = False

    async def _fake_create(*_args: Any, **_kwargs: Any) -> ResourceOutcome:
        nonlocal create_called
        create_called = True
        raise AssertionError("an invalid model must never reach create_blank_agent")

    monkeypatch.setattr(new_agent_mod, "create_blank_agent", _fake_create)

    modal = _modal(_state(account_id), runtime=_runtime(build_stub_anthropic()))
    _fill(modal, model="claude-retired-99")
    interaction = _interaction()

    await modal.on_submit(interaction)

    interaction.response.send_message.assert_called_once()
    message = interaction.response.send_message.call_args.args[0]
    assert "claude-retired-99" in message, "the error must name the model that was refused"
    interaction.response.defer.assert_not_called()
    assert not create_called, "a retired model id must never reach create_blank_agent"


# ---------------------------------------------------------------------------
# Creation outcome
# ---------------------------------------------------------------------------


async def test_submit_renders_the_created_agents_details_not_the_roster(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    account_id: uuid.UUID,
) -> None:
    state, interaction, _seen = await _drive_submit(
        db_session, db_session_factory, monkeypatch, account_id=account_id
    )

    interaction.edit_original_response.assert_called_once()
    view = interaction.edit_original_response.call_args.kwargs["view"]
    assert isinstance(view, DetailsView), "creation must land on the new agent's Details"
    assert view.details.name == _CREATED_NAME, "the card must describe the agent just created"
    assert state.selected_agent is not None and state.selected_agent.name == _CREATED_NAME, (
        "the created agent becomes the panel's selection"
    )
    assert interaction.edit_original_response.call_args.kwargs["allowed_mentions"].users is False, (
        "every panel render suppresses mentions"
    )


async def test_created_details_show_the_unrouted_note_and_the_routing_request(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    account_id: uuid.UUID,
) -> None:
    """ "Created" must not imply "available by mention"."""
    _state_after, interaction, _seen = await _drive_submit(
        db_session, db_session_factory, monkeypatch, account_id=account_id
    )

    view = interaction.edit_original_response.call_args.kwargs["view"]
    text = _text(view)
    assert "Not answering in any channel yet." in text, (
        "a freshly created agent answers nowhere and must say so"
    )
    assert f"An admin can tell Daimon: make {_CREATED_NAME} answer in #growth." in text, (
        "a member gets the exact request to hand an admin"
    )
    assert "answers in" not in text, "nothing may claim the new agent is reachable"


async def test_submit_opens_no_session_and_checks_no_admission(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    account_id: uuid.UUID,
) -> None:
    """Creation and inspection stay available when a billed turn could not be admitted."""
    import daimon.core.turn.admission as admission_mod

    async def _unexpected_admit(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("creating an agent must never run a billing admission check")

    monkeypatch.setattr(admission_mod, "admit", _unexpected_admit)

    _state_after, _interaction, seen = await _drive_submit(
        db_session, db_session_factory, monkeypatch, account_id=account_id
    )

    assert not any("/v1/sessions" in call for call in seen), (
        f"creation must open no MA session; MA saw {seen}"
    )
    assert not any("/v1/messages" in call for call in seen), (
        f"creation must run no turn; MA saw {seen}"
    )


async def test_setup_button_targets_the_newly_created_agent(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    account_id: uuid.UUID,
) -> None:
    """Manage on the returned card must configure the agent just created."""
    import daimon.adapters.discord.agent_setup.details_view as details_view_mod
    from daimon.core.setup_conversations import setup_target_label

    state, interaction, _seen = await _drive_submit(
        db_session, db_session_factory, monkeypatch, account_id=account_id
    )
    view = interaction.edit_original_response.call_args.kwargs["view"]

    captured: dict[str, Any] = {}

    async def _spy_open(_interaction: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(details_view_mod, "open_setup_conversation", _spy_open)
    setup_button = next(
        node
        for node in _walk(view)
        if isinstance(node, discord.ui.Button) and node.label == setup_target_label(_CREATED_NAME)
    )
    await setup_button.callback(_interaction())

    assert captured["target"] is state.selected_agent, (
        "Manage must carry the newly created agent as its target"
    )
    assert captured["target"].name == _CREATED_NAME


async def test_submit_stamps_the_guild_account_not_the_personal_one(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    account_id: uuid.UUID,
) -> None:
    """A panel-created agent is owned by the server, and must not be sweep-eligible."""
    guild_account = uuid.UUID("00000000-0000-0000-0000-000000004444")
    assert guild_account != account_id, "test setup: the two accounts must differ"

    await make_tenant(db_session, platform="discord", workspace_id=str(_GUILD_ID), id=_TENANT_ID)
    await db_session.commit()

    captured: dict[str, Any] = {}

    async def _spy_reconcile(
        _client: Any,
        spec: AgentSpec,
        *,
        tenant_id: uuid.UUID,
        dry_run: bool,
        account_id: uuid.UUID | None = None,
        public_url: str | None = None,
        managed: bool = True,
    ) -> ResourceOutcome:
        captured["spec"] = spec
        captured["account_id"] = account_id
        captured["managed"] = managed
        assert dry_run is False, "creation is not a dry run"
        assert public_url is None, "this deployment has no public MCP url configured"
        assert tenant_id is not None
        return ResourceOutcome(
            kind="agent", name=spec.name, action=Action.CREATED, anthropic_id=_CREATED_ID
        )

    monkeypatch.setattr(write_mod, "reconcile_agent", _spy_reconcile)
    monkeypatch.setattr(
        new_agent_mod, "resolve_tenant_for_panel", AsyncMock(return_value=_TENANT_ID)
    )

    seen: list[str] = []
    runtime = _runtime(
        build_stub_anthropic(_ma_handler(seen=seen, list_after_call=1)),
        sessionmaker=db_session_factory,
    )
    state = _state(account_id, guild_account_id=guild_account)
    modal = _modal(state, runtime=runtime)
    _fill(modal)

    await modal.on_submit(_interaction())

    assert captured["spec"].name == _CREATED_NAME, "the spec name comes from the form"
    assert captured["spec"].system == "looks at churn", "the purpose becomes the system prompt"
    assert captured["account_id"] == guild_account, (
        "the ownership stamp is the guild account, not the clicker's personal account"
    )
    assert captured["managed"] is False, (
        "a guild-owned agent must not be daimon_managed — the next defaults apply would archive it"
    )


# ---------------------------------------------------------------------------
# Failure
# ---------------------------------------------------------------------------


async def test_unexpected_failure_captures_sentry_with_tenant_context(
    monkeypatch: pytest.MonkeyPatch, account_id: uuid.UUID
) -> None:
    captured: list[BaseException] = []
    bound: dict[str, str] = {}

    def _spy_capture(err: BaseException) -> None:
        import structlog

        bound.update(structlog.contextvars.get_contextvars())
        captured.append(err)

    async def _boom(*_args: Any, **_kwargs: Any) -> ResourceOutcome:
        raise RuntimeError("MA fell over")

    monkeypatch.setattr(new_agent_mod, "capture_exception_with_scope", _spy_capture)
    monkeypatch.setattr(new_agent_mod, "create_blank_agent", _boom)
    monkeypatch.setattr(
        new_agent_mod, "resolve_tenant_for_panel", AsyncMock(return_value=_TENANT_ID)
    )

    modal = _modal(_state(account_id), runtime=_runtime(build_stub_anthropic()))
    _fill(modal)
    interaction = _interaction()

    await modal.on_submit(interaction)

    assert len(captured) == 1, "an unexpected failure must reach Sentry"
    assert isinstance(captured[0], RuntimeError), "the original exception is captured"
    assert bound.get("tenant_id") == str(_TENANT_ID), "the Sentry event must be tenant-attributable"
    assert bound.get("guild_id") == "2001", "the guild is bound alongside the tenant"
    interaction.followup.send.assert_called_once()
    assert interaction.followup.send.call_args.kwargs["ephemeral"] is True, (
        "the failure is reported privately"
    )


async def test_a_daimon_error_is_rendered_without_a_sentry_capture(
    monkeypatch: pytest.MonkeyPatch, account_id: uuid.UUID
) -> None:
    """A name collision is an expected outcome, not a defect to page someone about."""
    from daimon.core.errors import DaimonError

    captured: list[BaseException] = []

    async def _collide(*_args: Any, **_kwargs: Any) -> ResourceOutcome:
        raise DaimonError("This server already has an agent named **churn-explorer**.")

    monkeypatch.setattr(
        new_agent_mod, "capture_exception_with_scope", lambda err: captured.append(err)
    )
    monkeypatch.setattr(new_agent_mod, "create_blank_agent", _collide)
    monkeypatch.setattr(
        new_agent_mod, "resolve_tenant_for_panel", AsyncMock(return_value=_TENANT_ID)
    )

    modal = _modal(_state(account_id), runtime=_runtime(build_stub_anthropic()))
    _fill(modal)
    interaction = _interaction()

    await modal.on_submit(interaction)

    assert captured == [], "an expected refusal must not be captured as an exception"
    interaction.followup.send.assert_called_once()
    message = interaction.followup.send.call_args.args[0]
    assert "already has an agent named" in message, "the caller sees the real reason"
