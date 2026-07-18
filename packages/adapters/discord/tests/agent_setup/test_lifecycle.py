"""Lifecycle tests for New / Fork / Delete on the AgentSetupView."""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import httpx
import pytest
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.adapters.discord.agent_setup import write as write_mod
from daimon.adapters.discord.agent_setup.panel import (
    AgentSetupView,
    ForkAgentModal,
    NewAgentModal,
)
from daimon.adapters.discord.agent_setup.state import PanelState, RosterEntry
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.defaults.report import Action, ResourceOutcome
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import DeploymentDefault
from daimon.core.specs import AgentSpec
from daimon.testing.ma import build_stub_anthropic
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _find_button(view: discord.ui.LayoutView, label: str) -> discord.ui.Button[AgentSetupView]:
    """Find a button by label, walking into LayoutView's nested Container→ActionRow."""
    for child in view.children:
        if isinstance(child, discord.ui.Button) and child.label == label:
            return child  # type: ignore[return-value]  # generic Button
        # LayoutView: buttons live inside Container > ActionRow
        if isinstance(child, discord.ui.Container):
            for grandchild in child.children:  # type: ignore[union-attr]  # Container.children is partially unknown
                if isinstance(grandchild, discord.ui.Button) and grandchild.label == label:
                    return grandchild  # type: ignore[return-value]
                if isinstance(grandchild, discord.ui.ActionRow):
                    for item in grandchild.children:  # type: ignore[union-attr]  # ActionRow.children is partially unknown
                        if isinstance(item, discord.ui.Button) and item.label == label:
                            return item  # type: ignore[return-value]
    raise AssertionError(f"No button labeled {label!r}")


def _entry(
    name: str, *, system: str | None = None, mcp_servers: list[Any] | None = None
) -> RosterEntry:
    return RosterEntry(
        name=name,
        model="claude-sonnet-4-6",
        spec=AgentSpec(
            name=name,
            model="claude-sonnet-4-6",
            system=system,
            mcp_servers=mcp_servers,
        ),
    )


def _runtime(
    anthropic: Any,
    tenant_id: uuid.UUID,
    *,
    sessionmaker: Any = None,
) -> DiscordRuntime:
    # tenant_id param retained for call-site readability; the runtime no longer
    # carries it — callbacks resolve tenant per-interaction.
    _ = tenant_id
    settings = MagicMock()
    settings.mcp.public_url = None
    settings.crypto.keys = ()
    settings.github.oauth_scopes = ("repo", "read:user")
    return DiscordRuntime(
        settings=settings,
        anthropic=anthropic,
        sessionmaker=sessionmaker if sessionmaker is not None else MagicMock(),
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
    )


@pytest.mark.asyncio
async def test_new_agent_calls_reconcile_with_blank_spec_and_account_id(
    monkeypatch: pytest.MonkeyPatch,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
) -> None:
    """SC-2: NewAgentModal stamps the guild account, not the personal account."""
    captured: dict[str, Any] = {}

    # DISTINCT guild account so regression (using personal account) fails loudly.
    guild_account = uuid.UUID("00000000-0000-0000-0000-000000004444")
    assert guild_account != account_id, (
        "test setup: guild account must differ from personal account"
    )

    async def spy_reconcile(
        client: Any,
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
        return ResourceOutcome(
            kind="agent", name=spec.name, action=Action.CREATED, anthropic_id="ag_new"
        )

    monkeypatch.setattr(write_mod, "reconcile_agent", spy_reconcile)

    # No agents on MA initially — list returns []
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/skills":
            return httpx.Response(200, json={"data": [], "next_page": None})
        if request.method == "GET" and request.url.path == "/v1/agents":
            return httpx.Response(200, json={"data": [], "next_page": None})
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    runtime = _runtime(build_stub_anthropic(handler), tenant_id)
    state = PanelState.initial(
        roster=[],
        account_id=account_id,
        platform_principal_id=uuid.uuid4(),
        guild_account_id=guild_account,
    )

    modal = NewAgentModal(state, runtime=runtime, allowed_user_id=42)
    # Simulate the user typing into the TextInput components
    modal.name_in._value = "research-bot"  # pyright: ignore[reportPrivateUsage]  # TextInput private value
    modal.prompt_in._value = "be helpful"  # pyright: ignore[reportPrivateUsage]
    modal.model_in._value = "claude-sonnet-4-6"  # pyright: ignore[reportPrivateUsage]

    interaction = MagicMock()
    interaction.user.id = 42
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()

    await modal.on_submit(interaction)

    assert "spec" in captured, "new-agent submit must invoke reconcile_agent"
    assert captured["spec"].name == "research-bot", "spec name must come from modal input"
    assert captured["account_id"] == guild_account, (
        "SC-2: new-agent submit must stamp the guild account (guild_account_id), "
        "not the personal account_id — the panel passes state.guild_account_id"
    )
    assert captured["account_id"] != account_id, (
        "SC-2: personal account must not be used as the ownership stamp"
    )
    assert captured["managed"] is False, (
        "new-agent submit creates a guild-owned agent — managed=True would make it "
        "sweep-eligible, archived on the next deploy's defaults apply"
    )


def _source_ma_agent(
    *, name: str, tenant_id: uuid.UUID, account_id: uuid.UUID | None, system: str | None
) -> dict[str, Any]:
    """Return the MA agent JSON payload for a fork-source — matches list & retrieve responses."""
    metadata: dict[str, str] = {"daimon_tenant": str(tenant_id), "daimon_name": name}
    if account_id is not None:
        metadata["daimon_account"] = str(account_id)
    return BetaManagedAgentsAgent(
        id="ag_source",
        type="agent",
        name=name,
        model={"id": "claude-sonnet-4-6"},  # type: ignore[arg-type]
        metadata=metadata,
        description=None,
        archived_at=None,
        created_at="2026-05-01T00:00:00Z",  # type: ignore[arg-type]
        updated_at="2026-05-01T00:00:00Z",  # type: ignore[arg-type]
        version=1,
        mcp_servers=[],
        skills=[],
        tools=[],
        system=system,
    ).model_dump(mode="json")


@pytest.mark.asyncio
async def test_fork_creates_new_ma_agent_via_direct_create(
    db_session_factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
) -> None:
    """Fork calls `agents.create` directly (mirrors CLI) — no reconcile, no name-dedup.
    SC-2: fork stamps the guild account (guild_account_id), not the personal account.
    """
    # DISTINCT guild account so regression (using personal account) fails loudly.
    guild_account = uuid.UUID("00000000-0000-0000-0000-000000005555")
    assert guild_account != account_id, (
        "test setup: guild account must differ from personal account"
    )

    source_payload = _source_ma_agent(
        name="source-bot", tenant_id=tenant_id, account_id=None, system="be helpful"
    )
    create_calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/skills":
            return httpx.Response(200, json={"data": [], "next_page": None})
        if request.method == "GET" and request.url.path == "/v1/agents":
            return httpx.Response(200, json={"data": [source_payload], "next_page": None})
        if request.method == "GET" and request.url.path == "/v1/agents/ag_source":
            return httpx.Response(200, json=source_payload)
        if request.method == "POST" and request.url.path == "/v1/agents":
            body = json.loads(request.content)
            create_calls.append(body)
            return httpx.Response(
                200,
                json={
                    **source_payload,
                    "id": "ag_forked",
                    "name": body["name"],
                    "metadata": body["metadata"],
                },
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    runtime = _runtime(build_stub_anthropic(handler), tenant_id, sessionmaker=db_session_factory)
    source = _entry("source-bot", system="be helpful")
    state = PanelState(
        roster=[source],
        selected=source,
        account_id=account_id,
        guild_account_id=guild_account,
    )

    modal = ForkAgentModal(state, runtime=runtime, allowed_user_id=42)
    modal.name_in._value = "source-bot-v2"  # pyright: ignore[reportPrivateUsage]

    interaction = MagicMock()
    interaction.user.id = 42
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    interaction.followup.send = AsyncMock()

    await modal.on_submit(interaction)

    assert len(create_calls) == 1, "fork must POST exactly one create to MA"
    body = create_calls[0]
    assert body["name"] == "source-bot-v2", "fork uses the new name"
    assert body["system"] == "be helpful", "fork carries the source system prompt"
    assert body["metadata"]["daimon_name"] == "source-bot-v2"
    assert body["metadata"]["daimon_account"] == str(guild_account), (
        "SC-2: fork must stamp the guild account (guild_account_id), not the personal account"
    )
    assert body["metadata"]["daimon_account"] != str(account_id), (
        "SC-2: personal account must not be the ownership stamp"
    )
    interaction.followup.send.assert_not_called()


@pytest.mark.asyncio
async def test_fork_rejects_when_name_collides_under_guild_account(
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
) -> None:
    """If the target name already exists under the guild account, fork must refuse — no MA create.
    SC-2: fork checks collision against guild_account_id (not personal account_id).
    """
    guild_account = uuid.UUID("00000000-0000-0000-0000-000000006666")
    assert guild_account != account_id, (
        "test setup: guild account must differ from personal account"
    )

    source_payload = _source_ma_agent(
        name="source-bot", tenant_id=tenant_id, account_id=None, system=None
    )
    # Collision agent is owned by the GUILD account (not the personal account).
    collision_payload = BetaManagedAgentsAgent(
        id="ag_existing_copy",
        type="agent",
        name="source-bot-v2",
        model={"id": "claude-sonnet-4-6"},  # type: ignore[arg-type]
        metadata={
            "daimon_tenant": str(tenant_id),
            "daimon_name": "source-bot-v2",
            "daimon_account": str(guild_account),
        },
        description=None,
        archived_at=None,
        created_at="2026-05-02T00:00:00Z",  # type: ignore[arg-type]
        updated_at="2026-05-02T00:00:00Z",  # type: ignore[arg-type]
        version=1,
        mcp_servers=[],
        skills=[],
        tools=[],
        system=None,
    ).model_dump(mode="json")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/skills":
            return httpx.Response(200, json={"data": [], "next_page": None})
        if request.method == "GET" and request.url.path == "/v1/agents":
            return httpx.Response(
                200, json={"data": [source_payload, collision_payload], "next_page": None}
            )
        if request.method == "POST" and request.url.path == "/v1/agents":
            raise AssertionError("fork must not POST create when target name collides")
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    runtime = _runtime(build_stub_anthropic(handler), tenant_id)
    source = _entry("source-bot")
    state = PanelState(
        roster=[source],
        selected=source,
        account_id=account_id,
        guild_account_id=guild_account,
    )

    modal = ForkAgentModal(state, runtime=runtime, allowed_user_id=42)
    modal.name_in._value = "source-bot-v2"  # pyright: ignore[reportPrivateUsage]

    interaction = MagicMock()
    interaction.user.id = 42
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    interaction.followup.send = AsyncMock()

    await modal.on_submit(interaction)

    interaction.followup.send.assert_awaited_once()
    args, kwargs = interaction.followup.send.call_args
    assert "source-bot-v2" in args[0], "collision error must name the conflicting agent"
    assert kwargs.get("ephemeral") is True, "collision error must be ephemeral"
    interaction.edit_original_response.assert_not_called()


@pytest.mark.asyncio
async def test_fork_blocks_collision_under_different_account(
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
) -> None:
    """Any non-archived agent with the same target name in the tenant blocks fork,
    regardless of which account owns it. Tenant-wide name uniqueness."""
    guild_account = uuid.UUID("00000000-0000-0000-0000-000000007777")
    other_user_account = uuid.UUID("dddddddd-0000-0000-0000-000000000001")
    assert other_user_account != guild_account, (
        "test setup: other account must not be guild account"
    )

    source_payload = _source_ma_agent(
        name="daimon", tenant_id=tenant_id, account_id=None, system="seeded"
    )
    other_users_copy = BetaManagedAgentsAgent(
        id="ag_other_copy",
        type="agent",
        name="daimon-copy",
        model={"id": "claude-sonnet-4-6"},  # type: ignore[arg-type]
        metadata={
            "daimon_tenant": str(tenant_id),
            "daimon_name": "daimon-copy",
            "daimon_account": str(other_user_account),
        },
        description=None,
        archived_at=None,
        created_at="2026-05-02T00:00:00Z",  # type: ignore[arg-type]
        updated_at="2026-05-02T00:00:00Z",  # type: ignore[arg-type]
        version=1,
        mcp_servers=[],
        skills=[],
        tools=[],
        system=None,
    ).model_dump(mode="json")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/skills":
            return httpx.Response(200, json={"data": [], "next_page": None})
        if request.method == "GET" and request.url.path == "/v1/agents":
            return httpx.Response(
                200, json={"data": [source_payload, other_users_copy], "next_page": None}
            )
        if request.method == "GET" and request.url.path == "/v1/agents/ag_source":
            return httpx.Response(200, json=source_payload)
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    runtime = _runtime(build_stub_anthropic(handler), tenant_id)
    source = _entry("daimon", system="seeded")
    state = PanelState(
        roster=[source],
        selected=source,
        account_id=account_id,
        guild_account_id=guild_account,
    )

    modal = ForkAgentModal(state, runtime=runtime, allowed_user_id=42)
    modal.name_in._value = "daimon-copy"  # pyright: ignore[reportPrivateUsage]

    interaction = MagicMock()
    interaction.user.id = 42
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    interaction.followup.send = AsyncMock()

    await modal.on_submit(interaction)

    interaction.followup.send.assert_awaited_once()
    args, kwargs = interaction.followup.send.call_args
    assert "daimon-copy" in args[0], "collision error must name the conflicting agent"
    assert kwargs.get("ephemeral") is True, "collision error must be ephemeral"
    interaction.edit_original_response.assert_not_called()


@pytest.mark.asyncio
async def test_delete_archives_ma_agent_and_jumps_selection(
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
) -> None:
    """Deleting the selected agent must call beta.agents.archive and pick a neighbor."""
    a = _entry("alpha")
    b = _entry("bravo")
    c = _entry("charlie")
    # Mutation buttons only appear for admins.
    state = PanelState(roster=[a, b, c], selected=b, account_id=account_id, is_admin=True)

    archive_calls: list[str] = []
    agents_payload = [
        BetaManagedAgentsAgent(
            id="ag_bravo",
            type="agent",
            name="bravo",
            model={"id": "claude-sonnet-4-6"},  # type: ignore[arg-type]
            metadata={"daimon_tenant": str(tenant_id), "daimon_name": "bravo"},
            description=None,
            archived_at=None,
            created_at="2026-05-01T00:00:00Z",  # type: ignore[arg-type]
            updated_at="2026-05-01T00:00:00Z",  # type: ignore[arg-type]
            version=1,
            mcp_servers=[],
            skills=[],
            tools=[],
            system=None,
        ).model_dump(mode="json"),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/skills":
            return httpx.Response(200, json={"data": [], "next_page": None})
        if request.method == "GET" and request.url.path == "/v1/agents":
            return httpx.Response(200, json={"data": agents_payload, "next_page": None})
        if request.method == "POST" and request.url.path == "/v1/agents/ag_bravo/archive":
            archive_calls.append("ag_bravo")
            return httpx.Response(200, json=agents_payload[0])
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    runtime = _runtime(build_stub_anthropic(handler), tenant_id)
    view = AgentSetupView(state, runtime=runtime, allowed_user_id=42)

    delete_btn = _find_button(view, "Delete")
    interaction = MagicMock()
    interaction.user.id = 42
    interaction.response.defer = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    interaction.edit_original_response = AsyncMock()

    await delete_btn.callback(interaction)

    assert archive_calls == ["ag_bravo"], (
        "delete must call beta.agents.archive on the selected agent's MA id"
    )
    assert state.selected is not None, (
        "after deleting B from [A,B,C], selection must jump to a neighbor"
    )
    assert state.selected.name in {
        "alpha",
        "charlie",
    }, "selection must land on a roster neighbor after delete"
    assert "bravo" not in {e.name for e in state.roster}, (
        "deleted agent must be removed from roster"
    )


@pytest.mark.asyncio
async def test_delete_last_agent_disables_section_buttons(
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
) -> None:
    only = _entry("solo")
    # Mutation buttons only appear for admins.
    state = PanelState(roster=[only], selected=only, account_id=account_id, is_admin=True)

    agents_payload = [
        BetaManagedAgentsAgent(
            id="ag_solo",
            type="agent",
            name="solo",
            model={"id": "claude-sonnet-4-6"},  # type: ignore[arg-type]
            metadata={"daimon_tenant": str(tenant_id), "daimon_name": "solo"},
            description=None,
            archived_at=None,
            created_at="2026-05-01T00:00:00Z",  # type: ignore[arg-type]
            updated_at="2026-05-01T00:00:00Z",  # type: ignore[arg-type]
            version=1,
            mcp_servers=[],
            skills=[],
            tools=[],
            system=None,
        ).model_dump(mode="json"),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/skills":
            return httpx.Response(200, json={"data": [], "next_page": None})
        if request.method == "GET" and request.url.path == "/v1/agents":
            return httpx.Response(200, json={"data": agents_payload, "next_page": None})
        if request.method == "POST" and request.url.path == "/v1/agents/ag_solo/archive":
            return httpx.Response(200, json=agents_payload[0])
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    runtime = _runtime(build_stub_anthropic(handler), tenant_id)
    view = AgentSetupView(state, runtime=runtime, allowed_user_id=42)

    delete_btn = _find_button(view, "Delete")
    interaction = MagicMock()
    interaction.user.id = 42
    interaction.response.defer = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    interaction.edit_original_response = AsyncMock()

    await delete_btn.callback(interaction)

    assert state.selected is None, "selection must be cleared after deleting the last roster entry"
    assert state.roster == [], "roster must be empty after deleting the only entry"
    # interaction.edit_original_response is the post-render call; check it was awaited
    assert interaction.edit_original_response.await_count >= 1, (
        "panel must re-render after delete so the empty-state view replaces the old view"
    )
