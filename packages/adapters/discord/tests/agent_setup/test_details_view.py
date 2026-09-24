"""Behavioural tests for the Details screen.

What matters here is honesty and reachability: the card must name the model a
reader recognises, say exactly what was recorded about the repo, list key names
without ever touching a value, carry the selected agent into setup, and refuse a
member's coding-tools click before any token exists.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import jwt as pyjwt
import pytest
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.adapters.discord.agent_setup import mcp_access as mcp_access_mod
from daimon.adapters.discord.agent_setup.budget import (
    LAYOUT_COMPONENT_BUDGET,
    LAYOUT_TEXT_BUDGET,
)
from daimon.adapters.discord.agent_setup.details_view import (
    SHOW_FEWER_LABEL,
    SHOW_MORE_LABEL,
    DetailsView,
    build_details_container,
    coding_tools_refusal,
)
from daimon.adapters.discord.agent_setup.state import PanelState
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.agent_detail_lists import DETAIL_LIST_COLLAPSED_COUNT, DetailListName
from daimon.core.agent_details import (
    AgentDetails,
    KeyEntry,
    McpServerEntry,
    RepoBinding,
    SkillEntry,
    build_agent_details,
)
from daimon.core.github_repo_auth import RepoAccess
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.roster import RosterAgent
from daimon.core.scope import AnsweringPlace, DeploymentDefault
from daimon.core.setup_conversations import setup_target_label, shared_keys_sentence
from daimon.core.stores.domain import AccountRow, TenantRow
from daimon.core.stores.mcp_tokens import get_mcp_token
from daimon.testing import ma_agent
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import build_stub_anthropic
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_PUBLIC_URL = "https://mcp.example.com"
_JWT_SECRET = "test-jwt-secret-32-bytes-padding!"
_FIXED_CHECK = dt.datetime(2026, 3, 4, tzinfo=dt.UTC)


def _roster_agent(name: str = "research-bot", *, ma_agent_id: str = "ag_research") -> RosterAgent:
    return RosterAgent(
        name=name, ma_agent_id=ma_agent_id, model_id="claude-sonnet-4-6", is_built_in=False
    )


def _details(
    *,
    name: str = "research-bot",
    purpose: str | None = "digs through the warehouse",
    answers_in: tuple[AnsweringPlace, ...] = (AnsweringPlace(tier="channel", channel_id="900"),),
    unrouted_note: str | None = None,
    repo: RepoBinding | None = None,
    keys: tuple[KeyEntry, ...] = (),
    skills: tuple[SkillEntry, ...] = (),
    skills_listing_truncated: bool = False,
    mcp_servers: tuple[McpServerEntry, ...] = (),
) -> AgentDetails:
    """A validated AgentDetails for tests that are about rendering, not derivation."""
    return AgentDetails(
        ma_agent_id="ag_research",
        name=name,
        purpose=purpose,
        model_id="claude-opus-4-8",
        model_display_name="Opus 4.8",
        daimon_managed=False,
        created_by_is_workspace=True,
        created_at=_FIXED_CHECK,
        answers_in=answers_in,
        answers_here=bool(answers_in),
        repo=repo,
        skills=skills,
        skills_listing_truncated=skills_listing_truncated,
        mcp_servers=mcp_servers,
        keys=keys,
        applies_note=f"Changes to {name} apply from the next message to it.",
        unrouted_note=unrouted_note,
    )


def _state(
    details: AgentDetails,
    *,
    account_id: uuid.UUID,
    is_admin: bool = True,
    expanded_detail: DetailListName | None = None,
    roster_page: int = 0,
    roster_size: int = 1,
) -> PanelState:
    agent = _roster_agent(details.name)
    others = tuple(
        _roster_agent(f"other-{index}", ma_agent_id=f"ag_other_{index}")
        for index in range(roster_size - 1)
    )
    return PanelState(
        roster=[],
        selected=None,
        account_id=account_id,
        is_admin=is_admin,
        guild_id=2001,
        channel_id=900,
        channel_name="growth",
        deployment_default=DeploymentDefault(),
        roster_agents=(agent, *others),
        answering=agent,
        selected_agent=agent,
        roster_page=roster_page,
        expanded_detail=expanded_detail,
        details=details,
    )


def _make_runtime(sessionmaker: Any = None, *, settings: Any = None) -> DiscordRuntime:
    return DiscordRuntime(
        settings=settings if settings is not None else MagicMock(),
        anthropic=build_stub_anthropic(),
        sessionmaker=sessionmaker if sessionmaker is not None else MagicMock(),
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
        turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]  # never runs a turn
    )


def _mcp_settings() -> MagicMock:
    settings = MagicMock()
    settings.mcp.public_url = _PUBLIC_URL
    secret = MagicMock()
    secret.get_secret_value.return_value = _JWT_SECRET
    settings.mcp.jwt_secret = secret
    return settings


def _admin_interaction(user_id: int = 42) -> MagicMock:
    interaction = MagicMock()
    interaction.user = MagicMock(spec=discord.Member)
    interaction.user.id = user_id
    interaction.user.guild_permissions.administrator = True
    interaction.guild.owner_id = user_id + 1
    interaction.guild_id = 2001
    interaction.response.is_done = MagicMock(return_value=False)
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    interaction.followup.send = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    return interaction


def _member_interaction(user_id: int = 42) -> MagicMock:
    interaction = _admin_interaction(user_id)
    interaction.user.guild_permissions.administrator = False
    interaction.user.guild_permissions.manage_guild = False
    return interaction


def _walk(item: Any) -> list[Any]:
    """Every component in the tree, the way discord.py counts them."""
    found = [item]
    for child in getattr(item, "children", []) or []:
        found.extend(_walk(child))
    accessory = getattr(item, "accessory", None)
    if accessory is not None:
        found.extend(_walk(accessory))
    return found


def _container_text(container: discord.ui.Container[Any]) -> str:
    return "\n".join(
        str(item.content) for item in _walk(container) if isinstance(item, discord.ui.TextDisplay)
    )


def _find_button(view: discord.ui.LayoutView, label: str) -> discord.ui.Button[Any]:
    for item in _walk(view):
        if isinstance(item, discord.ui.Button) and item.label == label:
            return item
    raise AssertionError(f"No button labeled {label!r}")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_details_shows_the_model_display_name_and_every_section_when_populated(
    account_id: uuid.UUID,
) -> None:
    details = _details(
        repo=RepoBinding(
            repo_url="acme/warehouse",
            default_branch="main",
            access=RepoAccess(
                kind="connected", credential="per_agent_token", checked_at=_FIXED_CHECK
            ),
        ),
        keys=(KeyEntry(name="SNOWFLAKE_TOKEN", updated_at=_FIXED_CHECK),),
        skills=(SkillEntry(type="custom", skill_id="sk_1", title="Warehouse SQL", version="3"),),
        mcp_servers=(McpServerEntry(name="linear", url="https://linear.example/mcp"),),
    )
    container = build_details_container(
        _state(details, account_id=account_id),
        details,
        expanded_detail=None,
        is_admin=True,
        attribution="<@77>",
    )
    text = _container_text(container)

    assert "## research-bot" in text, "the plain header must name the agent"
    assert "digs through the warehouse" in text, "the purpose goes under the header"
    assert "**Model:** Opus 4.8" in text, "the model must show its display name, not its id"
    assert "<#900>" in text, "a channel it answers in renders as a channel mention"
    assert "[acme/warehouse](https://github.com/acme/warehouse)" in text, (
        "the working repo must be a link to the repo"
    )
    assert "`main`" in text, "the repo line must name the branch"
    assert "SNOWFLAKE_TOKEN" in text, "key names are shown"
    assert "Warehouse SQL" in text, "a resolved skill title is preferred over the skill id"
    assert "linear" in text, "an attached MCP server is listed by name"
    assert "Made by" not in text, "creator metadata is omitted from the compact card"
    assert text.index("**Model:**") < text.index("**Repository:**") < text.index("**Skills**"), (
        "the card reads model, then repository, then lists"
    )
    assert text.index("**Skills**") < text.index("**Connections**") < text.index("**Keys**"), (
        "lists render skills, connections, then keys"
    )
    assert "[linear](https://linear.example/mcp)" in text, "connection names link to their endpoint"


def test_details_shows_concrete_empty_states_when_nothing_is_attached(
    account_id: uuid.UUID,
) -> None:
    details = _details()
    container = build_details_container(
        _state(details, account_id=account_id),
        details,
        expanded_detail=None,
        is_admin=True,
        attribution=None,
    )
    text = _container_text(container)

    assert "**Repository:**" not in text, "an absent repo is omitted"
    assert "**Keys**" not in text, "an empty key list is omitted"
    assert "**Skills**" not in text, "an empty skill list is omitted"
    assert "**Connections**" not in text, "an empty connection list is omitted"
    assert "Made by" not in text, "no attribution line appears without a resolved creator"


@pytest.mark.parametrize("is_admin", [True, False])
def test_details_shows_the_unrouted_note_verbatim_in_the_readers_voice(
    account_id: uuid.UUID, is_admin: bool
) -> None:
    """A brand-new agent answers nowhere; core writes that sentence in the reader's voice."""
    details = build_agent_details(
        agent=ma_agent(id="ag_new", name="churn-explorer", description="looks at churn"),
        tenant_id=uuid.UUID(int=7),
        tenant=None,
        channels=[],
        default=DeploymentDefault(),
        resolved_here=None,
        binding=None,
        files=[],
        skill_titles={},
        skills_truncated=False,
        github=_github_facts(),
        public_mcp_url=None,
        is_admin=is_admin,
        channel_label="#growth",
    )
    assert details.unrouted_note is not None, "an agent routed nowhere must carry the note"

    container = build_details_container(
        _state(details, account_id=account_id, is_admin=is_admin),
        details,
        expanded_detail=None,
        is_admin=is_admin,
        attribution=None,
    )
    text = _container_text(container)

    assert details.unrouted_note in text, "the note is rendered exactly as core wrote it"
    assert "Not answering in any channel yet." in text, "the fact comes first"
    expected_lead = "Tell Daimon" if is_admin else "An admin can tell Daimon"
    assert f"{expected_lead}: make churn-explorer answer in #growth." in text, (
        "the next step is in the reader's own voice"
    )
    assert "answers in" not in text, "an unrouted agent must not claim to answer anywhere"


def _github_facts() -> Any:
    from daimon.core.agent_details import GitHubDeploymentFacts

    return GitHubDeploymentFacts(has_fallback_pat=False, app_configured=False)


@pytest.mark.parametrize(
    ("access", "expected"),
    [
        (
            RepoAccess(kind="connected", credential="per_agent_token", checked_at=_FIXED_CHECK),
            "via token\n-# Last checked",
        ),
        (
            RepoAccess(kind="connected", credential="deployment_public", checked_at=_FIXED_CHECK),
            "public repo\n-# Last checked",
        ),
        (
            RepoAccess(kind="checked", credential="github_app", checked_at=_FIXED_CHECK),
            "via GitHub App\n-# Last checked",
        ),
        (RepoAccess(kind="not_checked", credential="per_agent_token"), "not checked yet"),
        (
            RepoAccess(
                kind="needs_attention",
                credential="none",
                corrective="Add a token for acme/warehouse, or make it public.",
            ),
            "⚠️ needs attention — Add a token for acme/warehouse, or make it public.",
        ),
    ],
)
def test_repo_line_renders_the_recorded_access_state(
    account_id: uuid.UUID, access: RepoAccess, expected: str
) -> None:
    """Each RepoAccess kind gets its own sentence; none of them claims more than was recorded."""
    details = _details(
        repo=RepoBinding(repo_url="acme/warehouse", default_branch="main", access=access)
    )
    container = build_details_container(
        _state(details, account_id=account_id),
        details,
        expanded_detail=None,
        is_admin=True,
        attribution=None,
    )
    text = _container_text(container)

    assert expected in text, (
        f"RepoAccess {access.kind}/{access.credential} must render {expected!r}"
    )
    if access.kind != "connected":
        assert "connected" not in text, "only a working credential may read as connected"


def _details_with_list(name: DetailListName, count: int) -> AgentDetails:
    values = {
        "keys": {
            "keys": tuple(
                KeyEntry(name=f"KEY_{index:02d}", updated_at=_FIXED_CHECK) for index in range(count)
            )
        },
        "skills": {
            "skills": tuple(
                SkillEntry(
                    type="custom",
                    skill_id=f"skill-{index:02d}",
                    title=None,
                    version="1",
                )
                for index in range(count)
            )
        },
        "connections": {
            "mcp_servers": tuple(
                McpServerEntry(name=f"connection-{index:02d}", url=f"https://example.test/{index}")
                for index in range(count)
            )
        },
    }
    return _details(**values[name])  # pyright: ignore[reportArgumentType]  # test matrix selects one valid tuple field


@pytest.mark.parametrize("name", ["keys", "skills", "connections"])
@pytest.mark.parametrize("count", [0, 1, 5, 6, 7, 61])
def test_each_detail_list_collapses_and_expands_at_the_shared_limits(
    account_id: uuid.UUID, name: DetailListName, count: int
) -> None:
    details = _details_with_list(name, count)
    collapsed = build_details_container(
        _state(details, account_id=account_id),
        details,
        expanded_detail=None,
        is_admin=True,
        attribution=None,
    )
    collapsed_text = _container_text(collapsed)
    heading = {"keys": "Keys", "skills": "Skills", "connections": "Connections"}[name]
    assert (f"**{heading}**" in collapsed_text) is (count > 0), (
        "empty optional lists are omitted and populated lists retain their heading"
    )
    labels = [item.label for item in _walk(collapsed) if isinstance(item, discord.ui.Button)]
    assert (SHOW_MORE_LABEL in labels) is (count > DETAIL_LIST_COLLAPSED_COUNT), (
        "only a list with hidden rows offers expansion"
    )
    if count > DETAIL_LIST_COLLAPSED_COUNT:
        assert f"+{count - DETAIL_LIST_COLLAPSED_COUNT} more" in collapsed_text, (
            "the collapsed list reports every omitted row"
        )

        expanded = build_details_container(
            _state(details, account_id=account_id, expanded_detail=name),
            details,
            expanded_detail=name,
            is_admin=True,
            attribution=None,
        )
        expanded_text = _container_text(expanded)
        expanded_labels = [
            item.label for item in _walk(expanded) if isinstance(item, discord.ui.Button)
        ]
        assert SHOW_FEWER_LABEL in expanded_labels, "the expanded list can be collapsed"
        if count > 60:
            assert f"+{count - 60} more" in expanded_text, (
                "the expanded view reports rows beyond its hard cap"
            )


def test_details_renders_key_names_and_never_a_value(account_id: uuid.UUID) -> None:
    """The only thing a key can contribute to this card is its name."""
    keys = (
        KeyEntry(name="SNOWFLAKE_TOKEN", updated_at=_FIXED_CHECK),
        KeyEntry(name="STRIPE_KEY", updated_at=_FIXED_CHECK),
    )
    assert not hasattr(keys[0], "value"), "KeyEntry must never grow a value field"
    assert not hasattr(keys[0], "content"), "KeyEntry must never grow a content field"
    assert set(KeyEntry.model_fields) == {
        "name",
        "created_by_account_id",
        "last_set_by_account_id",
        "updated_at",
    }, "KeyEntry carries names and attribution only"

    details = _details(keys=keys)
    container = build_details_container(
        _state(details, account_id=account_id),
        details,
        expanded_detail=None,
        is_admin=True,
        attribution=None,
    )
    text = _container_text(container)
    for key in keys:
        assert key.name in text, f"{key.name} must be listed"
    assert shared_keys_sentence("research-bot") in text, (
        "the list must say who a stored key reaches"
    )


def test_truncated_skill_listing_says_so(account_id: uuid.UUID) -> None:
    details = _details(
        skills=(SkillEntry(type="custom", skill_id="sk_1", title=None, version="1"),),
        skills_listing_truncated=True,
    )
    container = build_details_container(
        _state(details, account_id=account_id),
        details,
        expanded_detail=None,
        is_admin=True,
        attribution=None,
    )
    text = _container_text(container)
    assert "sk_1" in text, "a skill with no resolved title falls back to its id"
    assert "-# Some skill names may be missing." in text, (
        "a truncated listing must admit it is incomplete"
    )


def test_empty_truncated_skill_listing_keeps_its_warning(account_id: uuid.UUID) -> None:
    details = _details(skills=(), skills_listing_truncated=True)
    container = build_details_container(
        _state(details, account_id=account_id),
        details,
        expanded_detail=None,
        is_admin=True,
        attribution=None,
    )

    text = _container_text(container)
    assert "**Skills**" not in text, "the empty optional list remains omitted"
    assert "-# Some skill names may be missing." in text, (
        "upstream truncation remains visible even when no accessible prefix was returned"
    )


def test_connection_link_escapes_name_and_url_markup(account_id: uuid.UUID) -> None:
    details = _details(
        mcp_servers=(McpServerEntry(name="search [private]", url="https://example.test/a_(b)"),)
    )
    container = build_details_container(
        _state(details, account_id=account_id),
        details,
        expanded_detail=None,
        is_admin=True,
        attribution=None,
    )

    assert "[search \\[private\\]](https://example.test/a_%28b%29)" in _container_text(container), (
        "connection links must not let names or URLs break Discord markdown"
    )


def test_many_routing_places_render_one_per_line_with_an_omission_count(
    account_id: uuid.UUID,
) -> None:
    details = _details(
        answers_in=tuple(
            AnsweringPlace(tier="channel", channel_id=str(10**17 + index)) for index in range(100)
        )
    )
    container = build_details_container(
        _state(details, account_id=account_id),
        details,
        expanded_detail=None,
        is_admin=True,
        attribution=None,
    )
    text = _container_text(container)

    assert "**Answers in:**\n<#100000000000000000>\n<#100000000000000001>" in text, (
        "multiple routing places use one complete place per line"
    )
    assert " more" in text, "bounded routing text reports how many complete places were omitted"


def test_long_fixed_fields_leave_room_for_lists_and_security_notes(
    account_id: uuid.UUID,
) -> None:
    details = _details(
        purpose="p" * 2000,
        repo=RepoBinding(
            repo_url=f"owner/{'r' * 300}",
            default_branch="b" * 300,
            access=RepoAccess(
                kind="needs_attention",
                credential="none",
                corrective="c" * 1000,
            ),
        ),
        keys=tuple(
            KeyEntry(name=f"KEY_{index:02d}_{'x' * 30}", updated_at=_FIXED_CHECK)
            for index in range(61)
        ),
    )
    view = DetailsView(
        _state(details, account_id=account_id, expanded_detail="keys"),
        runtime=_make_runtime(),
        allowed_user_id=42,
    )
    text = _container_text(
        next(item for item in view.children if isinstance(item, discord.ui.Container))
    )

    assert "…" in text, "overlong optional purpose text marks its truncation"
    assert "c" * 1000 in text, "the full corrective access text is preserved"
    assert shared_keys_sentence(details.name) in text, "the shared-key security note is preserved"
    assert view.content_length() <= LAYOUT_TEXT_BUDGET, "all visible text stays in Discord's budget"


async def test_expanding_a_detail_list_replaces_the_previous_expansion(
    account_id: uuid.UUID,
) -> None:
    details = _details_with_list("keys", 7).model_copy(
        update={
            "skills": _details_with_list("skills", 7).skills,
            "mcp_servers": _details_with_list("connections", 7).mcp_servers,
        }
    )
    state = _state(details, account_id=account_id, expanded_detail="keys")
    view = DetailsView(state, runtime=_make_runtime(), allowed_user_id=42)

    await view._on_toggle_detail(  # pyright: ignore[reportPrivateUsage]  # callback under test
        _admin_interaction(), name="skills"
    )

    assert state.expanded_detail == "skills", "opening Skills closes the previously-open Keys list"


def test_full_details_stays_within_the_component_budget(account_id: uuid.UUID) -> None:
    details = _details(
        repo=RepoBinding(
            repo_url="acme/warehouse",
            default_branch="main",
            access=RepoAccess(
                kind="connected", credential="per_agent_token", checked_at=_FIXED_CHECK
            ),
        ),
        keys=tuple(
            KeyEntry(name=f"KEY_{index:02d}_{'x' * 30}", updated_at=_FIXED_CHECK)
            for index in range(75)
        ),
        skills=tuple(
            SkillEntry(
                type="custom",
                skill_id=f"sk_{index}",
                title=f"Skill {index:02d} {'x' * 50}",
                version="1",
            )
            for index in range(75)
        ),
        mcp_servers=tuple(
            McpServerEntry(
                name=f"server-{index:02d}-{'x' * 30}",
                url=f"https://example.test/{index}/{'y' * 30}",
            )
            for index in range(75)
        ),
    )
    view = DetailsView(
        _state(details, account_id=account_id, expanded_detail="connections"),
        runtime=_make_runtime(),
        allowed_user_id=42,
    )
    components = _walk(view)
    assert len(components) - 1 <= LAYOUT_COMPONENT_BUDGET, (
        f"the widest Details must fit inside {LAYOUT_COMPONENT_BUDGET} components; "
        f"counted {len(components) - 1}"
    )
    text_chars = sum(
        len(str(item.content)) for item in components if isinstance(item, discord.ui.TextDisplay)
    )
    assert text_chars <= LAYOUT_TEXT_BUDGET, (
        f"the widest Details must fit inside {LAYOUT_TEXT_BUDGET} display characters"
    )


# ---------------------------------------------------------------------------
# Navigation and actions
# ---------------------------------------------------------------------------


async def test_back_returns_to_the_roster_with_its_page_and_selection_intact(
    account_id: uuid.UUID,
) -> None:
    from daimon.adapters.discord.agent_setup.roster_view import RosterView

    details = _details()
    # Nine agents so page 1 exists; `paginate` clamps a page the roster cannot
    # fill, which would hide a Back that silently reset the page.
    state = _state(details, account_id=account_id, roster_page=1, roster_size=9)
    selected_before = state.selected_agent
    view = DetailsView(state, runtime=_make_runtime(), allowed_user_id=42)
    interaction = _admin_interaction()

    await _find_button(view, "◀ Back").callback(interaction)

    interaction.response.edit_message.assert_called_once()
    swapped = interaction.response.edit_message.call_args.kwargs["view"]
    assert isinstance(swapped, RosterView), "Back must return to the roster screen"
    assert swapped.state is state, "Back must reuse the panel's state, not refetch it"
    assert state.selected_agent is selected_before, "Back must not clear the selection"
    assert state.roster_page == 1, "Back must not reset the roster page"


async def test_setup_targets_the_agent_this_card_describes(account_id: uuid.UUID) -> None:
    import daimon.adapters.discord.agent_setup.details_view as details_view_mod

    captured: dict[str, Any] = {}

    async def _spy_open(_interaction: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    details = _details(name="churn-explorer")
    state = _state(details, account_id=account_id)
    view = DetailsView(state, runtime=_make_runtime(), allowed_user_id=42)
    interaction = _admin_interaction()

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(details_view_mod, "open_setup_conversation", _spy_open)
        await _find_button(view, setup_target_label("churn-explorer")).callback(interaction)

    assert captured["target"] is state.selected_agent, (
        "Manage must carry this card's agent as its target"
    )
    assert captured["target"] is not None and captured["target"].name == "churn-explorer"
    interaction.response.defer.assert_called_once()


async def test_rendered_details_actions_keep_the_card_agent_after_selection_changes(
    account_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import daimon.adapters.discord.agent_setup.details_view as details_view_mod

    setup_targets: list[RosterAgent | None] = []
    coding_targets: list[RosterAgent | None] = []

    async def _capture_setup(_interaction: Any, **kwargs: Any) -> None:
        setup_targets.append(kwargs["target"])

    async def _capture_coding_tools(
        _interaction: Any, *, agent: RosterAgent | None = None, **_kwargs: Any
    ) -> None:
        coding_targets.append(agent)

    monkeypatch.setattr(details_view_mod, "open_setup_conversation", _capture_setup)
    monkeypatch.setattr(details_view_mod, "send_coding_tools_access", _capture_coding_tools)
    details = _details(name="card-agent")
    state = _state(details, account_id=account_id)
    view = DetailsView(state, runtime=_make_runtime(), allowed_user_id=42)
    state.select_agent(_roster_agent("later-selection", ma_agent_id="ag_later"))

    await _find_button(view, setup_target_label("card-agent")).callback(_admin_interaction())
    await _find_button(view, "🧰 Use from your coding tools").callback(_admin_interaction())

    assert [agent.name if agent else None for agent in setup_targets] == ["card-agent"], (
        "the setup target stays bound to the Details card"
    )
    assert [agent.name if agent else None for agent in coding_targets] == ["card-agent"], (
        "coding-tool access stays bound to the Details card"
    )


async def test_coding_tools_refuses_a_demoted_admin_without_minting(
    account_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The view's is_admin is a snapshot; the click reads the live interaction."""
    import daimon.adapters.discord.agent_setup.details_view as details_view_mod

    async def _unexpected(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("a non-admin caller must never reach the minting handler")

    monkeypatch.setattr(details_view_mod, "send_coding_tools_access", _unexpected)

    details = _details()
    state = _state(details, account_id=account_id, is_admin=True)
    view = DetailsView(state, runtime=_make_runtime(), allowed_user_id=42)
    interaction = _member_interaction()

    await _find_button(view, "🧰 Use from your coding tools").callback(interaction)

    interaction.response.send_message.assert_called_once()
    message = interaction.response.send_message.call_args.args[0]
    assert "Manage Server" in message, "the refusal must name the permission the caller lacks"
    assert "Bearer" not in message, "a refused caller must receive no token material"
    interaction.response.defer.assert_not_called()


async def test_coding_tools_gives_a_member_the_explanatory_refusal(
    account_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The button is visible to members on purpose, so its refusal has to explain itself."""
    import daimon.adapters.discord.agent_setup.details_view as details_view_mod

    async def _unexpected(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("a member's click must mint nothing")

    monkeypatch.setattr(details_view_mod, "send_coding_tools_access", _unexpected)

    details = _details(name="churn-explorer")
    state = _state(details, account_id=account_id, is_admin=False)
    view = DetailsView(state, runtime=_make_runtime(), allowed_user_id=42)
    interaction = _member_interaction()

    button = _find_button(view, "🧰 Use from your coding tools")
    assert button.disabled is False, "members see the button; the refusal explains the gate"
    await button.callback(interaction)

    kwargs = interaction.response.send_message.call_args.kwargs
    message = interaction.response.send_message.call_args.args[0]
    assert message == coding_tools_refusal("churn-explorer"), (
        "a member gets the explanatory refusal naming the agent and the way round it"
    )
    assert "Ask an admin to open Details" in message, "the refusal must name the way forward"
    assert kwargs.get("ephemeral") is True, "the refusal is ephemeral"


async def test_coding_tools_mints_for_a_live_admin_in_a_separate_ephemeral(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    account_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> None:
    """An admin's click mints a real token and delivers it on its own message.

    The panel's own ephemeral is untouched: the token block has to be plain
    copyable content, which a Components V2 card cannot carry.
    """
    await _seed_tenant_and_account(
        db_session, tenant_id=tenant_id, account_id=account_id, external_id="guild-details-mint"
    )
    agent = ma_agent(id="ag_research", name="research-bot", tenant_id=tenant_id)

    async def _fake_find(*_args: Any, **_kwargs: Any) -> BetaManagedAgentsAgent:
        return agent

    monkeypatch.setattr(mcp_access_mod, "find_agent_by_daimon_tag", _fake_find)
    monkeypatch.setattr(
        mcp_access_mod, "resolve_tenant_for_panel", AsyncMock(return_value=tenant_id)
    )

    details = _details()
    state = _state(details, account_id=account_id)
    runtime = _make_runtime(db_session_factory, settings=_mcp_settings())
    view = DetailsView(state, runtime=runtime, allowed_user_id=42)
    interaction = _admin_interaction()

    await _find_button(view, "🧰 Use from your coding tools").callback(interaction)

    interaction.response.send_message.assert_called_once()
    kwargs = interaction.response.send_message.call_args.kwargs
    assert kwargs["ephemeral"] is True, "token material is ephemeral"
    content: str = kwargs["content"]
    assert _PUBLIC_URL in content, "the config block must carry the deployment's MCP url"
    assert "claude mcp add" in content, "the one-liner must be offered first"
    interaction.response.edit_message.assert_not_called()
    interaction.edit_original_response.assert_not_called()

    token = content[content.index("Bearer ") + len("Bearer ") :].split('"')[0].strip()
    claims = pyjwt.decode(token, _JWT_SECRET.encode(), algorithms=["HS256"])
    row = await get_mcp_token(db_session, jti=uuid.UUID(claims["jti"]))
    assert row is not None, "an admin's click must write a real mcp_tokens row"
    assert row.account_id == account_id, "the token is attributed to the clicker's own account"


async def _seed_tenant_and_account(
    db_session: AsyncSession, *, tenant_id: uuid.UUID, account_id: uuid.UUID, external_id: str
) -> tuple[TenantRow, AccountRow]:
    tenant = await make_tenant(
        db_session, platform="discord", workspace_id=external_id, id=tenant_id
    )
    account = await make_account(db_session, tenant=tenant, id=account_id)
    return tenant, account
