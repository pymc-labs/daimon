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
from daimon.core.stores.identity import get_or_create_cli_principal
from daimon.testing.ma import build_stub_anthropic
from pydantic import HttpUrl, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _entry(name: str, *, mcp_servers: list[Any] | None = None) -> RosterEntry:
    return RosterEntry(
        name=name,
        model="claude-sonnet-4-6",
        spec=AgentSpec(name=name, model="claude-sonnet-4-6", mcp_servers=mcp_servers),
    )


_DEFAULT_PUBLIC_URL = HttpUrl("https://example.com/mcp")


def _runtime_for_view(
    *,
    anthropic: Any,
    tenant_id: uuid.UUID,
    public_url: HttpUrl | None = _DEFAULT_PUBLIC_URL,
    sessionmaker: Any = None,
    crypto_keys: tuple[str, ...] = (),
) -> DiscordRuntime:
    settings = MagicMock()
    # Real McpSettings so the app_root_url property (strips /mcp) computes for real.
    # jwt_secret must be present so the per-agent vault bootstrap path can mint a JWT.
    settings.mcp = McpSettings(public_url=public_url, jwt_secret=SecretStr("a" * 32))
    settings.github.oauth_scopes = ("repo", "read:user")
    # No App creds by default -> is_app_installed_for_repo returns False with zero
    # HTTP calls; a MagicMock here would be truthy and crash build_app_jwt.
    # Tests that need App-coverage behavior monkeypatch is_app_installed_for_repo.
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
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
    )


def _interaction(user_id: int = 42) -> MagicMock:
    interaction = MagicMock()
    interaction.user.id = user_id
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.send_modal = AsyncMock()
    interaction.response.edit_message = AsyncMock()
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
    from daimon.core._models import Tenant
    from daimon.core.ma_identity import derive_tenant_uuid

    _tid = derive_tenant_uuid(platform="discord", workspace_id="test-guild-pat")
    tenant = Tenant(id=_tid, platform="discord", external_id="test-guild-pat")
    db_session.add(tenant)
    await db_session.flush()
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
    from daimon.core._models import Tenant
    from daimon.core.github_credentials import get_pat
    from daimon.core.ma_identity import derive_tenant_uuid

    _guild = str(uuid.uuid4())
    tenant = Tenant(
        id=derive_tenant_uuid(platform="discord", workspace_id=_guild),
        platform="discord",
        external_id=_guild,
    )
    db_session.add(tenant)
    await db_session.flush()
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
    monkeypatch: pytest.MonkeyPatch, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> None:
    """Happy path: after reconcile succeeds, the modal POSTs a static_bearer
    credential to the per-agent vault carrying the submitted URL and token.
    Reconcile signal observed BEFORE the credential POST."""
    from daimon.core.defaults.report import Action, ResourceOutcome

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
    runtime = _runtime_for_view(anthropic=build_stub_anthropic(handler), tenant_id=tenant_id)

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
    monkeypatch: pytest.MonkeyPatch, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> None:
    """Idempotent resubmit: second submission with the same URL DELETEs the
    prior static_bearer credential and POSTs a fresh one. Two POSTs total
    across both submissions; one DELETE on the second."""
    from daimon.core.defaults.report import Action, ResourceOutcome

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
    runtime = _runtime_for_view(anthropic=build_stub_anthropic(handler), tenant_id=tenant_id)

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
async def test_anon_bind_writes_binding_when_repo_public(
    monkeypatch: pytest.MonkeyPatch, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> None:
    """No-PAT bind of a verified-public repo writes an anon: binding."""
    captured: dict[str, Any] = {}

    async def fake_is_public(http_client: Any, *, owner_repo: str) -> bool:
        captured["owner_repo"] = owner_repo
        return True

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

    modal = RepoAuthModal(state, runtime=runtime, allowed_user_id=42)
    modal.url_in._value = "https://github.com/me/public-repo"  # pyright: ignore[reportPrivateUsage]
    modal.branch_in._value = "main"  # pyright: ignore[reportPrivateUsage]
    modal.pat_in._value = ""  # pyright: ignore[reportPrivateUsage]

    await modal.on_submit(_interaction())

    assert captured.get("owner_repo") == "me/public-repo", (
        "visibility check must receive normalized owner/repo"
    )
    assert captured.get("ma_secret_ref") == "anon:", "public no-PAT bind writes an anon: binding"


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
    assert "private" in call_text.lower(), (
        "user must see the 'repo is private — connect GitHub or paste a PAT' message"
    )


@pytest.mark.asyncio
async def test_pat_bind_skips_visibility_check(
    monkeypatch: pytest.MonkeyPatch, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> None:
    """A bind WITH a PAT takes the store_inline_pat path; no visibility call."""
    visibility_called = False

    async def fake_is_public(http_client: Any, *, owner_repo: str) -> bool:
        nonlocal visibility_called
        visibility_called = True
        return True

    async def fake_store_inline_pat(
        runtime: Any, *, account_id: uuid.UUID, agent_id: uuid.UUID, plaintext_pat: str
    ) -> str:
        return f"inline-pat:{agent_id}"

    async def fake_set_binding(*args: Any, **kwargs: Any) -> Any:
        return MagicMock()

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
    modal.url_in._value = "https://github.com/me/repo"  # pyright: ignore[reportPrivateUsage]
    modal.branch_in._value = "main"  # pyright: ignore[reportPrivateUsage]
    modal.pat_in._value = "ghp_some_pat_1234"  # pyright: ignore[reportPrivateUsage]

    await modal.on_submit(_interaction())

    assert not visibility_called, "PAT path must never call the public-visibility check"
    assert pat_access_called, (
        "PAT path must verify the token grants repo access before binding "
        "(cross-tenant App-token clone guard)"
    )


# ----- 12. App-coverage detection on the no-PAT bind path -----


@pytest.mark.asyncio
async def test_app_covered_bind_shows_covered_status_and_skips_public_check(
    monkeypatch: pytest.MonkeyPatch, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> None:
    """No PAT + App installed -> surfaces the App-covered status and skips the
    public-only visibility check entirely (App mode clones private repos too)."""
    public_check_called = False

    async def fake_is_public(http_client: Any, *, owner_repo: str) -> bool:
        nonlocal public_check_called
        public_check_called = True
        return False

    async def fake_app_installed(http_client: Any, **kwargs: Any) -> bool:
        return True

    captured: dict[str, Any] = {}

    async def fake_set_binding(*args: Any, **kwargs: Any) -> Any:
        captured["ma_secret_ref"] = kwargs["ma_secret_ref"]
        return MagicMock()

    async def fake_reconcile(runtime: Any, state: PanelState, *, tenant_id: uuid.UUID) -> Any:
        return MagicMock()

    async def fake_find(*args: Any, **kwargs: Any) -> Any:
        return _fake_ma_agent_for_bind(tenant_id)

    monkeypatch.setattr(modals_mod, "is_app_installed_for_repo", fake_app_installed)
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
    modal.url_in._value = "https://github.com/me/private-repo"  # pyright: ignore[reportPrivateUsage]
    modal.branch_in._value = "main"  # pyright: ignore[reportPrivateUsage]
    modal.pat_in._value = ""  # pyright: ignore[reportPrivateUsage]

    interaction = _interaction()
    await modal.on_submit(interaction)

    assert not public_check_called, "App-covered repo must skip the public-visibility check"
    assert captured.get("ma_secret_ref") == "anon:", "App-covered no-PAT bind still writes anon:"
    interaction.followup.send.assert_called_once()
    call_text = str(interaction.followup.send.call_args)
    assert "App-covered" in call_text, "user must see the App-coverage status message"


@pytest.mark.asyncio
async def test_app_not_installed_falls_back_to_existing_public_check(
    monkeypatch: pytest.MonkeyPatch, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> None:
    """No PAT + App not installed -> the existing public-repo check still runs
    and gates the anon: bind; no coverage-status toast is shown."""
    public_check_called = False

    async def fake_is_public(http_client: Any, *, owner_repo: str) -> bool:
        nonlocal public_check_called
        public_check_called = True
        return True

    async def fake_app_installed(http_client: Any, **kwargs: Any) -> bool:
        return False

    captured: dict[str, Any] = {}

    async def fake_set_binding(*args: Any, **kwargs: Any) -> Any:
        captured["ma_secret_ref"] = kwargs["ma_secret_ref"]
        return MagicMock()

    async def fake_reconcile(runtime: Any, state: PanelState, *, tenant_id: uuid.UUID) -> Any:
        return MagicMock()

    async def fake_find(*args: Any, **kwargs: Any) -> Any:
        return _fake_ma_agent_for_bind(tenant_id)

    monkeypatch.setattr(modals_mod, "is_app_installed_for_repo", fake_app_installed)
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

    interaction = _interaction()
    await modal.on_submit(interaction)

    assert public_check_called, "App-not-installed repo must fall back to the public check"
    assert captured.get("ma_secret_ref") == "anon:", "public no-PAT bind writes an anon: binding"
    interaction.followup.send.assert_not_called()


@pytest.mark.asyncio
async def test_app_coverage_probe_error_does_not_block_bind(
    monkeypatch: pytest.MonkeyPatch, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> None:
    """T-97-12: a coverage-probe failure (httpx.HTTPError) must never block the
    bind -- it degrades to the existing public-repo check, same as an
    App-less deployment, and the bind still completes."""

    async def failing_app_installed(http_client: Any, **kwargs: Any) -> bool:
        raise httpx.ConnectError("simulated network failure")

    async def fake_is_public(http_client: Any, *, owner_repo: str) -> bool:
        return True

    captured: dict[str, Any] = {}

    async def fake_set_binding(*args: Any, **kwargs: Any) -> Any:
        captured["ma_secret_ref"] = kwargs["ma_secret_ref"]
        return MagicMock()

    async def fake_reconcile(runtime: Any, state: PanelState, *, tenant_id: uuid.UUID) -> Any:
        return MagicMock()

    async def fake_find(*args: Any, **kwargs: Any) -> Any:
        return _fake_ma_agent_for_bind(tenant_id)

    monkeypatch.setattr(modals_mod, "is_app_installed_for_repo", failing_app_installed)
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

    interaction = _interaction()
    await modal.on_submit(interaction)

    assert captured.get("ma_secret_ref") == "anon:", (
        "a coverage-probe failure must never block the bind — it must still complete"
    )
    interaction.followup.send.assert_called_once()
    call_text = str(interaction.followup.send.call_args).lower()
    assert "couldn't verify" in call_text or "could not verify" in call_text, (
        "user should see a neutral note that App coverage could not be verified"
    )
