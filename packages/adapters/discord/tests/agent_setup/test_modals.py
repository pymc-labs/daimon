"""Tests for the four section modals + Connect-GitHub link helper.

Plan 04 extends the Plan 03 panel: four section modals (Agent / Repo+Auth /
Skills / MCPs) and a separate Connect-GitHub button.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import httpx
import pytest
import structlog
from anthropic.types.beta import BetaManagedAgentsAgent
from anthropic.types.beta.beta_managed_agents_model_config import (
    BetaManagedAgentsModelConfig,
)
from anthropic.types.beta.beta_managed_agents_url_mcp_server_params import (
    BetaManagedAgentsURLMCPServerParams,
)
from cryptography.fernet import Fernet
from daimon.adapters.discord.agent_setup import modals as modals_mod
from daimon.adapters.discord.agent_setup import modals_mcp as modals_mcp_mod
from daimon.adapters.discord.agent_setup import write as write_mod
from daimon.adapters.discord.agent_setup.modals import (
    AddMcpModal,
    AddSkillModal,
    AgentSectionModal,
    RepoAuthModal,
)
from daimon.adapters.discord.agent_setup.panel import build_panel_container
from daimon.adapters.discord.agent_setup.state import PanelState, RosterEntry
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.config import McpSettings
from daimon.core.github_credentials import build_multifernet
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import DeploymentDefault
from daimon.core.skill_sync.orchestrator import SyncReport
from daimon.core.specs import AgentSpec
from daimon.core.stores import github_credentials as creds_store
from daimon.core.stores.agent_repo_binding import get_binding
from daimon.core.stores.identity import get_or_create_cli_principal
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import build_stub_anthropic
from pydantic import HttpUrl, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _entry(
    name: str, *, mcp_servers: list[Any] | None = None, is_system: bool = False
) -> RosterEntry:
    return RosterEntry(
        name=name,
        model="claude-sonnet-4-6",
        spec=AgentSpec(name=name, model="claude-sonnet-4-6", mcp_servers=mcp_servers),
        is_system=is_system,
    )


_DEFAULT_PUBLIC_URL = HttpUrl("https://example.com/mcp")


def _runtime_for_view(
    *,
    anthropic: Any,
    tenant_id: uuid.UUID,
    public_url: HttpUrl | None = _DEFAULT_PUBLIC_URL,
    sessionmaker: Any = None,
    crypto_keys: tuple[str, ...] = (),
    deployment_default: DeploymentDefault | None = None,
) -> DiscordRuntime:
    settings = MagicMock()
    # Real McpSettings so the app_root_url property (strips /mcp) computes for real.
    # jwt_secret must be present so the per-agent vault bootstrap path can mint a JWT.
    settings.mcp = McpSettings(public_url=public_url, jwt_secret=SecretStr("a" * 32))
    settings.github.oauth_scopes = ("repo", "read:user")
    # The bind path no longer reads App credentials at all; set to None so a
    # stray MagicMock read elsewhere doesn't look truthy by accident.
    settings.github.app_id = None
    settings.github.app_private_key = None
    # crypto.keys is tuple[SecretStr, ...]; tests pass plain Fernet keys and wrap.
    settings.crypto.keys = tuple(MagicMock(get_secret_value=lambda k=k: k) for k in crypto_keys)
    _ = tenant_id  # runtime no longer carries tenant_id; resolved per-interaction
    return DiscordRuntime(
        settings=settings,
        anthropic=anthropic,
        sessionmaker=sessionmaker if sessionmaker is not None else MagicMock(),
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=deployment_default
        if deployment_default is not None
        else DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
        # fernet is real: the MCP modal encrypts an agent-scoped copy of the token.
        turn_deps=MagicMock(  # pyright: ignore[reportArgumentType]  # never runs a turn
            fernet=build_multifernet((Fernet.generate_key().decode(),))
        ),
    )


def _interaction(user_id: int = 42, *, is_admin: bool = True, guild_id: int = 12345) -> MagicMock:
    """A discord.Interaction stand-in, a live guild admin by default so the
    write-callback's authz gate short-circuits without a DB read. Tests
    proving the reachable/system-agent refusal pass ``is_admin=False``."""
    interaction = MagicMock()
    interaction.guild_id = guild_id
    interaction.user = MagicMock(spec=discord.Member)
    interaction.user.id = user_id
    interaction.user.guild_permissions.administrator = is_admin
    interaction.user.guild_permissions.manage_guild = False
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.send_modal = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    interaction.response.is_done = MagicMock(return_value=False)
    interaction.followup.send = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    return interaction


# ----- 1. PAT masked in embed and logs -----


@pytest.mark.asyncio
async def test_pat_masked_in_embed_and_logs(
    monkeypatch: pytest.MonkeyPatch, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> None:
    """Plaintext PAT must never appear in logs or embed."""
    plaintext = "ghp_1234567890"

    async def fake_store_inline_pat(
        runtime: Any, *, account_id: uuid.UUID, agent_id: uuid.UUID, plaintext_pat: str
    ) -> str:
        return "inline-pat:test"

    async def fake_set_binding(*args: Any, **kwargs: Any) -> Any:
        return MagicMock()

    async def fake_reconcile(runtime: Any, state: PanelState, *, tenant_id: uuid.UUID) -> Any:
        return MagicMock()

    async def fake_find(*args: Any, **kwargs: Any) -> Any:
        # Real MA returns a BetaManagedAgentsAgent whose `id` is a prefixed
        # string like `agent_017vXaNG5P7Fu1g4orggSwEY` — NOT a UUID. This shape
        # is what surfaces BUG-25-01 (modals.py does `uuid.UUID(str(ma_agent.id))`
        # which crashes on prefixed strings).
        from datetime import UTC, datetime

        now = datetime.now(UTC)
        return BetaManagedAgentsAgent(
            id="agent_017vXaNG5P7Fu1g4orggSwEY",
            type="agent",
            name="a",
            version=1,
            model=BetaManagedAgentsModelConfig(id="claude-sonnet-4-6", speed="standard"),
            created_at=now,
            updated_at=now,
            metadata={"daimon_tenant": str(tenant_id), "daimon_name": "a"},
            mcp_servers=[],
            tools=[],
            skills=[],
        )

    monkeypatch.setattr(modals_mod, "store_inline_pat", fake_store_inline_pat)
    monkeypatch.setattr(modals_mod, "set_agent_repo_binding", fake_set_binding)
    monkeypatch.setattr(modals_mod, "call_reconcile_for_panel", fake_reconcile)
    monkeypatch.setattr(modals_mod, "find_agent_by_daimon_tag", fake_find)

    selected = _entry("a")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = _runtime_for_view(anthropic=build_stub_anthropic(), tenant_id=tenant_id)

    async def fake_pat_access(*args: Any, **kwargs: Any) -> bool:
        return True

    monkeypatch.setattr(modals_mod, "pat_can_access_repo", fake_pat_access)

    modal = RepoAuthModal(state, runtime=runtime, allowed_user_id=42)
    modal.url_in._value = "https://github.com/me/repo"  # pyright: ignore[reportPrivateUsage]
    modal.branch_in._value = "main"  # pyright: ignore[reportPrivateUsage]
    modal.pat_in._value = plaintext  # pyright: ignore[reportPrivateUsage]

    with structlog.testing.capture_logs() as captured:
        await modal.on_submit(_interaction())

    rendered = "\n".join(str(rec) for rec in captured)
    assert plaintext not in rendered, "plaintext PAT must never appear in log records"
    assert "****7890" in rendered, "masked PAT tail must appear in log records"
    assert state.pat_last4 == "7890", "PanelState.pat_last4 carries the last-4 only"

    container = build_panel_container(state, thumbnail_url=None)
    # Collect all text from TextDisplay children inside the container.
    container_text_parts: list[str] = []
    for child in container.children:
        if isinstance(child, discord.ui.TextDisplay):
            container_text_parts.append(child.content)
        elif isinstance(child, discord.ui.Section):
            # Section may contain a TextDisplay as its content child.
            for section_child in child.children:
                if isinstance(section_child, discord.ui.TextDisplay):
                    container_text_parts.append(section_child.content)
    container_text = "\n".join(container_text_parts)
    assert plaintext not in container_text, "plaintext PAT must never appear in panel container"
    assert "••••7890" in container_text, (
        "panel container confirms a PAT is set via the Repo & auth group's masked tail"
    )


# ----- 2. Inline PAT persisted encrypted -----


@pytest.mark.asyncio
async def test_inline_pat_persisted_encrypted(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Inline PAT must be Fernet-encrypted before DB write."""
    from daimon.testing.factories import make_tenant

    tenant = await make_tenant(db_session, platform="discord", workspace_id="test-guild-pat")
    principal = await get_or_create_cli_principal(
        db_session, tenant_id=tenant.id, os_user="test-discord-pat"
    )
    await db_session.flush()

    fernet_key = Fernet.generate_key().decode()
    plaintext = "ghp_secret_value_xxxx7890"

    settings = MagicMock()
    settings.crypto.keys = (MagicMock(get_secret_value=lambda: fernet_key),)
    settings.github.oauth_scopes = ("repo", "read:user")
    settings.mcp.public_url = None
    runtime = DiscordRuntime(
        settings=settings,
        anthropic=build_stub_anthropic(),
        sessionmaker=db_session_factory,
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
        # fernet is real: the MCP modal encrypts an agent-scoped copy of the token.
        turn_deps=MagicMock(  # pyright: ignore[reportArgumentType]  # never runs a turn
            fernet=build_multifernet((Fernet.generate_key().decode(),))
        ),
    )

    # The credential is stored under agent_id (per-agent principal), not account_id.
    agent_uuid = uuid.uuid4()
    secret_ref = await write_mod.store_inline_pat(
        runtime,
        account_id=principal.account_id,
        agent_id=agent_uuid,
        plaintext_pat=plaintext,
    )
    assert secret_ref, "store_inline_pat must return a non-empty ma_secret_ref string"

    # Credential is stored under the per-agent principal (agent_uuid), not account_id.
    row = await creds_store.get_credential_by_principal(db_session, principal_id=agent_uuid)
    assert row is not None, (
        "inline PAT must be persisted to github_credentials under the agent_uuid"
    )
    assert bytes(row.encrypted_token) != plaintext.encode(), (
        "stored token bytes must be ciphertext, never the plaintext"
    )
    fernet = build_multifernet((fernet_key,))
    assert fernet.decrypt(bytes(row.encrypted_token)).decode() == plaintext, (
        "decryption with the configured key must recover the plaintext"
    )


# ----- 3c. Inline PAT writes per-agent overlay -----


@pytest.mark.asyncio
async def test_store_inline_pat_writes_per_agent_overlay(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """store_inline_pat writes credential under agent_id; Agent B cannot resolve it."""
    from daimon.core.github_credentials import get_pat
    from daimon.testing.factories import make_tenant

    _guild = str(uuid.uuid4())
    tenant = await make_tenant(db_session, platform="discord", workspace_id=_guild)
    principal = await get_or_create_cli_principal(
        db_session, tenant_id=tenant.id, os_user="test-per-agent-pat"
    )
    await db_session.flush()

    fernet_key = Fernet.generate_key().decode()
    fernet = build_multifernet((fernet_key,))
    plaintext = "ghp_per_agent_xxxx1234"

    settings = MagicMock()
    settings.crypto.keys = (MagicMock(get_secret_value=lambda: fernet_key),)
    settings.github.oauth_scopes = ("repo", "read:user")
    settings.mcp.public_url = None
    runtime = DiscordRuntime(
        settings=settings,
        anthropic=build_stub_anthropic(),
        sessionmaker=db_session_factory,
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
        # fernet is real: the MCP modal encrypts an agent-scoped copy of the token.
        turn_deps=MagicMock(  # pyright: ignore[reportArgumentType]  # never runs a turn
            fernet=build_multifernet((Fernet.generate_key().decode(),))
        ),
    )

    agent_a = uuid.uuid4()
    agent_b = uuid.uuid4()

    await write_mod.store_inline_pat(
        runtime,
        account_id=principal.account_id,
        agent_id=agent_a,
        plaintext_pat=plaintext,
    )

    # Agent A can resolve the PAT.
    pat_a = await get_pat(
        principal_id=principal.account_id,
        agent_id=agent_a,
        sessionmaker=db_session_factory,
        fernet=fernet,
    )
    assert pat_a == plaintext, (
        "get_pat(agent_id=agent_a) must resolve the inline PAT stored for Agent A"
    )

    # Agent B cannot resolve Agent A's PAT (cross-agent isolation).
    pat_b = await get_pat(
        principal_id=principal.account_id,
        agent_id=agent_b,
        sessionmaker=db_session_factory,
        fernet=fernet,
    )
    assert pat_b is None, (
        "get_pat(agent_id=agent_b) must return None — Agent B must not resolve Agent A's PAT"
    )


# ----- 4. AddSkill kicks off sync_agent_skills -----


@pytest.mark.asyncio
async def test_skill_modal_kicks_off_sync(
    monkeypatch: pytest.MonkeyPatch, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> None:
    """AddSkill must hand the URL to the orchestrator via asyncio.create_task."""
    captured: dict[str, Any] = {}
    sync_done = asyncio.Event()

    async def spy_kickoff(
        runtime: Any, *, tenant_id: uuid.UUID, account_id: uuid.UUID, agent_name: str, repo_url: str
    ) -> Any:
        captured["account_id"] = account_id
        captured["agent_name"] = agent_name
        captured["repo_url"] = repo_url
        sync_done.set()
        return MagicMock()

    monkeypatch.setattr(modals_mod, "kick_off_skill_sync", spy_kickoff)

    selected = _entry("research-bot")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = _runtime_for_view(anthropic=build_stub_anthropic(), tenant_id=tenant_id)

    modal = AddSkillModal(state, runtime=runtime, allowed_user_id=42)
    repo_url = "https://github.com/me/skills-repo"
    modal.url_in._value = repo_url  # pyright: ignore[reportPrivateUsage]

    await modal.on_submit(_interaction())
    # Wait for the fire-and-forget task to run.
    await asyncio.wait_for(sync_done.wait(), timeout=2.0)

    assert captured["account_id"] == account_id, "skill-sync must scope to the caller's account"
    assert captured["agent_name"] == "research-bot", (
        "skill-sync must target the selected agent by name"
    )
    assert captured["repo_url"] == repo_url, "skill-sync must receive the submitted URL"


# ----- 5. AddSkill marks pending in state -----


@pytest.mark.asyncio
async def test_skill_modal_marks_pending_in_state(
    monkeypatch: pytest.MonkeyPatch, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> None:
    """Panel re-render must show 'syncing…' for the just-added URL."""

    async def noop_kickoff(*args: Any, **kwargs: Any) -> Any:
        return MagicMock()

    monkeypatch.setattr(modals_mod, "kick_off_skill_sync", noop_kickoff)

    selected = _entry("a")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = _runtime_for_view(anthropic=build_stub_anthropic(), tenant_id=tenant_id)

    modal = AddSkillModal(state, runtime=runtime, allowed_user_id=42)
    repo_url = "https://github.com/me/skills"
    modal.url_in._value = repo_url  # pyright: ignore[reportPrivateUsage]

    await modal.on_submit(_interaction())
    assert repo_url in state.pending_skill_repo_urls, (
        "pending_skill_repo_urls must include the newly-added URL for 'syncing…' rendering"
    )


# ----- 6. Agent modal name field is read-only -----


def test_agent_modal_name_field_is_read_only(tenant_id: uuid.UUID, account_id: uuid.UUID) -> None:
    """Pitfall 4: rename forbidden day-1; use Fork+Delete to rename."""
    selected = _entry("locked-name")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = _runtime_for_view(anthropic=build_stub_anthropic(), tenant_id=tenant_id)

    modal = AgentSectionModal(state, runtime=runtime, allowed_user_id=42)
    # The modal exposes a name field for display, but it must not be editable.
    name_field = getattr(modal, "name_in", None)
    assert name_field is not None, "AgentSectionModal must show the agent name field"
    # Discord doesn't have an explicit disabled flag for TextInput; the contract
    # is that on_submit ignores any change. We document and assert the property.
    assert getattr(name_field, "_value", None) in (
        None,
        "",
    ), "name field must start empty (or default to current name); editor cannot rebind it"


def test_agent_modal_prompt_field_fits_long_system_prompt(
    tenant_id: uuid.UUID, account_id: uuid.UUID
) -> None:
    """Discord rejects a modal whose prefilled value exceeds the field's
    max_length. The seeded daimon system prompt is ~3370 chars, so opening the
    Agent modal for any such agent 400s ('This interaction failed') unless the
    prompt TextInput's max_length accommodates the default (Discord cap 4000)."""
    long_system = "x" * 3370
    selected = RosterEntry(
        name="daimon-copy",
        model="claude-sonnet-4-6",
        spec=AgentSpec(name="daimon-copy", model="claude-sonnet-4-6", system=long_system),
    )
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = _runtime_for_view(anthropic=build_stub_anthropic(), tenant_id=tenant_id)

    modal = AgentSectionModal(state, runtime=runtime, allowed_user_id=42)
    default = modal.prompt_in.default or ""
    max_len = modal.prompt_in.max_length or 4000
    assert len(default) <= max_len, (
        f"prefilled system prompt ({len(default)} chars) exceeds TextInput "
        f"max_length ({max_len}); Discord rejects the modal payload on send_modal"
    )
    assert max_len <= 4000, "Discord hard-caps text input max_length at 4000"


@pytest.mark.asyncio
async def test_agent_modal_omits_oversize_system_prompt_and_preserves_on_submit(
    monkeypatch: pytest.MonkeyPatch, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> None:
    """A system prompt over Discord's 4000 cap can't be prefilled, so the Agent
    modal omits it (blank field + placeholder) instead of failing to open —
    otherwise a model-only edit is blocked. A blank submit must KEEP the stored
    prompt, never wipe it."""

    async def fake_reconcile(runtime: Any, state: PanelState, *, tenant_id: uuid.UUID) -> Any:
        return MagicMock()

    monkeypatch.setattr(modals_mod, "call_reconcile_for_panel", fake_reconcile)

    huge = "y" * 4500  # exceeds the 4000 TextInput cap (e.g. prompt + injected preamble)
    selected = RosterEntry(
        name="dev_agent",
        model="claude-opus-4-8",
        spec=AgentSpec(name="dev_agent", model="claude-opus-4-8", system=huge),
    )
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = _runtime_for_view(anthropic=build_stub_anthropic(), tenant_id=tenant_id)

    modal = AgentSectionModal(state, runtime=runtime, allowed_user_id=42)
    # Constructs cleanly: an oversize default would make send_modal 400 on open.
    assert (modal.prompt_in.default or "") == "", "oversize prompt must NOT be prefilled"
    assert (modal.prompt_in.max_length or 0) <= 4000
    assert len(modal.prompt_in.placeholder or "") <= 100, "Discord placeholder cap is 100 chars"

    # User only changes the model and leaves the (hidden) prompt blank.
    modal.prompt_in._value = ""  # pyright: ignore[reportPrivateUsage]
    modal.model_in._value = "claude-sonnet-4-6"  # pyright: ignore[reportPrivateUsage]
    await modal.on_submit(_interaction())

    assert state.selected is not None
    assert state.selected.spec.system == huge, (
        "a blank submit on an omitted (too-long) prompt must preserve it, not wipe it"
    )


@pytest.mark.asyncio
async def test_agent_modal_submit_does_not_rename(
    monkeypatch: pytest.MonkeyPatch, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> None:
    """on_submit must NOT change state.selected.name regardless of name-field content."""

    async def fake_reconcile(runtime: Any, state: PanelState, *, tenant_id: uuid.UUID) -> Any:
        return MagicMock()

    monkeypatch.setattr(modals_mod, "call_reconcile_for_panel", fake_reconcile)

    selected = _entry("immutable-name")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = _runtime_for_view(anthropic=build_stub_anthropic(), tenant_id=tenant_id)

    modal = AgentSectionModal(state, runtime=runtime, allowed_user_id=42)
    # User attempts to rename via the name input
    modal.name_in._value = "attempted-rename"  # pyright: ignore[reportPrivateUsage]
    modal.prompt_in._value = "new system prompt"  # pyright: ignore[reportPrivateUsage]
    modal.model_in._value = "claude-sonnet-4-6"  # pyright: ignore[reportPrivateUsage]

    await modal.on_submit(_interaction())

    assert state.selected is not None and state.selected.name == "immutable-name", (
        "agent rename is forbidden — name must remain unchanged after submit"
    )
    assert state.selected.spec.system == "new system prompt", "system prompt edit must apply"


# ----- 7. AddMcp requires all three fields -----


@pytest.mark.asyncio
async def test_add_mcp_modal_requires_all_three_fields(
    monkeypatch: pytest.MonkeyPatch, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> None:
    """MA SDK couples auth to URL — all three required."""

    async def fake_reconcile(runtime: Any, state: PanelState, *, tenant_id: uuid.UUID) -> Any:
        return MagicMock()

    monkeypatch.setattr(modals_mcp_mod, "call_reconcile_for_panel", fake_reconcile)

    selected = _entry("a")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = _runtime_for_view(anthropic=build_stub_anthropic(), tenant_id=tenant_id)

    modal = AddMcpModal(state, runtime=runtime, allowed_user_id=42)
    modal.name_in._value = "my-mcp"  # pyright: ignore[reportPrivateUsage]
    modal.url_in._value = "https://mcp.example.com"  # pyright: ignore[reportPrivateUsage]
    modal.token_in._value = ""  # pyright: ignore[reportPrivateUsage]  # missing

    interaction = _interaction()
    await modal.on_submit(interaction)
    # Modal must send an error message; spec mcp_servers must be unchanged.
    assert state.selected is not None
    assert not state.selected.spec.mcp_servers, (
        "missing auth token must short-circuit; no MCP entry appended"
    )
    interaction.response.send_message.assert_called_once()
    call_text = str(interaction.response.send_message.call_args)
    assert "required" in call_text.lower() or "three" in call_text.lower(), (
        "user must see a 'fields required' error"
    )


# ----- 8. AddMcp appends to spec and reconciles -----


@pytest.mark.asyncio
async def test_add_mcp_modal_appends_to_spec_and_reconciles(
    monkeypatch: pytest.MonkeyPatch, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> None:
    """MCP add appends to spec then reconciles per Pattern 1."""
    called: dict[str, Any] = {}

    async def spy_reconcile(runtime: Any, state: PanelState, *, tenant_id: uuid.UUID) -> Any:
        called["state"] = state
        return MagicMock()

    monkeypatch.setattr(modals_mcp_mod, "call_reconcile_for_panel", spy_reconcile)

    selected = _entry("a", mcp_servers=[])
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = _runtime_for_view(anthropic=build_stub_anthropic(), tenant_id=tenant_id)

    modal = AddMcpModal(state, runtime=runtime, allowed_user_id=42)
    modal.name_in._value = "ga4-mcp"  # pyright: ignore[reportPrivateUsage]
    modal.url_in._value = "https://ga4.example.com/mcp"  # pyright: ignore[reportPrivateUsage]
    modal.token_in._value = "secret_token_abcd"  # pyright: ignore[reportPrivateUsage]

    await modal.on_submit(_interaction())

    # Real SDK constructor used in the modal — the appended entry should match.
    expected_entry = BetaManagedAgentsURLMCPServerParams(
        name="ga4-mcp", type="url", url="https://ga4.example.com/mcp"
    )
    assert state.selected is not None
    mcps = state.selected.spec.mcp_servers or []
    assert len(mcps) == 1, "exactly one MCP entry must be appended"
    assert mcps[0] == expected_entry, (
        "appended MCP entry must match the SDK TypedDict shape from inline construction"
    )
    assert "state" in called, "AddMcp must trigger reconcile after appending"


# ----- 9. AddMcp stores auth-token-last4 masked -----


@pytest.mark.asyncio
async def test_add_mcp_modal_stores_auth_token_masked_in_state(
    monkeypatch: pytest.MonkeyPatch, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> None:
    """Auth token masked in panel; never logged plaintext."""

    async def fake_reconcile(runtime: Any, state: PanelState, *, tenant_id: uuid.UUID) -> Any:
        return MagicMock()

    monkeypatch.setattr(modals_mcp_mod, "call_reconcile_for_panel", fake_reconcile)

    selected = _entry("a", mcp_servers=[])
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = _runtime_for_view(anthropic=build_stub_anthropic(), tenant_id=tenant_id)

    modal = AddMcpModal(state, runtime=runtime, allowed_user_id=42)
    plaintext = "secret_xyz_abcd"
    modal.name_in._value = "m"  # pyright: ignore[reportPrivateUsage]
    modal.url_in._value = "https://m.example.com"  # pyright: ignore[reportPrivateUsage]
    modal.token_in._value = plaintext  # pyright: ignore[reportPrivateUsage]

    await modal.on_submit(_interaction())
    assert state.mcp_token_last4 == "abcd", "state must carry only last-4 of MCP auth token"


# ----- 9b. AddMcp writes the auth token to the per-agent vault after reconcile -----

_VAULT_MA_AGENT_ID = "agent_vaulttest_abcdefgh1234"


def _fake_ma_agent_for_vault(tenant_id: uuid.UUID) -> BetaManagedAgentsAgent:
    """Real SDK BetaManagedAgentsAgent used by vault tests to stub find_agent_by_daimon_tag."""
    now = datetime.now(UTC)
    return BetaManagedAgentsAgent(
        id=_VAULT_MA_AGENT_ID,
        type="agent",
        name="a",
        version=1,
        model=BetaManagedAgentsModelConfig(id="claude-sonnet-4-6", speed="standard"),
        created_at=now,
        updated_at=now,
        metadata={"daimon_tenant": str(tenant_id), "daimon_name": "a"},
        mcp_servers=[],
        tools=[],
        skills=[],
    )


def _vault_handler(
    *,
    account_id: uuid.UUID,
    agent_uuid: uuid.UUID,
    vault_id: str = "vlt_agent",
    prior_creds: list[dict[str, Any]] | None = None,
    call_log: list[tuple[str, str]] | None = None,
    created_bodies: list[dict[str, Any]] | None = None,
    deleted_ids: list[str] | None = None,
) -> Any:
    """Inline httpx.MockTransport handler covering the vault endpoints the
    AddMcpModal vault-write path hits: list vaults, list creds, delete cred,
    create cred. All response shapes inlined; no shared factory."""

    import httpx

    display = f"daimon-mcp:{account_id}:{agent_uuid}"
    creds_state: list[dict[str, Any]] = list(prior_creds or [])

    def handler(req: httpx.Request) -> httpx.Response:
        if call_log is not None:
            call_log.append((req.method, req.url.path))
        if req.method == "GET" and req.url.path == "/v1/vaults":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": vault_id,
                            "type": "vault",
                            "display_name": display,
                            "metadata": None,
                            "archived_at": None,
                            "created_at": "2026-04-01T00:00:00Z",
                        }
                    ],
                    "has_more": False,
                },
            )
        if req.method == "GET" and req.url.path == f"/v1/vaults/{vault_id}/credentials":
            return httpx.Response(
                200,
                json={"data": list(creds_state), "has_more": False},
            )
        if req.method == "DELETE" and req.url.path.startswith(
            f"/v1/vaults/{vault_id}/credentials/"
        ):
            import json as _json

            cred_id = req.url.path.rsplit("/", 1)[-1]
            if deleted_ids is not None:
                deleted_ids.append(cred_id)
            creds_state[:] = [c for c in creds_state if c["id"] != cred_id]
            return httpx.Response(200, content=_json.dumps({"id": cred_id, "deleted": True}))
        if req.method == "POST" and req.url.path == f"/v1/vaults/{vault_id}/credentials":
            import json as _json

            body = _json.loads(req.content)
            if created_bodies is not None:
                created_bodies.append(body)
            new_id = f"vcrd_{len(creds_state) + 1}"
            new_cred = {
                "id": new_id,
                "type": "credential",
                "vault_id": vault_id,
                "auth": {
                    "type": "static_bearer",
                    "mcp_server_url": body["auth"]["mcp_server_url"],
                },
            }
            creds_state.append(new_cred)
            return httpx.Response(200, json=new_cred)
        raise AssertionError(f"unexpected call: {req.method} {req.url}")

    return handler


@pytest.mark.asyncio
async def test_add_mcp_modal_writes_vault_credential_after_reconcile(
    monkeypatch: pytest.MonkeyPatch,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Happy path: after reconcile succeeds, the modal POSTs a static_bearer
    credential to the per-agent vault carrying the submitted URL and token.
    Reconcile signal observed BEFORE the credential POST."""
    from daimon.core.defaults.report import Action, ResourceOutcome

    # The modal now persists an agent-scoped copy of the token, which FKs to
    # tenants — seed the row the tenant_id fixture names.
    async with db_session_factory() as _session, _session.begin():
        await make_tenant(_session, id=tenant_id, workspace_id="guild-mcp-modal-write")
    agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=_VAULT_MA_AGENT_ID)

    reconcile_order: list[str] = []

    async def spy_reconcile(runtime: Any, state: PanelState, *, tenant_id: uuid.UUID) -> Any:
        reconcile_order.append("reconcile")
        return ResourceOutcome(
            kind="agent", name="a", action=Action.UPDATED, anthropic_id="agent_x"
        )

    monkeypatch.setattr(modals_mcp_mod, "call_reconcile_for_panel", spy_reconcile)

    async def fake_find(client: Any, *, tenant_id: uuid.UUID, name: str) -> Any:
        return _fake_ma_agent_for_vault(tenant_id)

    monkeypatch.setattr(modals_mcp_mod, "find_agent_by_daimon_tag", fake_find)

    call_log: list[tuple[str, str]] = []
    created_bodies: list[dict[str, Any]] = []
    handler = _vault_handler(
        account_id=account_id,
        agent_uuid=agent_uuid,
        call_log=call_log,
        created_bodies=created_bodies,
    )

    selected = _entry("a", mcp_servers=[])
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = _runtime_for_view(
        anthropic=build_stub_anthropic(handler),
        tenant_id=tenant_id,
        sessionmaker=db_session_factory,
    )

    modal = AddMcpModal(state, runtime=runtime, allowed_user_id=42)
    submitted_url = "https://ga4.example.com/mcp"
    submitted_token = "secret_token_abcd"
    modal.name_in._value = "ga4-mcp"  # pyright: ignore[reportPrivateUsage]
    modal.url_in._value = submitted_url  # pyright: ignore[reportPrivateUsage]
    modal.token_in._value = submitted_token  # pyright: ignore[reportPrivateUsage]

    await modal.on_submit(_interaction())

    assert reconcile_order == ["reconcile"], "reconcile must be called once"
    assert len(created_bodies) == 1, "exactly one credential POST expected"
    body = created_bodies[0]
    assert body["auth"]["type"] == "static_bearer"
    assert body["auth"]["mcp_server_url"] == submitted_url
    assert body["auth"]["token"] == submitted_token, (
        "the user's submitted token must reach the vault"
    )
    # The credential POST must come after the vault list/cred list — verifies ordering.
    post_idx = next(
        i for i, c in enumerate(call_log) if c == ("POST", "/v1/vaults/vlt_agent/credentials")
    )
    list_idx = next(i for i, c in enumerate(call_log) if c == ("GET", "/v1/vaults"))
    assert list_idx < post_idx, "must list vaults before posting credential"


@pytest.mark.asyncio
async def test_add_mcp_modal_does_not_write_vault_when_reconcile_fails(
    monkeypatch: pytest.MonkeyPatch, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> None:
    """If call_reconcile_for_panel raises, no vault credential POST may occur."""

    async def failing_reconcile(runtime: Any, state: PanelState, *, tenant_id: uuid.UUID) -> Any:
        raise RuntimeError("reconcile blew up")

    monkeypatch.setattr(modals_mcp_mod, "call_reconcile_for_panel", failing_reconcile)

    call_log: list[tuple[str, str]] = []
    created_bodies: list[dict[str, Any]] = []
    agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=_VAULT_MA_AGENT_ID)
    handler = _vault_handler(
        account_id=account_id,
        agent_uuid=agent_uuid,
        call_log=call_log,
        created_bodies=created_bodies,
    )

    selected = _entry("a", mcp_servers=[])
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = _runtime_for_view(anthropic=build_stub_anthropic(handler), tenant_id=tenant_id)

    modal = AddMcpModal(state, runtime=runtime, allowed_user_id=42)
    modal.name_in._value = "ga4-mcp"  # pyright: ignore[reportPrivateUsage]
    modal.url_in._value = "https://ga4.example.com/mcp"  # pyright: ignore[reportPrivateUsage]
    modal.token_in._value = "secret_token_abcd"  # pyright: ignore[reportPrivateUsage]

    interaction = _interaction()
    await modal.on_submit(interaction)

    cred_calls = [c for c in call_log if "credentials" in c[1] and c[0] == "POST"]
    assert cred_calls == [], (
        f"reconcile failure must short-circuit before the vault write; got {cred_calls}"
    )
    assert created_bodies == []
    interaction.followup.send.assert_called_once()


@pytest.mark.asyncio
async def test_add_mcp_modal_resubmit_replaces_prior_vault_credential(
    monkeypatch: pytest.MonkeyPatch,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Idempotent resubmit: second submission with the same URL DELETEs the
    prior static_bearer credential and POSTs a fresh one. Two POSTs total
    across both submissions; one DELETE on the second."""
    from daimon.core.defaults.report import Action, ResourceOutcome

    # The modal now persists an agent-scoped copy of the token, which FKs to
    # tenants — seed the row the tenant_id fixture names.
    async with db_session_factory() as _session, _session.begin():
        await make_tenant(_session, id=tenant_id, workspace_id="guild-mcp-modal-resubmit")
    agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=_VAULT_MA_AGENT_ID)

    async def spy_reconcile(runtime: Any, state: PanelState, *, tenant_id: uuid.UUID) -> Any:
        return ResourceOutcome(
            kind="agent", name="a", action=Action.UPDATED, anthropic_id="agent_x"
        )

    monkeypatch.setattr(modals_mcp_mod, "call_reconcile_for_panel", spy_reconcile)

    async def fake_find(client: Any, *, tenant_id: uuid.UUID, name: str) -> Any:
        return _fake_ma_agent_for_vault(tenant_id)

    monkeypatch.setattr(modals_mcp_mod, "find_agent_by_daimon_tag", fake_find)

    created_bodies: list[dict[str, Any]] = []
    deleted_ids: list[str] = []
    handler = _vault_handler(
        account_id=account_id,
        agent_uuid=agent_uuid,
        created_bodies=created_bodies,
        deleted_ids=deleted_ids,
    )

    selected = _entry("a", mcp_servers=[])
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = _runtime_for_view(
        anthropic=build_stub_anthropic(handler),
        tenant_id=tenant_id,
        sessionmaker=db_session_factory,
    )

    url = "https://ga4.example.com/mcp"

    modal1 = AddMcpModal(state, runtime=runtime, allowed_user_id=42)
    modal1.name_in._value = "ga4-mcp"  # pyright: ignore[reportPrivateUsage]
    modal1.url_in._value = url  # pyright: ignore[reportPrivateUsage]
    modal1.token_in._value = "first_token_aaaa"  # pyright: ignore[reportPrivateUsage]
    await modal1.on_submit(_interaction())

    modal2 = AddMcpModal(state, runtime=runtime, allowed_user_id=42)
    modal2.name_in._value = "ga4-mcp"  # pyright: ignore[reportPrivateUsage]
    modal2.url_in._value = url  # pyright: ignore[reportPrivateUsage]
    modal2.token_in._value = "second_token_bbbb"  # pyright: ignore[reportPrivateUsage]
    await modal2.on_submit(_interaction())

    assert len(created_bodies) == 2, "two credential POSTs total across both submissions"
    assert len(deleted_ids) == 1, (
        f"second submission must DELETE the prior credential at the same URL; got {deleted_ids}"
    )
    assert created_bodies[1]["auth"]["token"] == "second_token_bbbb"


# ----- 10. AddSkill toasts outcome after sync completes -----


@pytest.mark.asyncio
async def test_skill_modal_toasts_success_on_all_synced(
    monkeypatch: pytest.MonkeyPatch, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> None:
    """All-success sync must produce an ephemeral success toast."""

    async def fake_kickoff(
        runtime: Any, *, tenant_id: uuid.UUID, account_id: uuid.UUID, agent_name: str, repo_url: str
    ) -> SyncReport:
        return SyncReport(synced=2)

    monkeypatch.setattr(modals_mod, "kick_off_skill_sync", fake_kickoff)

    selected = _entry("research-bot")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = _runtime_for_view(anthropic=build_stub_anthropic(), tenant_id=tenant_id)

    modal = AddSkillModal(state, runtime=runtime, allowed_user_id=42)
    repo_url = "https://github.com/me/skills-repo"
    modal.url_in._value = repo_url  # pyright: ignore[reportPrivateUsage]

    interaction = _interaction()
    await modal.on_submit(interaction)
    # Allow any background task to complete.
    await asyncio.sleep(0)

    # followup.send must be called exactly once after all-success sync
    interaction.followup.send.assert_called_once()
    call_kwargs = interaction.followup.send.call_args
    assert call_kwargs.kwargs.get("ephemeral") is True, "outcome toast must be ephemeral"
    content = call_kwargs.kwargs.get("content") or str(call_kwargs)
    assert "2" in content, "toast content must mention synced skill count"
    assert any(marker in content for marker in ("Synced", "synced", "✓", "✔")), (
        "toast content must contain a success marker (Synced / checkmark)"
    )


@pytest.mark.asyncio
async def test_skill_modal_toasts_partial_on_mixed_result(
    monkeypatch: pytest.MonkeyPatch, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> None:
    """Partial sync (some ok, some failed) must produce a warning toast."""

    async def fake_kickoff(
        runtime: Any, *, tenant_id: uuid.UUID, account_id: uuid.UUID, agent_name: str, repo_url: str
    ) -> SyncReport:
        return SyncReport(synced=1, failed_uploads=[("skill-x", "bad SKILL.md")])

    monkeypatch.setattr(modals_mod, "kick_off_skill_sync", fake_kickoff)

    selected = _entry("research-bot")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = _runtime_for_view(anthropic=build_stub_anthropic(), tenant_id=tenant_id)

    modal = AddSkillModal(state, runtime=runtime, allowed_user_id=42)
    modal.url_in._value = "https://github.com/me/skills-repo"  # pyright: ignore[reportPrivateUsage]

    interaction = _interaction()
    await modal.on_submit(interaction)
    await asyncio.sleep(0)

    # followup.send must be called after partial sync
    interaction.followup.send.assert_called_once()
    call_kwargs = interaction.followup.send.call_args
    assert call_kwargs.kwargs.get("ephemeral") is True, "outcome toast must be ephemeral"
    content = call_kwargs.kwargs.get("content") or str(call_kwargs)
    assert "bad SKILL.md" in content, "partial toast must mention the failure reason"
    # Must NOT be a pure success marker — should indicate warning/partial.
    assert any(marker in content for marker in ("⚠", "failed", "Failed", "partial", "Partial")), (
        "partial toast must signal a warning or partial outcome"
    )


@pytest.mark.asyncio
async def test_skill_modal_toasts_failure_on_all_failed(
    monkeypatch: pytest.MonkeyPatch, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> None:
    """All-failed sync (skipped repo) must produce a failure toast."""

    async def fake_kickoff(
        runtime: Any, *, tenant_id: uuid.UUID, account_id: uuid.UUID, agent_name: str, repo_url: str
    ) -> SyncReport:
        return SyncReport(skipped_repos=[("https://github.com/o/r", "fetch failed")])

    monkeypatch.setattr(modals_mod, "kick_off_skill_sync", fake_kickoff)

    selected = _entry("research-bot")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = _runtime_for_view(anthropic=build_stub_anthropic(), tenant_id=tenant_id)

    modal = AddSkillModal(state, runtime=runtime, allowed_user_id=42)
    modal.url_in._value = "https://github.com/me/skills-repo"  # pyright: ignore[reportPrivateUsage]

    interaction = _interaction()
    await modal.on_submit(interaction)
    await asyncio.sleep(0)

    # followup.send must be called after all-failed sync
    interaction.followup.send.assert_called_once()
    call_kwargs = interaction.followup.send.call_args
    assert call_kwargs.kwargs.get("ephemeral") is True, "outcome toast must be ephemeral"
    content = call_kwargs.kwargs.get("content") or str(call_kwargs)
    assert "fetch failed" in content, "failure toast must mention the failure reason"
    assert any(marker in content for marker in ("✗", "failed", "Failed", "Sync failed")), (
        "failure toast must signal failure"
    )


@pytest.mark.asyncio
async def test_skill_modal_toasts_failure_on_exception(
    monkeypatch: pytest.MonkeyPatch, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> None:
    """Exception during sync must still produce an
    ephemeral failure toast (boundary catch preserved)."""

    async def fake_kickoff(
        runtime: Any, *, tenant_id: uuid.UUID, account_id: uuid.UUID, agent_name: str, repo_url: str
    ) -> SyncReport:
        raise RuntimeError("network error during sync")

    monkeypatch.setattr(modals_mod, "kick_off_skill_sync", fake_kickoff)

    selected = _entry("research-bot")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = _runtime_for_view(anthropic=build_stub_anthropic(), tenant_id=tenant_id)

    modal = AddSkillModal(state, runtime=runtime, allowed_user_id=42)
    modal.url_in._value = "https://github.com/me/skills-repo"  # pyright: ignore[reportPrivateUsage]

    interaction = _interaction()
    await modal.on_submit(interaction)
    await asyncio.sleep(0)

    # boundary catch must send an ephemeral failure toast on exception
    interaction.followup.send.assert_called_once()
    call_kwargs = interaction.followup.send.call_args
    assert call_kwargs.kwargs.get("ephemeral") is True, "exception toast must be ephemeral"
    content = call_kwargs.kwargs.get("content") or str(call_kwargs)
    assert "network error during sync" in content or "RuntimeError" in content, (
        "exception toast must include the error type or message"
    )


# ----- 11. Anon-bind public-visibility guard (quick task 260616-45k) -----


def _fake_ma_agent_for_bind(tenant_id: uuid.UUID) -> BetaManagedAgentsAgent:
    now = datetime.now(UTC)
    return BetaManagedAgentsAgent(
        id="agent_bindtest_abcdefgh1234",
        type="agent",
        name="a",
        version=1,
        model=BetaManagedAgentsModelConfig(id="claude-sonnet-4-6", speed="standard"),
        created_at=now,
        updated_at=now,
        metadata={"daimon_tenant": str(tenant_id), "daimon_name": "a"},
        mcp_servers=[],
        tools=[],
        skills=[],
    )


@pytest.mark.asyncio
async def test_anon_bind_writes_binding_and_proof_when_repo_public(
    monkeypatch: pytest.MonkeyPatch,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """No-PAT bind of a verified-public repo writes an anon: binding whose
    recorded proof carries kind='public', a timestamp, and the submitting
    account."""
    tenant = await make_tenant(
        db_session, platform="discord", workspace_id="test-anon-public", id=tenant_id
    )
    await make_account(db_session, tenant=tenant, id=account_id)
    await db_session.flush()

    captured: dict[str, Any] = {}

    async def fake_is_public(http_client: Any, *, owner_repo: str) -> bool:
        captured["owner_repo"] = owner_repo
        return True

    async def fake_reconcile(runtime: Any, state: PanelState, *, tenant_id: uuid.UUID) -> Any:
        return MagicMock()

    async def fake_find(*args: Any, **kwargs: Any) -> Any:
        return _fake_ma_agent_for_bind(tenant_id)

    monkeypatch.setattr(modals_mod, "is_public_repo", fake_is_public)
    monkeypatch.setattr(modals_mod, "call_reconcile_for_panel", fake_reconcile)
    monkeypatch.setattr(modals_mod, "find_agent_by_daimon_tag", fake_find)

    selected = _entry("a")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = _runtime_for_view(
        anthropic=build_stub_anthropic(), tenant_id=tenant_id, sessionmaker=db_session_factory
    )

    modal = RepoAuthModal(state, runtime=runtime, allowed_user_id=42)
    modal.url_in._value = "https://github.com/me/public-repo"  # pyright: ignore[reportPrivateUsage]
    modal.branch_in._value = "main"  # pyright: ignore[reportPrivateUsage]
    modal.pat_in._value = ""  # pyright: ignore[reportPrivateUsage]

    await modal.on_submit(_interaction())

    assert captured.get("owner_repo") == "me/public-repo", (
        "visibility check must receive normalized owner/repo"
    )

    agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id="agent_bindtest_abcdefgh1234")
    row = await get_binding(db_session, tenant_id=tenant_id, agent_id=agent_uuid)
    assert row is not None, "the bind must write an agent_repo_binding row"
    assert row.ma_secret_ref == "anon:", "public no-PAT bind writes an anon: binding"
    # proof_kind / proof_at / proof_account_id must all be recorded on the write.
    assert row.proof_kind == "public", "a probed-public bind must record proof_kind='public'"
    assert row.proof_at is not None, "proof must record a timestamp"
    assert row.proof_account_id == account_id, "proof must record the submitting account"


@pytest.mark.asyncio
async def test_anon_bind_rejected_when_repo_private(
    monkeypatch: pytest.MonkeyPatch, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> None:
    """No-PAT bind of a private/404 repo raises and writes no binding."""
    set_binding_called = False

    async def fake_is_public(http_client: Any, *, owner_repo: str) -> bool:
        return False

    async def fake_set_binding(*args: Any, **kwargs: Any) -> Any:
        nonlocal set_binding_called
        set_binding_called = True
        return MagicMock()

    async def fake_find(*args: Any, **kwargs: Any) -> Any:
        return _fake_ma_agent_for_bind(tenant_id)

    monkeypatch.setattr(modals_mod, "is_public_repo", fake_is_public)
    monkeypatch.setattr(modals_mod, "set_agent_repo_binding", fake_set_binding)
    monkeypatch.setattr(modals_mod, "find_agent_by_daimon_tag", fake_find)

    selected = _entry("a")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = _runtime_for_view(anthropic=build_stub_anthropic(), tenant_id=tenant_id)

    modal = RepoAuthModal(state, runtime=runtime, allowed_user_id=42)
    modal.url_in._value = "https://github.com/me/private-repo"  # pyright: ignore[reportPrivateUsage]
    modal.branch_in._value = "main"  # pyright: ignore[reportPrivateUsage]
    modal.pat_in._value = ""  # pyright: ignore[reportPrivateUsage]

    interaction = _interaction()
    await modal.on_submit(interaction)

    assert not set_binding_called, "private/404 repo must not write a binding"
    interaction.followup.send.assert_called_once()
    call_text = str(interaction.followup.send.call_args)
    assert "publicly readable" in call_text and "token" in call_text, (
        "user must see a refusal naming the fix (paste a token), not GitHub's bare framing"
    )


@pytest.mark.asyncio
async def test_anon_bind_rejected_when_repo_lookup_returns_404(
    monkeypatch: pytest.MonkeyPatch, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> None:
    """A 404 repo lookup (GitHub's response for a repo the caller cannot see
    unauthenticated) must produce the same refusal as an explicitly-private
    repo — not a "repo doesn't exist" message. Drives the real `is_public_repo`
    mapping via a mocked transport, rather than stubbing the function itself."""
    set_binding_called = False

    async def fake_set_binding(*args: Any, **kwargs: Any) -> Any:
        nonlocal set_binding_called
        set_binding_called = True
        return MagicMock()

    async def fake_find(*args: Any, **kwargs: Any) -> Any:
        return _fake_ma_agent_for_bind(tenant_id)

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/repos/me/missing-repo", (
            "the real is_public_repo must hit the GitHub repo-lookup endpoint"
        )
        return httpx.Response(404)

    class _HttpxProxyWithMockedAsyncClient:
        """Delegates to the real `httpx` module except for `AsyncClient`, which
        gets the mocked transport. Patching `modals_mod.httpx` (the module's own
        name binding) rather than the shared global `httpx.AsyncClient` attribute
        keeps this test from breaking unrelated code (e.g. the anthropic SDK)
        that also constructs `httpx.AsyncClient` instances during this test."""

        def AsyncClient(self, *args: Any, **kwargs: Any) -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=httpx.MockTransport(handler))

        def __getattr__(self, name: str) -> Any:
            return getattr(httpx, name)

    monkeypatch.setattr(modals_mod, "httpx", _HttpxProxyWithMockedAsyncClient())
    monkeypatch.setattr(modals_mod, "set_agent_repo_binding", fake_set_binding)
    monkeypatch.setattr(modals_mod, "find_agent_by_daimon_tag", fake_find)

    selected = _entry("a")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    # is_public_repo is the only request the mocked transport needs to answer.
    runtime = _runtime_for_view(anthropic=build_stub_anthropic(), tenant_id=tenant_id)

    modal = RepoAuthModal(state, runtime=runtime, allowed_user_id=42)
    modal.url_in._value = "https://github.com/me/missing-repo"  # pyright: ignore[reportPrivateUsage]
    modal.branch_in._value = "main"  # pyright: ignore[reportPrivateUsage]
    modal.pat_in._value = ""  # pyright: ignore[reportPrivateUsage]

    interaction = _interaction()
    await modal.on_submit(interaction)

    assert not set_binding_called, "a 404 repo lookup must not write a binding"
    interaction.followup.send.assert_called_once()
    call_text = str(interaction.followup.send.call_args)
    assert "publicly readable" in call_text and "token" in call_text, (
        "a 404 repo lookup must produce the same refusal as a private repo"
    )


@pytest.mark.asyncio
async def test_pat_bind_skips_visibility_check_and_records_proof(
    monkeypatch: pytest.MonkeyPatch,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A bind WITH a PAT takes the store_inline_pat path; no visibility call,
    and the recorded proof carries kind='pat'."""
    tenant = await make_tenant(
        db_session, platform="discord", workspace_id="test-pat-bind", id=tenant_id
    )
    await make_account(db_session, tenant=tenant, id=account_id)
    await db_session.flush()

    visibility_called = False

    async def fake_is_public(http_client: Any, *, owner_repo: str) -> bool:
        nonlocal visibility_called
        visibility_called = True
        return True

    async def fake_store_inline_pat(
        runtime: Any, *, account_id: uuid.UUID, agent_id: uuid.UUID, plaintext_pat: str
    ) -> str:
        return f"inline-pat:{agent_id}"

    async def fake_reconcile(runtime: Any, state: PanelState, *, tenant_id: uuid.UUID) -> Any:
        return MagicMock()

    async def fake_find(*args: Any, **kwargs: Any) -> Any:
        return _fake_ma_agent_for_bind(tenant_id)

    pat_access_called = False

    async def fake_pat_access(http_client: Any, *, owner_repo: str, pat: str) -> bool:
        nonlocal pat_access_called
        pat_access_called = True
        return True

    monkeypatch.setattr(modals_mod, "is_public_repo", fake_is_public)
    monkeypatch.setattr(modals_mod, "pat_can_access_repo", fake_pat_access)
    monkeypatch.setattr(modals_mod, "store_inline_pat", fake_store_inline_pat)
    monkeypatch.setattr(modals_mod, "call_reconcile_for_panel", fake_reconcile)
    monkeypatch.setattr(modals_mod, "find_agent_by_daimon_tag", fake_find)

    selected = _entry("a")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = _runtime_for_view(
        anthropic=build_stub_anthropic(), tenant_id=tenant_id, sessionmaker=db_session_factory
    )

    modal = RepoAuthModal(state, runtime=runtime, allowed_user_id=42)
    modal.url_in._value = "https://github.com/me/repo"  # pyright: ignore[reportPrivateUsage]
    modal.branch_in._value = "main"  # pyright: ignore[reportPrivateUsage]
    modal.pat_in._value = "ghp_some_pat_1234"  # pyright: ignore[reportPrivateUsage]

    await modal.on_submit(_interaction())

    assert not visibility_called, "PAT path must never call the public-visibility check"
    assert pat_access_called, (
        "PAT path must verify the token grants repo access before binding "
        "(cross-tenant App-token clone guard)"
    )

    agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id="agent_bindtest_abcdefgh1234")
    row = await get_binding(db_session, tenant_id=tenant_id, agent_id=agent_uuid)
    assert row is not None, "the bind must write an agent_repo_binding row"
    # proof_kind / proof_at / proof_account_id must all be recorded on the write.
    assert row.proof_kind == "pat", "a pasted-PAT bind must record proof_kind='pat'"
    assert row.proof_at is not None, "proof must record a timestamp"
    assert row.proof_account_id == account_id, "proof must record the submitting account"


# ----- 12. Blank-PAT bind ignores GitHub App coverage entirely -----


@pytest.mark.asyncio
async def test_blank_pat_bind_refused_when_repo_private_even_though_app_is_installed(
    monkeypatch: pytest.MonkeyPatch, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> None:
    """A blank-PAT bind against a private repo must be refused even when the
    deployment's GitHub App is installed on the repo owner -- App coverage is
    global to the deployment and proves nothing about whether this binder may
    read the repo. The bind path must not even look up installation state."""
    requested_paths: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        requested_paths.append(req.url.path)
        assert req.url.path == "/repos/orgA/private-repo", (
            "the real is_public_repo must hit the GitHub repo-lookup endpoint"
        )
        return httpx.Response(200, json={"private": True})

    class _HttpxProxyWithMockedAsyncClient:
        """Delegates to the real `httpx` module except for `AsyncClient`, which
        gets the mocked transport."""

        def AsyncClient(self, *args: Any, **kwargs: Any) -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=httpx.MockTransport(handler))

        def __getattr__(self, name: str) -> Any:
            return getattr(httpx, name)

    set_binding_called = False

    async def fake_set_binding(*args: Any, **kwargs: Any) -> Any:
        nonlocal set_binding_called
        set_binding_called = True
        return MagicMock()

    async def fake_find(*args: Any, **kwargs: Any) -> Any:
        return _fake_ma_agent_for_bind(tenant_id)

    monkeypatch.setattr(modals_mod, "httpx", _HttpxProxyWithMockedAsyncClient())
    monkeypatch.setattr(modals_mod, "set_agent_repo_binding", fake_set_binding)
    monkeypatch.setattr(modals_mod, "find_agent_by_daimon_tag", fake_find)

    selected = _entry("a")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = _runtime_for_view(anthropic=build_stub_anthropic(), tenant_id=tenant_id)
    runtime.settings.github.app_id = "app_123"
    runtime.settings.github.app_private_key = SecretStr("fake-private-key")

    modal = RepoAuthModal(state, runtime=runtime, allowed_user_id=42)
    modal.url_in._value = "https://github.com/orgA/private-repo"  # pyright: ignore[reportPrivateUsage]
    modal.branch_in._value = "main"  # pyright: ignore[reportPrivateUsage]
    modal.pat_in._value = ""  # pyright: ignore[reportPrivateUsage]

    interaction = _interaction()
    await modal.on_submit(interaction)

    assert not set_binding_called, (
        "a private repo must be refused even when the deployment's App is installed on it"
    )
    interaction.followup.send.assert_called_once()
    call_text = str(interaction.followup.send.call_args).lower()
    assert "token" in call_text, "the refusal must name the fix: pasting a GitHub token"
    assert not any("/installation" in path for path in requested_paths), (
        "the bind path must never issue an App-installation lookup"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("app_id", "app_private_key"),
    [
        (None, None),
        ("app_123", "fake-private-key"),
    ],
    ids=["app_not_configured", "app_configured"],
)
async def test_blank_pat_bind_probes_public_visibility_regardless_of_app_config(
    monkeypatch: pytest.MonkeyPatch,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    app_id: str | None,
    app_private_key: str | None,
) -> None:
    """A blank-PAT bind always probes public visibility and gates on the
    result, whether or not the deployment has a GitHub App configured --
    App configuration is irrelevant to the bind path now."""
    public_check_called = False

    async def fake_is_public(http_client: Any, *, owner_repo: str) -> bool:
        nonlocal public_check_called
        public_check_called = True
        return True

    captured: dict[str, Any] = {}

    async def fake_set_binding(*args: Any, **kwargs: Any) -> Any:
        captured["ma_secret_ref"] = kwargs["ma_secret_ref"]
        return MagicMock()

    async def fake_reconcile(runtime: Any, state: PanelState, *, tenant_id: uuid.UUID) -> Any:
        return MagicMock()

    async def fake_find(*args: Any, **kwargs: Any) -> Any:
        return _fake_ma_agent_for_bind(tenant_id)

    monkeypatch.setattr(modals_mod, "is_public_repo", fake_is_public)
    monkeypatch.setattr(modals_mod, "set_agent_repo_binding", fake_set_binding)
    monkeypatch.setattr(modals_mod, "call_reconcile_for_panel", fake_reconcile)
    monkeypatch.setattr(modals_mod, "find_agent_by_daimon_tag", fake_find)

    selected = _entry("a")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    sm = MagicMock()
    sm.begin.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
    sm.begin.return_value.__aexit__ = AsyncMock(return_value=False)
    runtime = _runtime_for_view(
        anthropic=build_stub_anthropic(), tenant_id=tenant_id, sessionmaker=sm
    )
    runtime.settings.github.app_id = app_id
    runtime.settings.github.app_private_key = (
        SecretStr(app_private_key) if app_private_key is not None else None
    )

    modal = RepoAuthModal(state, runtime=runtime, allowed_user_id=42)
    modal.url_in._value = "https://github.com/me/public-repo"  # pyright: ignore[reportPrivateUsage]
    modal.branch_in._value = "main"  # pyright: ignore[reportPrivateUsage]
    modal.pat_in._value = ""  # pyright: ignore[reportPrivateUsage]

    interaction = _interaction()
    await modal.on_submit(interaction)

    assert public_check_called, "the public-visibility probe must fire regardless of App config"
    assert captured.get("ma_secret_ref") == "anon:", "public no-PAT bind writes an anon: binding"


# ---------------------------------------------------------------------------
# A blank PAT field must not mean "no inline PAT exists"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_blank_pat_bind_refused_when_stored_inline_pat_cannot_access_repo(
    monkeypatch: pytest.MonkeyPatch, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> None:
    """A repo bound with a blank PAT field must re-verify any already-stored
    inline PAT against the newly typed repo, and refuse the bind when that PAT
    can't access it -- never falling through to the public probe. This is
    regression guard: removing the mitigation block must make this
    test fail (verified by temporary local revert per the plan's acceptance
    criteria)."""
    public_called = False
    received: dict[str, Any] = {}

    async def fake_load_inline_pat(runtime: Any, *, agent_id: uuid.UUID) -> str | None:
        return "ghp_stale_stored_pat"

    async def fake_pat_access(http_client: Any, *, owner_repo: str, pat: str) -> bool:
        received["owner_repo"] = owner_repo
        received["pat"] = pat
        return False

    async def fake_is_public(http_client: Any, *, owner_repo: str) -> bool:
        nonlocal public_called
        public_called = True
        return True

    set_binding_called = False

    async def fake_set_binding(*args: Any, **kwargs: Any) -> Any:
        nonlocal set_binding_called
        set_binding_called = True
        return MagicMock()

    async def fake_find(*args: Any, **kwargs: Any) -> Any:
        return _fake_ma_agent_for_bind(tenant_id)

    monkeypatch.setattr(modals_mod, "load_agent_inline_pat", fake_load_inline_pat)
    monkeypatch.setattr(modals_mod, "pat_can_access_repo", fake_pat_access)
    monkeypatch.setattr(modals_mod, "is_public_repo", fake_is_public)
    monkeypatch.setattr(modals_mod, "set_agent_repo_binding", fake_set_binding)
    monkeypatch.setattr(modals_mod, "find_agent_by_daimon_tag", fake_find)

    selected = _entry("a")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = _runtime_for_view(anthropic=build_stub_anthropic(), tenant_id=tenant_id)

    modal = RepoAuthModal(state, runtime=runtime, allowed_user_id=42)
    modal.url_in._value = "https://github.com/me/other-repo"  # pyright: ignore[reportPrivateUsage]
    modal.branch_in._value = "main"  # pyright: ignore[reportPrivateUsage]
    modal.pat_in._value = ""  # pyright: ignore[reportPrivateUsage]

    interaction = _interaction()
    await modal.on_submit(interaction)

    assert not set_binding_called, (
        "bind must be refused when the stored inline PAT can't access the new repo"
    )
    assert not public_called, "must not fall through to the public-visibility probe on refusal"
    assert received.get("owner_repo") == "me/other-repo", (
        "re-verification must receive the normalized owner/repo, not the raw URL"
    )
    assert received.get("pat") == "ghp_stale_stored_pat", (
        "re-verification must check the stored PAT, not the (blank) submitted one"
    )
    interaction.followup.send.assert_called_once()
    call_text = str(interaction.followup.send.call_args).lower()
    assert "stored" in call_text and "token" in call_text, (
        "the refusal message must mention the stored token"
    )


@pytest.mark.asyncio
async def test_blank_pat_bind_writes_inline_pat_ref_and_proof_when_stored_pat_covers_repo(
    monkeypatch: pytest.MonkeyPatch,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """When the stored inline PAT DOES cover the newly typed repo, the bind
    succeeds and records inline-pat:{agent} with proof kind='pat' -- skipping
    the public-visibility probe entirely, since select_clone_auth gives a
    per-agent PAT unconditional precedence regardless of what that probe
    would otherwise report."""
    tenant = await make_tenant(
        db_session, platform="discord", workspace_id="test-blank-pat-covers", id=tenant_id
    )
    await make_account(db_session, tenant=tenant, id=account_id)
    await db_session.flush()

    public_called = False

    async def fake_load_inline_pat(runtime: Any, *, agent_id: uuid.UUID) -> str | None:
        return "ghp_covers_new_repo"

    async def fake_pat_access(http_client: Any, *, owner_repo: str, pat: str) -> bool:
        return True

    async def fake_is_public(http_client: Any, *, owner_repo: str) -> bool:
        nonlocal public_called
        public_called = True
        return True

    async def fake_reconcile(runtime: Any, state: PanelState, *, tenant_id: uuid.UUID) -> Any:
        return MagicMock()

    async def fake_find(*args: Any, **kwargs: Any) -> Any:
        return _fake_ma_agent_for_bind(tenant_id)

    monkeypatch.setattr(modals_mod, "load_agent_inline_pat", fake_load_inline_pat)
    monkeypatch.setattr(modals_mod, "pat_can_access_repo", fake_pat_access)
    monkeypatch.setattr(modals_mod, "is_public_repo", fake_is_public)
    monkeypatch.setattr(modals_mod, "call_reconcile_for_panel", fake_reconcile)
    monkeypatch.setattr(modals_mod, "find_agent_by_daimon_tag", fake_find)

    selected = _entry("a")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = _runtime_for_view(
        anthropic=build_stub_anthropic(), tenant_id=tenant_id, sessionmaker=db_session_factory
    )

    modal = RepoAuthModal(state, runtime=runtime, allowed_user_id=42)
    modal.url_in._value = "https://github.com/me/covered-repo"  # pyright: ignore[reportPrivateUsage]
    modal.branch_in._value = "main"  # pyright: ignore[reportPrivateUsage]
    modal.pat_in._value = ""  # pyright: ignore[reportPrivateUsage]

    await modal.on_submit(_interaction())

    assert not public_called, (
        "must skip the public-visibility probe once the stored PAT covers the repo"
    )

    expected_agent_uuid = derive_agent_uuid(
        tenant_id=tenant_id, ma_agent_id="agent_bindtest_abcdefgh1234"
    )
    row = await get_binding(db_session, tenant_id=tenant_id, agent_id=expected_agent_uuid)
    assert row is not None, "the bind must write an agent_repo_binding row"
    assert row.ma_secret_ref == f"inline-pat:{expected_agent_uuid}", (
        "a covered blank-PAT bind must record the stored inline PAT's ref"
    )
    # proof_kind / proof_at / proof_account_id must all be recorded on the write.
    assert row.proof_kind == "pat", "a stored-PAT-covers-repo bind must record proof_kind='pat'"
    assert row.proof_at is not None, "proof must record a timestamp"
    assert row.proof_account_id == account_id, "proof must record the submitting account"


@pytest.mark.asyncio
async def test_blank_pat_bind_falls_through_to_public_check_without_stored_pat(
    monkeypatch: pytest.MonkeyPatch, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> None:
    """No stored inline PAT for this agent -> the pre-existing anon:
    App-coverage / public-visibility logic must run completely unchanged."""
    public_called = False

    async def fake_load_inline_pat(runtime: Any, *, agent_id: uuid.UUID) -> str | None:
        return None

    async def fake_is_public(http_client: Any, *, owner_repo: str) -> bool:
        nonlocal public_called
        public_called = True
        return True

    captured: dict[str, Any] = {}

    async def fake_set_binding(*args: Any, **kwargs: Any) -> Any:
        captured["ma_secret_ref"] = kwargs["ma_secret_ref"]
        return MagicMock()

    async def fake_reconcile(runtime: Any, state: PanelState, *, tenant_id: uuid.UUID) -> Any:
        return MagicMock()

    async def fake_find(*args: Any, **kwargs: Any) -> Any:
        return _fake_ma_agent_for_bind(tenant_id)

    monkeypatch.setattr(modals_mod, "load_agent_inline_pat", fake_load_inline_pat)
    monkeypatch.setattr(modals_mod, "is_public_repo", fake_is_public)
    monkeypatch.setattr(modals_mod, "set_agent_repo_binding", fake_set_binding)
    monkeypatch.setattr(modals_mod, "call_reconcile_for_panel", fake_reconcile)
    monkeypatch.setattr(modals_mod, "find_agent_by_daimon_tag", fake_find)

    selected = _entry("a")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    sm = MagicMock()
    sm.begin.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
    sm.begin.return_value.__aexit__ = AsyncMock(return_value=False)
    runtime = _runtime_for_view(
        anthropic=build_stub_anthropic(), tenant_id=tenant_id, sessionmaker=sm
    )

    modal = RepoAuthModal(state, runtime=runtime, allowed_user_id=42)
    modal.url_in._value = "https://github.com/me/public-repo"  # pyright: ignore[reportPrivateUsage]
    modal.branch_in._value = "main"  # pyright: ignore[reportPrivateUsage]
    modal.pat_in._value = ""  # pyright: ignore[reportPrivateUsage]

    await modal.on_submit(_interaction())

    assert public_called, (
        "no stored inline PAT -> the existing public-visibility check must still run"
    )
    assert captured.get("ma_secret_ref") == "anon:", (
        "unchanged anon: behavior when no inline PAT is stored for this agent"
    )


# ---------------------------------------------------------------------------
# Modal submit returns to the launching view (EditView), not AgentSetupView
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_section_submit_returns_to_edit_view(
    monkeypatch: pytest.MonkeyPatch, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> None:
    """AgentSectionModal.on_submit must return the user to EditView, not AgentSetupView."""
    from daimon.adapters.discord.agent_setup.edit_view import EditView

    async def fake_reconcile(runtime: Any, state: PanelState, *, tenant_id: uuid.UUID) -> Any:
        return MagicMock()

    monkeypatch.setattr(modals_mod, "call_reconcile_for_panel", fake_reconcile)

    selected = _entry("immutable-name")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = _runtime_for_view(anthropic=build_stub_anthropic(), tenant_id=tenant_id)

    modal = AgentSectionModal(state, runtime=runtime, allowed_user_id=99)
    modal.model_in._value = "claude-sonnet-4-6"  # pyright: ignore[reportPrivateUsage]

    interaction = _interaction()
    await modal.on_submit(interaction)

    interaction.edit_original_response.assert_called_once()
    view_kwarg = interaction.edit_original_response.call_args.kwargs["view"]
    assert isinstance(view_kwarg, EditView), (
        "AgentSectionModal submit must return to EditView, not AgentSetupView"
    )
    assert view_kwarg.allowed_user_id == 99, "the invoker gate must survive the round trip"


@pytest.mark.asyncio
async def test_repo_auth_submit_returns_to_edit_view(
    monkeypatch: pytest.MonkeyPatch, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> None:
    """RepoAuthModal.on_submit must return the user to EditView, not AgentSetupView."""
    from daimon.adapters.discord.agent_setup.edit_view import EditView

    async def fake_is_public(http_client: Any, *, owner_repo: str) -> bool:
        return True

    async def fake_set_binding(*args: Any, **kwargs: Any) -> Any:
        return MagicMock()

    async def fake_reconcile(runtime: Any, state: PanelState, *, tenant_id: uuid.UUID) -> Any:
        return MagicMock()

    async def fake_find(*args: Any, **kwargs: Any) -> Any:
        return _fake_ma_agent_for_bind(tenant_id)

    monkeypatch.setattr(modals_mod, "is_public_repo", fake_is_public)
    monkeypatch.setattr(modals_mod, "set_agent_repo_binding", fake_set_binding)
    monkeypatch.setattr(modals_mod, "call_reconcile_for_panel", fake_reconcile)
    monkeypatch.setattr(modals_mod, "find_agent_by_daimon_tag", fake_find)

    selected = _entry("a")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    sm = MagicMock()
    sm.begin.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
    sm.begin.return_value.__aexit__ = AsyncMock(return_value=False)
    runtime = _runtime_for_view(
        anthropic=build_stub_anthropic(), tenant_id=tenant_id, sessionmaker=sm
    )

    modal = RepoAuthModal(state, runtime=runtime, allowed_user_id=99)
    modal.url_in._value = "https://github.com/me/public-repo"  # pyright: ignore[reportPrivateUsage]
    modal.branch_in._value = "main"  # pyright: ignore[reportPrivateUsage]
    modal.pat_in._value = ""  # pyright: ignore[reportPrivateUsage]

    interaction = _interaction()
    await modal.on_submit(interaction)

    interaction.edit_original_response.assert_called_once()
    view_kwarg = interaction.edit_original_response.call_args.kwargs["view"]
    assert isinstance(view_kwarg, EditView), (
        "RepoAuthModal submit must return to EditView, not AgentSetupView"
    )
    assert view_kwarg.allowed_user_id == 99, "the invoker gate must survive the round trip"


@pytest.mark.asyncio
async def test_add_skill_submit_returns_to_edit_view(
    monkeypatch: pytest.MonkeyPatch, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> None:
    """AddSkillModal.on_submit must return the user to EditView, not AgentSetupView."""
    from daimon.adapters.discord.agent_setup.edit_view import EditView

    async def fake_kickoff(
        runtime: Any, *, tenant_id: uuid.UUID, account_id: uuid.UUID, agent_name: str, repo_url: str
    ) -> SyncReport:
        return SyncReport(synced=1)

    monkeypatch.setattr(modals_mod, "kick_off_skill_sync", fake_kickoff)

    selected = _entry("research-bot")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = _runtime_for_view(anthropic=build_stub_anthropic(), tenant_id=tenant_id)

    modal = AddSkillModal(state, runtime=runtime, allowed_user_id=99)
    modal.url_in._value = "https://github.com/me/skills-repo"  # pyright: ignore[reportPrivateUsage]

    interaction = _interaction()
    await modal.on_submit(interaction)
    await asyncio.sleep(0)  # let the fire-and-forget sync task finish

    interaction.edit_original_response.assert_called_once()
    view_kwarg = interaction.edit_original_response.call_args.kwargs["view"]
    assert isinstance(view_kwarg, EditView), (
        "AddSkillModal submit must return to EditView, not AgentSetupView"
    )
    assert view_kwarg.allowed_user_id == 99, "the invoker gate must survive the round trip"


# ---------------------------------------------------------------------------
# Repo URL becomes optional — the PAT-only path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repo_auth_refuses_submit_with_no_url_and_no_pat(
    monkeypatch: pytest.MonkeyPatch, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> None:
    """Both fields blank must be refused before defer(), not written as an
    empty binding."""
    set_binding_called = False

    async def fake_set_binding(*args: Any, **kwargs: Any) -> Any:
        nonlocal set_binding_called
        set_binding_called = True
        return MagicMock()

    monkeypatch.setattr(modals_mod, "set_agent_repo_binding", fake_set_binding)

    selected = _entry("a")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = _runtime_for_view(anthropic=build_stub_anthropic(), tenant_id=tenant_id)

    modal = RepoAuthModal(state, runtime=runtime, allowed_user_id=42)
    modal.url_in._value = ""  # pyright: ignore[reportPrivateUsage]
    modal.pat_in._value = ""  # pyright: ignore[reportPrivateUsage]

    interaction = _interaction()
    await modal.on_submit(interaction)

    interaction.response.send_message.assert_called_once()
    call_text = str(interaction.response.send_message.call_args)
    assert "Enter a repo URL" in call_text, "user must see the validation copy"
    interaction.response.defer.assert_not_called()
    assert not set_binding_called, "a fully blank submit must never write a binding"


@pytest.mark.asyncio
async def test_pat_only_submit_stores_pat_and_writes_no_binding(
    monkeypatch: pytest.MonkeyPatch, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> None:
    """A PAT submitted with no repo URL is verified via GET /user, stored, and
    leaves agent_repo_binding untouched."""
    stored: dict[str, Any] = {}
    set_binding_called = False
    reconcile_called = False

    async def fake_is_valid_pat(http_client: Any, *, pat: str) -> bool:
        return True

    async def fake_store_inline_pat(
        runtime: Any, *, account_id: uuid.UUID, agent_id: uuid.UUID, plaintext_pat: str
    ) -> str:
        stored["agent_id"] = agent_id
        stored["plaintext_pat"] = plaintext_pat
        return f"inline-pat:{agent_id}"

    async def fake_set_binding(*args: Any, **kwargs: Any) -> Any:
        nonlocal set_binding_called
        set_binding_called = True
        return MagicMock()

    async def fake_reconcile(runtime: Any, state: PanelState, *, tenant_id: uuid.UUID) -> Any:
        nonlocal reconcile_called
        reconcile_called = True
        return MagicMock()

    async def fake_find(*args: Any, **kwargs: Any) -> Any:
        return _fake_ma_agent_for_bind(tenant_id)

    monkeypatch.setattr(modals_mod, "is_valid_pat", fake_is_valid_pat)
    monkeypatch.setattr(modals_mod, "store_inline_pat", fake_store_inline_pat)
    monkeypatch.setattr(modals_mod, "set_agent_repo_binding", fake_set_binding)
    monkeypatch.setattr(modals_mod, "call_reconcile_for_panel", fake_reconcile)
    monkeypatch.setattr(modals_mod, "find_agent_by_daimon_tag", fake_find)

    selected = _entry("a")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = _runtime_for_view(anthropic=build_stub_anthropic(), tenant_id=tenant_id)

    modal = RepoAuthModal(state, runtime=runtime, allowed_user_id=42)
    modal.url_in._value = ""  # pyright: ignore[reportPrivateUsage]
    modal.branch_in._value = "main"  # pyright: ignore[reportPrivateUsage]
    modal.pat_in._value = "ghp_pat_only_token_1234"  # pyright: ignore[reportPrivateUsage]

    interaction = _interaction()
    await modal.on_submit(interaction)

    assert stored.get("plaintext_pat") == "ghp_pat_only_token_1234", (
        "the submitted token must reach store_inline_pat"
    )
    assert not set_binding_called, "a PAT-only submit must write no agent_repo_binding row"
    assert not reconcile_called, "nothing on the AgentSpec changed; reconcile must not run"
    assert state.pat_last4 == "1234", "state must show the stored token's last-4"
    assert state.bound_repo_url is None, "a PAT-only submit must not pin a repo"
    interaction.followup.send.assert_called_once()
    call_text = str(interaction.followup.send.call_args)
    assert "GitHub MCP" in call_text, "the user must see the GitHub MCP explanation"


@pytest.mark.asyncio
async def test_pat_only_submit_refused_when_github_rejects_token(
    monkeypatch: pytest.MonkeyPatch, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> None:
    """GitHub rejecting the token (401 from GET /user) must refuse the store."""
    store_called = False

    async def fake_is_valid_pat(http_client: Any, *, pat: str) -> bool:
        return False

    async def fake_store_inline_pat(*args: Any, **kwargs: Any) -> str:
        nonlocal store_called
        store_called = True
        return "inline-pat:unused"

    async def fake_find(*args: Any, **kwargs: Any) -> Any:
        return _fake_ma_agent_for_bind(tenant_id)

    monkeypatch.setattr(modals_mod, "is_valid_pat", fake_is_valid_pat)
    monkeypatch.setattr(modals_mod, "store_inline_pat", fake_store_inline_pat)
    monkeypatch.setattr(modals_mod, "find_agent_by_daimon_tag", fake_find)

    selected = _entry("a")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = _runtime_for_view(anthropic=build_stub_anthropic(), tenant_id=tenant_id)

    modal = RepoAuthModal(state, runtime=runtime, allowed_user_id=42)
    modal.url_in._value = ""  # pyright: ignore[reportPrivateUsage]
    modal.pat_in._value = "ghp_bad_token"  # pyright: ignore[reportPrivateUsage]

    interaction = _interaction()
    await modal.on_submit(interaction)

    assert not store_called, "a rejected token must never reach store_inline_pat"
    interaction.followup.send.assert_called_once()
    call_text = str(interaction.followup.send.call_args)
    assert "rejected" in call_text.lower(), (
        "the failure must surface through the existing followup.send error path"
    )


@pytest.mark.asyncio
async def test_pat_only_submit_does_not_call_repo_visibility_helpers(
    monkeypatch: pytest.MonkeyPatch, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> None:
    """No repo given -> none of the repo-scoped visibility helpers may be
    called."""
    pat_access_called = False
    is_public_called = False

    async def fake_is_valid_pat(http_client: Any, *, pat: str) -> bool:
        return True

    async def fake_pat_access(http_client: Any, *, owner_repo: str, pat: str) -> bool:
        nonlocal pat_access_called
        pat_access_called = True
        return True

    async def fake_is_public(http_client: Any, *, owner_repo: str) -> bool:
        nonlocal is_public_called
        is_public_called = True
        return True

    async def fake_store_inline_pat(*args: Any, **kwargs: Any) -> str:
        return "inline-pat:test"

    async def fake_find(*args: Any, **kwargs: Any) -> Any:
        return _fake_ma_agent_for_bind(tenant_id)

    monkeypatch.setattr(modals_mod, "is_valid_pat", fake_is_valid_pat)
    monkeypatch.setattr(modals_mod, "pat_can_access_repo", fake_pat_access)
    monkeypatch.setattr(modals_mod, "is_public_repo", fake_is_public)
    monkeypatch.setattr(modals_mod, "store_inline_pat", fake_store_inline_pat)
    monkeypatch.setattr(modals_mod, "find_agent_by_daimon_tag", fake_find)

    selected = _entry("a")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = _runtime_for_view(anthropic=build_stub_anthropic(), tenant_id=tenant_id)

    modal = RepoAuthModal(state, runtime=runtime, allowed_user_id=42)
    modal.url_in._value = ""  # pyright: ignore[reportPrivateUsage]
    modal.pat_in._value = "ghp_no_repo_token"  # pyright: ignore[reportPrivateUsage]

    await modal.on_submit(_interaction())

    assert not pat_access_called, (
        "pat_can_access_repo is repo-scoped; must not run when no repo is given"
    )
    assert not is_public_called, "is_public_repo is repo-scoped; must not run when no repo is given"


def test_repo_auth_modal_field_labels_fit_discord_limits(
    tenant_id: uuid.UUID, account_id: uuid.UUID
) -> None:
    """Every RepoAuthModal TextInput label/placeholder must respect Discord's
    45/100-char caps, and the token field's label must mention GitHub MCP."""
    selected = _entry("a")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = _runtime_for_view(anthropic=build_stub_anthropic(), tenant_id=tenant_id)

    modal = RepoAuthModal(state, runtime=runtime, allowed_user_id=42)
    for field in (modal.url_in, modal.branch_in, modal.pat_in):
        assert len(field.label) <= 45, f"{field.label!r} exceeds Discord's 45-char label cap"
        if field.placeholder is not None:
            assert len(field.placeholder) <= 100, (
                f"{field.placeholder!r} exceeds Discord's 100-char placeholder cap"
            )
    assert "GitHub MCP" in modal.pat_in.label, (
        "the token field's label must mention the GitHub MCP server"
    )


# ----- 13. Click-time authz gate on the mutating write callbacks ------------
#
# AgentSectionModal and AddSkillModal each carry a write to the agent spec
# (system/model, skills). A non-admin caller must be refused at submit time
# when the target is currently reachable or is a system agent — the guard is
# re-derived from live state, never from any render-time snapshot.
# RepoAuthModal is guarded too, but through the shared-state guard rather than
# the spec guard: it never writes the agent spec, so an admin may still bind a
# repo to the built-in agent — the first-run onboarding step — while a
# non-admin is refused whenever the target is a system agent or is currently
# reachable.


@pytest.mark.asyncio
async def test_agent_section_modal_refuses_write_when_target_is_reachable_for_non_admin(
    monkeypatch: pytest.MonkeyPatch,
    account_id: uuid.UUID,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from daimon.testing.factories import make_tenant

    reconcile_calls: list[str] = []

    async def spy_reconcile(runtime: Any, state: PanelState, *, tenant_id: uuid.UUID) -> Any:
        reconcile_calls.append("called")
        return MagicMock()

    monkeypatch.setattr(modals_mod, "call_reconcile_for_panel", spy_reconcile)

    guild_id = 910001
    async with db_session_factory() as session, session.begin():
        await make_tenant(session, platform="discord", workspace_id=str(guild_id))

    selected = _entry("a")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = _runtime_for_view(
        anthropic=build_stub_anthropic(),
        tenant_id=uuid.uuid4(),
        sessionmaker=db_session_factory,
        deployment_default=DeploymentDefault(agent_name="a"),
    )

    modal = AgentSectionModal(state, runtime=runtime, allowed_user_id=42)
    modal.model_in._value = "claude-sonnet-4-6"  # pyright: ignore[reportPrivateUsage]

    interaction = _interaction(is_admin=False, guild_id=guild_id)
    await modal.on_submit(interaction)

    assert reconcile_calls == [], "reconcile must not run when the write is refused"
    interaction.response.send_message.assert_called_once()
    assert interaction.response.send_message.call_args.kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_agent_section_modal_refuses_write_on_system_agent_even_for_admin(
    monkeypatch: pytest.MonkeyPatch, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> None:
    reconcile_calls: list[str] = []

    async def spy_reconcile(runtime: Any, state: PanelState, *, tenant_id: uuid.UUID) -> Any:
        reconcile_calls.append("called")
        return MagicMock()

    monkeypatch.setattr(modals_mod, "call_reconcile_for_panel", spy_reconcile)

    selected = _entry("daimon", is_system=True)
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = _runtime_for_view(anthropic=build_stub_anthropic(), tenant_id=tenant_id)

    modal = AgentSectionModal(state, runtime=runtime, allowed_user_id=42)
    modal.model_in._value = "claude-sonnet-4-6"  # pyright: ignore[reportPrivateUsage]

    interaction = _interaction(is_admin=True)
    await modal.on_submit(interaction)

    assert reconcile_calls == [], "a system agent's spec must never be reconciled from the panel"
    interaction.response.send_message.assert_called_once()
    assert interaction.response.send_message.call_args.kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_add_skill_modal_refuses_write_when_target_is_reachable_for_non_admin(
    monkeypatch: pytest.MonkeyPatch,
    account_id: uuid.UUID,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from daimon.testing.factories import make_tenant

    async def unexpected_kickoff(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("skill sync must not be kicked off when the write is refused")

    monkeypatch.setattr(modals_mod, "kick_off_skill_sync", unexpected_kickoff)

    guild_id = 910002
    async with db_session_factory() as session, session.begin():
        await make_tenant(session, platform="discord", workspace_id=str(guild_id))

    selected = _entry("research-bot")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = _runtime_for_view(
        anthropic=build_stub_anthropic(),
        tenant_id=uuid.uuid4(),
        sessionmaker=db_session_factory,
        deployment_default=DeploymentDefault(agent_name="research-bot"),
    )

    modal = AddSkillModal(state, runtime=runtime, allowed_user_id=42)
    modal.url_in._value = "https://github.com/me/skills-repo"  # pyright: ignore[reportPrivateUsage]

    interaction = _interaction(is_admin=False, guild_id=guild_id)
    await modal.on_submit(interaction)

    assert state.pending_skill_repo_urls == [], (
        "a refused submit must leave no pending-skill marker on the panel"
    )
    interaction.response.send_message.assert_called_once()
    assert interaction.response.send_message.call_args.kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_add_skill_modal_refuses_write_on_system_agent_even_for_admin(
    monkeypatch: pytest.MonkeyPatch, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> None:
    async def unexpected_kickoff(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("skill sync must not be kicked off when the write is refused")

    monkeypatch.setattr(modals_mod, "kick_off_skill_sync", unexpected_kickoff)

    selected = _entry("daimon", is_system=True)
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = _runtime_for_view(anthropic=build_stub_anthropic(), tenant_id=tenant_id)

    modal = AddSkillModal(state, runtime=runtime, allowed_user_id=42)
    modal.url_in._value = "https://github.com/me/skills-repo"  # pyright: ignore[reportPrivateUsage]

    interaction = _interaction(is_admin=True)
    await modal.on_submit(interaction)

    assert state.pending_skill_repo_urls == [], (
        "a system agent's skills must never gain a pending-skill marker from the panel"
    )
    interaction.response.send_message.assert_called_once()
    assert interaction.response.send_message.call_args.kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_repo_auth_modal_refuses_binding_on_reachable_system_agent_for_non_admin(
    monkeypatch: pytest.MonkeyPatch, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> None:
    """A repo binding is shared state: everyone in the install talks to the
    deployment's built-in agent, so a non-admin re-pointing it at a repo of
    their choosing is refused at submit time."""
    set_binding_called = False

    async def fake_is_public(http_client: Any, *, owner_repo: str) -> bool:
        return True

    async def fake_set_binding(*args: Any, **kwargs: Any) -> Any:
        nonlocal set_binding_called
        set_binding_called = True
        return MagicMock()

    async def fake_reconcile(runtime: Any, state: PanelState, *, tenant_id: uuid.UUID) -> Any:
        return MagicMock()

    async def fake_find(*args: Any, **kwargs: Any) -> Any:
        return _fake_ma_agent_for_bind(tenant_id)

    monkeypatch.setattr(modals_mod, "is_public_repo", fake_is_public)
    monkeypatch.setattr(modals_mod, "set_agent_repo_binding", fake_set_binding)
    monkeypatch.setattr(modals_mod, "call_reconcile_for_panel", fake_reconcile)
    monkeypatch.setattr(modals_mod, "find_agent_by_daimon_tag", fake_find)

    selected = _entry("daimon", is_system=True)
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    sm = MagicMock()
    sm.begin.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
    sm.begin.return_value.__aexit__ = AsyncMock(return_value=False)
    runtime = _runtime_for_view(
        anthropic=build_stub_anthropic(),
        tenant_id=tenant_id,
        sessionmaker=sm,
        deployment_default=DeploymentDefault(agent_name="daimon"),
    )

    modal = RepoAuthModal(state, runtime=runtime, allowed_user_id=42)
    modal.url_in._value = "https://github.com/me/public-repo"  # pyright: ignore[reportPrivateUsage]
    modal.branch_in._value = "main"  # pyright: ignore[reportPrivateUsage]
    modal.pat_in._value = ""  # pyright: ignore[reportPrivateUsage]

    interaction = _interaction(is_admin=False)
    await modal.on_submit(interaction)

    assert set_binding_called is False, (
        "a non-admin must not bind a repo to the deployment's built-in agent"
    )
    interaction.response.send_message.assert_called_once()
    assert interaction.response.send_message.call_args.kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_repo_auth_modal_still_writes_binding_on_reachable_system_agent_for_admin(
    monkeypatch: pytest.MonkeyPatch, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> None:
    """Pointing the built-in default agent at a repo is the first thing an
    admin does on a fresh install, and the built-in agent is both a system
    agent and the workspace's default. That bind must keep working."""
    set_binding_called = False

    async def fake_is_public(http_client: Any, *, owner_repo: str) -> bool:
        return True

    async def fake_set_binding(*args: Any, **kwargs: Any) -> Any:
        nonlocal set_binding_called
        set_binding_called = True
        return MagicMock()

    async def fake_reconcile(runtime: Any, state: PanelState, *, tenant_id: uuid.UUID) -> Any:
        return MagicMock()

    async def fake_find(*args: Any, **kwargs: Any) -> Any:
        return _fake_ma_agent_for_bind(tenant_id)

    monkeypatch.setattr(modals_mod, "is_public_repo", fake_is_public)
    monkeypatch.setattr(modals_mod, "set_agent_repo_binding", fake_set_binding)
    monkeypatch.setattr(modals_mod, "call_reconcile_for_panel", fake_reconcile)
    monkeypatch.setattr(modals_mod, "find_agent_by_daimon_tag", fake_find)

    selected = _entry("daimon", is_system=True)
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    sm = MagicMock()
    sm.begin.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
    sm.begin.return_value.__aexit__ = AsyncMock(return_value=False)
    runtime = _runtime_for_view(
        anthropic=build_stub_anthropic(),
        tenant_id=tenant_id,
        sessionmaker=sm,
        deployment_default=DeploymentDefault(agent_name="daimon"),
    )

    modal = RepoAuthModal(state, runtime=runtime, allowed_user_id=42)
    modal.url_in._value = "https://github.com/me/public-repo"  # pyright: ignore[reportPrivateUsage]
    modal.branch_in._value = "main"  # pyright: ignore[reportPrivateUsage]
    modal.pat_in._value = ""  # pyright: ignore[reportPrivateUsage]

    interaction = _interaction(is_admin=True)
    await modal.on_submit(interaction)

    assert set_binding_called is True, (
        "an admin binding a repo to the built-in default agent must still write the binding"
    )


@pytest.mark.asyncio
async def test_repo_auth_modal_refuses_binding_on_reachable_non_system_agent_for_non_admin(
    monkeypatch: pytest.MonkeyPatch,
    account_id: uuid.UUID,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A user-created agent that something currently resolves to is shared the
    moment it is scoped, so its repo binding closes to non-admins even though
    the agent carries no system provenance."""
    set_binding_called = False

    async def fake_is_public(http_client: Any, *, owner_repo: str) -> bool:
        return True

    async def fake_set_binding(*args: Any, **kwargs: Any) -> Any:
        nonlocal set_binding_called
        set_binding_called = True
        return MagicMock()

    async def fake_reconcile(runtime: Any, state: PanelState, *, tenant_id: uuid.UUID) -> Any:
        return MagicMock()

    async def fake_find(*args: Any, **kwargs: Any) -> Any:
        return _fake_ma_agent_for_bind(uuid.uuid4())

    monkeypatch.setattr(modals_mod, "is_public_repo", fake_is_public)
    monkeypatch.setattr(modals_mod, "set_agent_repo_binding", fake_set_binding)
    monkeypatch.setattr(modals_mod, "call_reconcile_for_panel", fake_reconcile)
    monkeypatch.setattr(modals_mod, "find_agent_by_daimon_tag", fake_find)

    guild_id = 910201
    async with db_session_factory() as session, session.begin():
        await make_tenant(session, platform="discord", workspace_id=str(guild_id))

    selected = _entry("bot")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = _runtime_for_view(
        anthropic=build_stub_anthropic(),
        tenant_id=uuid.uuid4(),
        sessionmaker=db_session_factory,
        deployment_default=DeploymentDefault(agent_name="bot"),
    )

    modal = RepoAuthModal(state, runtime=runtime, allowed_user_id=42)
    modal.url_in._value = "https://github.com/me/public-repo"  # pyright: ignore[reportPrivateUsage]
    modal.branch_in._value = "main"  # pyright: ignore[reportPrivateUsage]
    modal.pat_in._value = ""  # pyright: ignore[reportPrivateUsage]

    interaction = _interaction(is_admin=False, guild_id=guild_id)
    await modal.on_submit(interaction)

    assert set_binding_called is False, (
        "a non-admin must not re-point a currently-reachable agent's repo"
    )
    interaction.response.send_message.assert_called_once()
    assert interaction.response.send_message.call_args.kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_repo_auth_modal_writes_binding_on_unreachable_agent_for_non_admin(
    monkeypatch: pytest.MonkeyPatch,
    account_id: uuid.UUID,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An agent nobody has scoped is the member's own scratchpad — the gate
    closes shared agents, not every agent a member owns."""
    set_binding_called = False

    async def fake_is_public(http_client: Any, *, owner_repo: str) -> bool:
        return True

    async def fake_set_binding(*args: Any, **kwargs: Any) -> Any:
        nonlocal set_binding_called
        set_binding_called = True
        return MagicMock()

    async def fake_reconcile(runtime: Any, state: PanelState, *, tenant_id: uuid.UUID) -> Any:
        return MagicMock()

    async def fake_find(*args: Any, **kwargs: Any) -> Any:
        return _fake_ma_agent_for_bind(uuid.uuid4())

    monkeypatch.setattr(modals_mod, "is_public_repo", fake_is_public)
    monkeypatch.setattr(modals_mod, "set_agent_repo_binding", fake_set_binding)
    monkeypatch.setattr(modals_mod, "call_reconcile_for_panel", fake_reconcile)
    monkeypatch.setattr(modals_mod, "find_agent_by_daimon_tag", fake_find)

    guild_id = 910202
    async with db_session_factory() as session, session.begin():
        await make_tenant(session, platform="discord", workspace_id=str(guild_id))

    selected = _entry("scratchpad")
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = _runtime_for_view(
        anthropic=build_stub_anthropic(),
        tenant_id=uuid.uuid4(),
        sessionmaker=db_session_factory,
        deployment_default=DeploymentDefault(agent_name="daimon"),
    )

    modal = RepoAuthModal(state, runtime=runtime, allowed_user_id=42)
    modal.url_in._value = "https://github.com/me/public-repo"  # pyright: ignore[reportPrivateUsage]
    modal.branch_in._value = "main"  # pyright: ignore[reportPrivateUsage]
    modal.pat_in._value = ""  # pyright: ignore[reportPrivateUsage]

    interaction = _interaction(is_admin=False, guild_id=guild_id)
    await modal.on_submit(interaction)

    assert set_binding_called is True, (
        "a non-admin must still bind a repo to an agent nothing currently resolves to"
    )


@pytest.mark.asyncio
async def test_repo_auth_modal_refusal_logs_no_masked_token(
    monkeypatch: pytest.MonkeyPatch, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> None:
    """A refused submit must not leave the submitted token in the log stream,
    even masked — the gate runs before the submit log line."""

    async def unexpected_set_binding(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("no binding may be written on a refused submit")

    async def fake_find(*args: Any, **kwargs: Any) -> Any:
        return _fake_ma_agent_for_bind(tenant_id)

    monkeypatch.setattr(modals_mod, "set_agent_repo_binding", unexpected_set_binding)
    monkeypatch.setattr(modals_mod, "find_agent_by_daimon_tag", fake_find)

    selected = _entry("daimon", is_system=True)
    state = PanelState(roster=[selected], selected=selected, account_id=account_id)
    runtime = _runtime_for_view(
        anthropic=build_stub_anthropic(),
        tenant_id=tenant_id,
        deployment_default=DeploymentDefault(agent_name="daimon"),
    )

    modal = RepoAuthModal(state, runtime=runtime, allowed_user_id=42)
    modal.url_in._value = "https://github.com/me/private-repo"  # pyright: ignore[reportPrivateUsage]
    modal.branch_in._value = "main"  # pyright: ignore[reportPrivateUsage]
    modal.pat_in._value = "ghp_refusedtoken1234"  # pyright: ignore[reportPrivateUsage]

    interaction = _interaction(is_admin=False)
    cap = structlog.testing.LogCapture()
    structlog.configure(processors=[cap])
    try:
        await modal.on_submit(interaction)
    finally:
        structlog.reset_defaults()

    assert all("pat_masked" not in entry for entry in cap.entries), (
        "a refused submit must emit no log entry carrying the token, masked or not"
    )
    assert all("ghp_refusedtoken1234" not in str(entry) for entry in cap.entries), (
        "the plaintext token must never reach the log stream"
    )
