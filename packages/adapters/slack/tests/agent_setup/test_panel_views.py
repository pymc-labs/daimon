"""Block Kit builders for the read-only setup panel.

Pure builders, so every test here is "given these facts, what does the modal
say". The facts are built through the real core assemblers — `build_agent_details`,
`build_answering_map`, the `Roster` constructors — so a change to what the core
records shows up as a failing rendering test rather than as a view quietly
describing a state that no longer exists.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from daimon.adapters.slack.agent_setup.panel_views import (
    ACTION_CODING_TOOLS,
    ACTION_EXPAND_CONNECTIONS,
    ACTION_EXPAND_KEYS,
    ACTION_EXPAND_SKILLS,
    ACTION_NEW,
    ACTION_PAGE_NEXT,
    ACTION_ROUTING,
    CALLBACK_NEW_AGENT,
    LEGACY_ACTION_IDS,
    build_agents_view,
    build_creating_view,
    build_details_view,
    build_new_agent_form,
    build_routing_view,
)
from daimon.adapters.slack.agent_setup.state import PANEL_PAGE_SIZE, PanelMetadata
from daimon.adapters.slack.agent_setup.views import build_l3_new_agent_form
from daimon.adapters.slack.modal_limits import (
    MAX_BLOCKS_PER_VIEW,
    MAX_PRIVATE_METADATA_CHARS,
    MAX_TITLE_CHARS,
)
from daimon.core.agent_details import AgentDetails, GitHubDeploymentFacts, build_agent_details
from daimon.core.answering_map import AnsweringMap, build_answering_map
from daimon.core.github_repo_auth import RepoAccessKind
from daimon.core.models_catalog import list_model_choices
from daimon.core.roster import Page, Roster, RosterAgent, paginate
from daimon.core.routing_facts import PRECEDENCE_LINE, UNROUTED_LINE
from daimon.core.scope import ChannelConfigRow, DeploymentDefault, ResolvedConfig, TenantConfigRow
from daimon.core.stores.domain import AgentFileRow, AgentRepoBindingRow
from daimon.testing import ma_agent

_TEAM_ID = "T_PANEL"
_CHANNEL_ID = "C0HERE1111"
_OTHER_CHANNEL_ID = "C0THERE222"
_TENANT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_MAKER = uuid.UUID("22222222-2222-2222-2222-222222222222")
_MOMENT = datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Builders for the inputs
# ---------------------------------------------------------------------------


def _meta(**overrides: Any) -> PanelMetadata:
    kwargs: dict[str, Any] = {
        "team_id": _TEAM_ID,
        "channel_id": _CHANNEL_ID,
        "view": "agents",
    }
    kwargs.update(overrides)
    return PanelMetadata(**kwargs)


def _roster_agent(name: str, **overrides: Any) -> RosterAgent:
    kwargs: dict[str, Any] = {
        "name": name,
        "ma_agent_id": f"ag_{name}",
        "model_id": "claude-sonnet-4-6",
        "is_built_in": False,
    }
    kwargs.update(overrides)
    return RosterAgent(**kwargs)


def _roster(*agents: RosterAgent, answering: RosterAgent | None = None) -> Roster:
    return Roster(rows=agents, answering=answering)


def _page(roster: Roster, *, page: int = 0) -> Page[RosterAgent]:
    return paginate(roster.rows, page=page, page_size=PANEL_PAGE_SIZE)


def _agent_file(key: str) -> AgentFileRow:
    return AgentFileRow(
        tenant_id=_TENANT_ID,
        agent_id=uuid.uuid4(),
        key=key,
        content="never-rendered",
        created_by_account_id=_MAKER,
        last_set_by_account_id=_MAKER,
        created_at=_MOMENT,
        updated_at=_MOMENT,
    )


def _binding(**overrides: Any) -> AgentRepoBindingRow:
    kwargs: dict[str, Any] = {
        "tenant_id": _TENANT_ID,
        "agent_id": uuid.uuid4(),
        "repo_url": "https://github.com/pymc-labs/churn-models",
        "default_branch": "main",
        "ma_secret_ref": "inline-pat:abc",
        "proof_kind": "pat",
        "proof_at": _MOMENT,
        "proof_account_id": _MAKER,
        "created_at": _MOMENT,
        "updated_at": _MOMENT,
    }
    kwargs.update(overrides)
    return AgentRepoBindingRow(**kwargs)


def _details(
    *,
    name: str = "research-bot",
    channels: list[ChannelConfigRow] | None = None,
    tenant: TenantConfigRow | None = None,
    files: list[AgentFileRow] | None = None,
    binding: AgentRepoBindingRow | None = None,
    skills: list[dict[str, Any]] | None = None,
    skills_truncated: bool = False,
    mcp_servers: list[dict[str, Any]] | None = None,
    purpose: str | None = None,
    is_admin: bool = True,
    github: GitHubDeploymentFacts | None = None,
    default: DeploymentDefault | None = None,
) -> AgentDetails:
    resolved_channels = (
        channels
        if channels is not None
        else [ChannelConfigRow(tenant_id=_TENANT_ID, channel_id=_CHANNEL_ID, agent_name=name)]
    )
    return build_agent_details(
        agent=ma_agent(
            id=f"ag_{name}",
            name=name,
            tenant_id=_TENANT_ID,
            description=purpose,
            skills=skills or [],
            mcp_servers=mcp_servers or [],
        ),
        tenant_id=_TENANT_ID,
        tenant=tenant,
        channels=resolved_channels,
        default=default or DeploymentDefault(),
        resolved_here=ResolvedConfig(agent_name=name, agent_name_tier="channel"),
        binding=binding,
        files=files or [],
        skill_titles={},
        skills_truncated=skills_truncated,
        github=github or GitHubDeploymentFacts(has_fallback_pat=True, app_configured=True),
        public_mcp_url="https://mcp.example.com/mcp",
        is_admin=is_admin,
        channel_label=f"<#{_CHANNEL_ID}>",
    )


def _answering_map(
    *,
    channels: list[ChannelConfigRow] | None = None,
    tenant: TenantConfigRow | None = None,
    deployment_default: str | None = "daimon",
) -> AnsweringMap:
    return build_answering_map(
        tenant=tenant,
        channels=channels or [],
        default=DeploymentDefault(agent_name=deployment_default),
        setup_threads=[],
        setup_threads_truncated=False,
    )


# ---------------------------------------------------------------------------
# View readers
# ---------------------------------------------------------------------------


def _blocks(view: dict[str, Any]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = view["blocks"]
    return blocks


def _texts(view: dict[str, Any]) -> list[str]:
    """Every rendered string in the view, section, field and context alike."""
    found: list[str] = []
    for block in _blocks(view):
        text = block.get("text")
        if isinstance(text, dict):
            found.append(str(text.get("text", "")))
        for field in block.get("fields", []) or []:
            found.append(str(field.get("text", "")))
        for element in block.get("elements", []) or []:
            if element.get("type") in {"mrkdwn", "plain_text"}:
                found.append(str(element.get("text", "")))
    return found


def _joined(view: dict[str, Any]) -> str:
    return "\n".join(_texts(view))


def _action_ids(view: dict[str, Any]) -> list[str]:
    found: list[str] = []
    for block in _blocks(view):
        accessory = block.get("accessory")
        if isinstance(accessory, dict) and "action_id" in accessory:
            found.append(str(accessory["action_id"]))
        for element in block.get("elements", []) or []:
            if isinstance(element, dict) and "action_id" in element:
                found.append(str(element["action_id"]))
        element = block.get("element")
        if isinstance(element, dict) and "action_id" in element:
            found.append(str(element["action_id"]))
    return found


def _button_labels(view: dict[str, Any]) -> list[str]:
    labels: list[str] = []
    for block in _blocks(view):
        candidates = [block.get("accessory"), *(block.get("elements", []) or [])]
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get("type") == "button":
                labels.append(str(candidate.get("text", {}).get("text", "")))
    return labels


def _all_views() -> list[dict[str, Any]]:
    """One of each panel view, built from the same small world."""
    answering = _roster_agent("research-bot", created_by_account_id=_MAKER)
    other = _roster_agent("churn-explorer")
    roster = _roster(answering, other, answering=answering)
    meta = _meta()
    return [
        build_agents_view(
            roster,
            page=_page(roster),
            meta=meta,
            is_admin=True,
            attributions={_MAKER: "<@U1>"},
            channel_id=_CHANNEL_ID,
        ),
        build_details_view(
            _details(),
            meta=meta,
            is_admin=True,
            coding_tools_available=True,
            channel_id=_CHANNEL_ID,
            attribution="<@U1>",
        ),
        build_routing_view(
            _answering_map(),
            page=paginate((), page=0, page_size=PANEL_PAGE_SIZE),
            meta=meta,
            is_admin=True,
            attributions={},
            setup_links=[],
            channel_id=_CHANNEL_ID,
            unrouted_agent_name=None,
        ),
        build_creating_view(agent_name="new-agent", meta=meta),
    ]


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------


def test_agents_view_when_agent_answers_here_lists_it_first_then_stable_name_order() -> None:
    answering = _roster_agent("zebra-bot")
    roster = _roster(
        answering,
        _roster_agent("alpha-bot"),
        _roster_agent("beta-bot"),
        answering=answering,
    )
    view = build_agents_view(
        roster,
        page=_page(roster),
        meta=_meta(),
        is_admin=True,
        attributions={},
        channel_id=_CHANNEL_ID,
    )
    headings = [text for text in _texts(view) if text.startswith("*")]
    assert headings[0] == f"*Agents in <#{_CHANNEL_ID}>*", "the roster names its channel"
    assert headings[1] == f"*zebra-bot*\nAnswers in <#{_CHANNEL_ID}>", (
        "the agent answering where the reader stands comes first and says so"
    )
    assert headings[2:] == [
        "*alpha-bot*\nRouting unavailable",
        "*beta-bot*\nRouting unavailable",
    ], "the remaining agents keep the roster's own order"
    details_values = [
        block["accessory"]["value"]
        for block in _blocks(view)
        if isinstance(block.get("accessory"), dict)
    ]
    assert details_values == ["zebra-bot", "alpha-bot", "beta-bot"], (
        "every row carries a Details button, including the answering one"
    )


def test_agents_view_when_member_has_same_blocks_as_admin() -> None:
    answering = _roster_agent("research-bot", created_by_account_id=_MAKER)
    roster = _roster(answering, _roster_agent("churn-explorer"), answering=answering)
    shared: dict[str, Any] = {
        "page": _page(roster),
        "meta": _meta(),
        "attributions": {_MAKER: "<@U1>"},
        "channel_id": _CHANNEL_ID,
    }
    admin_view = build_agents_view(roster, is_admin=True, **shared)
    member_view = build_agents_view(roster, is_admin=False, **shared)
    assert admin_view == member_view, (
        "the roster is orientation, not permission — both roles see the same view"
    )


def test_agents_view_when_roster_empty_shows_empty_state_and_setup_action() -> None:
    roster = _roster()
    view = build_agents_view(
        roster,
        page=_page(roster),
        meta=_meta(),
        is_admin=False,
        attributions={},
        channel_id=_CHANNEL_ID,
    )
    assert "No agent answers here yet. Ask Daimon to help set one up." in _joined(view), (
        "an empty roster states the empty case in the shared copy"
    )
    assert "agent_setup__conversation" in _action_ids(view), (
        "setup stays reachable with no agent to select"
    )
    assert ACTION_NEW in _action_ids(view), "creating the first agent stays reachable"
    assert ACTION_ROUTING in _action_ids(view), "the routing view stays reachable"


def test_agents_view_when_over_page_size_renders_pager_and_stays_under_100_blocks() -> None:
    agents = [_roster_agent(f"agent-{index:03d}") for index in range(45)]
    roster = _roster(*agents)
    page = _page(roster)
    view = build_agents_view(
        roster,
        page=page,
        meta=_meta(),
        is_admin=True,
        attributions={},
        channel_id=_CHANNEL_ID,
    )
    assert page.page_count == 3, "45 agents at a page size of 20 is three pages"
    assert "Page 1 of 3" in _joined(view), "a paged roster says where the reader is"
    assert ACTION_PAGE_NEXT in _action_ids(view), "a first page offers Next"
    assert len(_blocks(view)) <= MAX_BLOCKS_PER_VIEW, (
        "a full page plus its chrome must fit Slack's block budget"
    )


def test_agents_view_emits_no_legacy_action_ids() -> None:
    for view in _all_views():
        offenders = sorted(set(_action_ids(view)) & LEGACY_ACTION_IDS)
        assert not offenders, (
            f"the read-only panel must not emit the editor's actions; found {offenders}"
        )


def test_agents_view_never_renders_made_by_for_workspace_stamp() -> None:
    stamped = _roster_agent("seeded-bot", created_by_account_id=_MAKER, is_built_in=True)
    roster = _roster(stamped)
    view = build_agents_view(
        roster,
        page=_page(roster),
        meta=_meta(),
        is_admin=True,
        attributions={},
        channel_id=_CHANNEL_ID,
    )
    rendered = _joined(view)
    assert "made by" not in rendered, (
        "an account with no resolved person behind it — the workspace stamp — gets no attribution"
    )
    assert "built in" not in rendered, "provenance does not compete with routing status"


def test_agents_view_when_routed_names_given_distinguishes_unrouted_from_elsewhere() -> None:
    answering = _roster_agent("research-bot")
    roster = _roster(
        answering,
        _roster_agent("churn-explorer"),
        _roster_agent("growth-bot"),
        answering=answering,
    )
    view = build_agents_view(
        roster,
        page=_page(roster),
        meta=_meta(),
        is_admin=True,
        attributions={},
        channel_id=_CHANNEL_ID,
        routed_agent_names={"growth-bot"},
    )
    rendered = _joined(view)
    assert "Not assigned" in rendered, "an agent nothing routes to says so"
    assert "Answers in another channel" in rendered, (
        "an agent routed somewhere else says that instead"
    )


# ---------------------------------------------------------------------------
# Details
# ---------------------------------------------------------------------------


def test_details_view_when_name_exceeds_24_chars_truncates_title_and_keeps_full_name_in_body() -> (
    None
):
    long_name = "a-very-long-agent-name-that-overflows"
    view = build_details_view(
        _details(name=long_name),
        meta=_meta(),
        is_admin=True,
        coding_tools_available=True,
        channel_id=_CHANNEL_ID,
        attribution=None,
    )
    assert len(view["title"]["text"]) == MAX_TITLE_CHARS, "the title is cut to Slack's cap"
    assert view["title"]["text"] != long_name, "the title cannot hold the whole name"
    assert f"*{long_name}*" in _joined(view), (
        "a cut title must be repaired by the full name in the body"
    )


def test_details_view_when_short_name_does_not_repeat_it_in_the_body() -> None:
    view = build_details_view(
        _details(name="research-bot"),
        meta=_meta(),
        is_admin=True,
        coding_tools_available=True,
        channel_id=_CHANNEL_ID,
        attribution=None,
    )
    assert view["title"]["text"] == "research-bot", "a short name fits the title untouched"
    assert "*research-bot*" not in _joined(view), (
        "a name already in the title is not repeated as a heading"
    )


@pytest.mark.parametrize("is_admin", [True, False])
def test_details_view_when_unrouted_shows_note(is_admin: bool) -> None:
    details = _details(channels=[], is_admin=is_admin)
    view = build_details_view(
        details,
        meta=_meta(),
        is_admin=is_admin,
        coding_tools_available=True,
        channel_id=_CHANNEL_ID,
        attribution=None,
    )
    rendered = _joined(view)
    assert details.unrouted_note is not None, "an agent nothing routes to carries the note"
    assert details.unrouted_note in rendered, "the note is rendered as the core wrote it"
    expected_lead = "Tell Daimon:" if is_admin else "An admin can tell Daimon:"
    assert expected_lead in rendered, "the next step is phrased in the reader's own voice"
    assert f"<#{_CHANNEL_ID}>" in rendered, "the request names the channel as a live mention"
    assert rendered.count(UNROUTED_LINE) == 1, (
        "the fact is stated once, in the body — the header says nothing about routing"
    )


def test_details_view_orders_identity_then_configuration_and_collections() -> None:
    """Details reads top to bottom the way it does on the other platform."""
    view = build_details_view(
        _details(
            purpose="Finds evidence and analyzes data.",
            files=[_agent_file("TOGGL_TOKEN")],
            binding=_binding(),
            skills=[{"type": "custom", "skill_id": "explore", "version": "1"}],
            mcp_servers=[{"type": "url", "name": "example", "url": "https://example.com/mcp"}],
        ),
        meta=_meta(),
        is_admin=True,
        coding_tools_available=True,
        channel_id=_CHANNEL_ID,
        attribution=None,
    )
    rendered = _joined(view)
    headings = [
        "Finds evidence and analyzes data.",
        "*Answers in:*",
        "*Model:*",
        "*Repository:*",
        "*Branch:*",
        "*Skills*",
        "*Connections*",
        "*Keys*",
    ]
    positions = [rendered.index(heading) for heading in headings]
    assert positions == sorted(positions), (
        f"Details must read {' → '.join(headings)}, not {rendered}"
    )


def test_details_view_never_renders_key_values() -> None:
    files = [_agent_file("TOGGL_TOKEN"), _agent_file("XERO_API_KEY")]
    view = build_details_view(
        _details(files=files),
        meta=_meta(),
        is_admin=True,
        coding_tools_available=True,
        channel_id=_CHANNEL_ID,
        attribution=None,
    )
    serialized = json.dumps(view)
    assert "TOGGL_TOKEN" in serialized, "key names are shown so people know what to ask for"
    assert "never-rendered" not in serialized, "a key's value must never reach a view"
    assert "Anyone who talks to research-bot can use these." in _joined(view), (
        "the shared-use sentence sits beside the key list"
    )


def test_details_view_renders_multiple_answering_places_one_per_line() -> None:
    view = build_details_view(
        _details(
            channels=[
                ChannelConfigRow(
                    tenant_id=_TENANT_ID, channel_id=_CHANNEL_ID, agent_name="research-bot"
                ),
                ChannelConfigRow(
                    tenant_id=_TENANT_ID,
                    channel_id=_OTHER_CHANNEL_ID,
                    agent_name="research-bot",
                ),
            ]
        ),
        meta=_meta(),
        is_admin=True,
        coding_tools_available=True,
        channel_id=_CHANNEL_ID,
        attribution=None,
    )
    assert f"*Answers in*\n<#{_CHANNEL_ID}>\n<#{_OTHER_CHANNEL_ID}>" in _joined(view), (
        "multiple routing values remain a stable vertical list"
    )


def test_details_view_sanitizes_connection_names_inside_slack_links() -> None:
    view = build_details_view(
        _details(
            mcp_servers=[
                {
                    "type": "url",
                    "name": "Example|service\nspoofed",
                    "url": "https://example.com/mcp",
                }
            ]
        ),
        meta=_meta(),
        is_admin=True,
        coding_tools_available=True,
        channel_id=_CHANNEL_ID,
        attribution=None,
    )
    assert "<https://example.com/mcp|Example¦service spoofed>" in _joined(view), (
        "a service name cannot terminate its link label or inject another row"
    )


def test_details_view_when_many_keys_collapses_and_offers_expand() -> None:
    files = [_agent_file(f"KEY_{index:02d}") for index in range(12)]
    collapsed = build_details_view(
        _details(files=files),
        meta=_meta(),
        is_admin=True,
        coding_tools_available=True,
        channel_id=_CHANNEL_ID,
        attribution=None,
    )
    rendered = _joined(collapsed)
    assert "KEY_05" in rendered, "the first six key names are shown"
    assert "KEY_06" not in rendered, "the seventh and beyond collapse behind the expander"
    assert ACTION_EXPAND_KEYS in _action_ids(collapsed), "a long key list offers expansion"
    assert "Show more" in _button_labels(collapsed), "the expander says what it does"

    expanded = build_details_view(
        _details(files=files),
        meta=_meta(expanded="keys"),
        is_admin=True,
        coding_tools_available=True,
        channel_id=_CHANNEL_ID,
        attribution=None,
    )
    assert "KEY_11" in _joined(expanded), "an expanded list shows the rest"
    assert "Show fewer" in _button_labels(expanded), "an expanded list offers to collapse again"


@pytest.mark.parametrize("count", [0, 1, 5, 6, 7, 61])
@pytest.mark.parametrize(
    ("kind", "heading", "action_id"),
    [
        ("skills", "*Skills*", ACTION_EXPAND_SKILLS),
        ("connections", "*Connections*", ACTION_EXPAND_CONNECTIONS),
        ("keys", "*Keys*", ACTION_EXPAND_KEYS),
    ],
)
def test_details_view_bounds_each_collection_consistently(
    count: int, kind: str, heading: str, action_id: str
) -> None:
    names = [f"ITEM_{index:02d}" for index in range(count)]
    details = _details(
        files=[_agent_file(name) for name in names] if kind == "keys" else None,
        skills=(
            [{"type": "custom", "skill_id": name, "version": "1"} for name in names]
            if kind == "skills"
            else None
        ),
        mcp_servers=(
            [{"type": "url", "name": name, "url": f"https://example.com/{name}"} for name in names]
            if kind == "connections"
            else None
        ),
    )
    collapsed = build_details_view(
        details,
        meta=_meta(),
        is_admin=True,
        coding_tools_available=True,
        channel_id=_CHANNEL_ID,
        attribution=None,
    )
    collapsed_text = _joined(collapsed)
    assert (heading in collapsed_text) is (count > 0), "empty optional lists are omitted"
    assert (action_id in _action_ids(collapsed)) is (count > 6), (
        "only a list with hidden entries needs Show more"
    )
    if count > 6:
        assert "ITEM_05" in collapsed_text, "the sixth complete item remains visible"
        assert "ITEM_06" not in collapsed_text, "the seventh item stays behind Show more"
        assert f"+{count - 6} more" in collapsed_text, "the hidden count is exact"

        expanded = build_details_view(
            details,
            meta=_meta(expanded=kind),
            is_admin=True,
            coding_tools_available=True,
            channel_id=_CHANNEL_ID,
            attribution=None,
        )
        expanded_text = _joined(expanded)
        assert "Show fewer" in _button_labels(expanded), "expanded lists can collapse"
        visible_count = min(count, 60)
        assert f"ITEM_{visible_count - 1:02d}" in expanded_text, (
            "expansion keeps complete ordered items up to the cap"
        )
        if count > 60:
            assert f"+{count - 60} more" in expanded_text, "the expanded cap reports its remainder"


def test_details_view_when_coding_tools_unconfigured_renders_note_not_button() -> None:
    available = build_details_view(
        _details(),
        meta=_meta(),
        is_admin=True,
        coding_tools_available=True,
        channel_id=_CHANNEL_ID,
        attribution=None,
    )
    assert ACTION_CODING_TOOLS in _action_ids(available), (
        "a configured deployment offers the token button"
    )
    assert "claude mcp add" not in _joined(available), "CLI instructions wait for the click"

    unavailable = build_details_view(
        _details(),
        meta=_meta(),
        is_admin=True,
        coding_tools_available=False,
        channel_id=_CHANNEL_ID,
        attribution=None,
    )
    assert ACTION_CODING_TOOLS not in _action_ids(unavailable), (
        "a button that cannot produce a working connection is not offered"
    )
    assert "Coding-tool access is not configured for this deployment." in _joined(unavailable), (
        "the reason the button is missing is stated"
    )


@pytest.mark.parametrize(
    ("binding", "github", "expected_kind", "expected_phrase"),
    [
        (
            _binding(),
            GitHubDeploymentFacts(has_fallback_pat=True, app_configured=True),
            "connected",
            "via token",
        ),
        (
            _binding(proof_at=None, proof_account_id=None, proof_kind=None),
            GitHubDeploymentFacts(has_fallback_pat=True, app_configured=True),
            "not_checked",
            "Not checked yet",
        ),
        (
            _binding(ma_secret_ref="none", proof_kind="public"),
            GitHubDeploymentFacts(has_fallback_pat=True, app_configured=False),
            "connected",
            "public repo",
        ),
        (
            _binding(ma_secret_ref="none", proof_kind="pat"),
            GitHubDeploymentFacts(has_fallback_pat=False, app_configured=True),
            "checked",
            "via the GitHub App",
        ),
        (
            _binding(ma_secret_ref="none", proof_kind="pat"),
            GitHubDeploymentFacts(has_fallback_pat=False, app_configured=False),
            "needs_attention",
            "⚠️ needs attention:",
        ),
    ],
)
def test_details_view_renders_each_repo_access_kind(
    binding: AgentRepoBindingRow,
    github: GitHubDeploymentFacts,
    expected_kind: RepoAccessKind,
    expected_phrase: str,
) -> None:
    details = _details(binding=binding, github=github)
    assert details.repo is not None, "the fixture binds a repo"
    assert details.repo.access.kind == expected_kind, (
        "the fixture must produce the access kind under test"
    )
    view = build_details_view(
        details,
        meta=_meta(),
        is_admin=True,
        coding_tools_available=True,
        channel_id=_CHANNEL_ID,
        attribution=None,
    )
    rendered = _joined(view)
    assert "<https://github.com/pymc-labs/churn-models|pymc-labs/churn-models>" in rendered, (
        "the repo is a link to itself, labelled owner/repo"
    )
    assert expected_phrase in rendered, f"{expected_kind} must read as {expected_phrase!r}"
    if expected_kind in {"connected", "checked"} and binding.proof_at is not None:
        assert "Last checked" in rendered, "a recorded check is dated"
    assert "connected" not in rendered, (
        "the panel describes what was recorded; it never claims a live connection"
    )


def test_details_view_when_no_repo_omits_the_optional_section() -> None:
    view = build_details_view(
        _details(binding=None),
        meta=_meta(),
        is_admin=True,
        coding_tools_available=True,
        channel_id=_CHANNEL_ID,
        attribution=None,
    )
    assert "*Repository:*" not in _joined(view), "an unbound repo adds no empty-state noise"


def test_details_view_when_skills_truncated_says_names_may_be_missing() -> None:
    skills = [
        {"type": "custom", "skill_id": "skill_one", "version": "1"},
    ]
    view = build_details_view(
        _details(skills=skills, skills_truncated=True),
        meta=_meta(),
        is_admin=True,
        coding_tools_available=True,
        channel_id=_CHANNEL_ID,
        attribution=None,
    )
    assert "Some skill names may be missing." in _joined(view), (
        "a truncated skills listing says it is incomplete rather than reading as the whole set"
    )


def test_details_view_when_skill_source_truncated_but_empty_keeps_the_warning() -> None:
    view = build_details_view(
        _details(skills=[], skills_truncated=True),
        meta=_meta(),
        is_admin=True,
        coding_tools_available=True,
        channel_id=_CHANNEL_ID,
        attribution=None,
    )
    assert "*Skills*" not in _joined(view), "an empty list does not grow an empty section"
    assert "Some skill names may be missing." in _joined(view), (
        "source truncation remains visible even when no skill name was resolved"
    )


# ---------------------------------------------------------------------------
# Who answers where
# ---------------------------------------------------------------------------


def test_routing_view_renders_channel_mentions_not_raw_ids_and_names_deployment_default() -> None:
    answering_map = _answering_map(
        channels=[
            ChannelConfigRow(
                tenant_id=_TENANT_ID,
                channel_id=_CHANNEL_ID,
                agent_name="research-bot",
                agent_name_set_by_account_id=_MAKER,
                agent_name_set_at=_MOMENT,
            )
        ],
        tenant=TenantConfigRow(tenant_id=_TENANT_ID, agent_name="daimon"),
        deployment_default="daimon",
    )
    view = build_routing_view(
        answering_map,
        page=paginate(answering_map.channel_overrides, page=0, page_size=PANEL_PAGE_SIZE),
        meta=_meta(),
        is_admin=True,
        attributions={_MAKER: "<@U1>"},
        setup_links=[],
        channel_id=_CHANNEL_ID,
        unrouted_agent_name=None,
    )
    rendered = _joined(view)
    assert f"*Channel:* <#{_CHANNEL_ID}>\n*Agent:* research-bot" in rendered, (
        "a channel row reads as a live mention, not a raw id"
    )
    assert "set by <@U1>" in rendered, "a recorded actor is named"
    assert "*Workspace default:* daimon" in rendered, "the workspace default is named"
    assert "Deployment default: *daimon*" in rendered, "the deployment default is named too"
    assert "_not in effect while a workspace default is set_" in rendered, (
        "a workspace default consumes the deployment fall-through, and the view says so"
    )
    assert PRECEDENCE_LINE in rendered, "the precedence rule is stated once, from the core"
    assert "Tell Daimon: Make research-bot answer in <#C0HERE1111>." in rendered, (
        "an admin gets the request in their own voice"
    )
    assert "agent_setup__conversation" not in _action_ids(view), (
        "the routing view carries no setup button"
    )


def test_routing_view_when_member_asks_an_admin_for_the_change() -> None:
    answering_map = _answering_map()
    view = build_routing_view(
        answering_map,
        page=paginate(answering_map.channel_overrides, page=0, page_size=PANEL_PAGE_SIZE),
        meta=_meta(),
        is_admin=False,
        attributions={},
        setup_links=[],
        channel_id=_CHANNEL_ID,
        unrouted_agent_name="churn-explorer",
    )
    assert "An admin can tell Daimon: Make churn-explorer answer in <#C0HERE1111>." in _joined(
        view
    ), "a member is told who can make the change, and exactly what to ask for"


def test_routing_view_paginates_channel_overrides() -> None:
    channels = [
        ChannelConfigRow(tenant_id=_TENANT_ID, channel_id=f"C{index:03d}", agent_name="daimon")
        for index in range(25)
    ]
    answering_map = _answering_map(channels=channels)
    page = paginate(answering_map.channel_overrides, page=1, page_size=PANEL_PAGE_SIZE)
    view = build_routing_view(
        answering_map,
        page=page,
        meta=_meta(),
        is_admin=True,
        attributions={},
        setup_links=[],
        channel_id=_CHANNEL_ID,
        unrouted_agent_name=None,
    )
    rendered = _joined(view)
    assert "Page 2 of 2" in rendered, "the second page says so"
    assert "<#C020>" in rendered, "page two carries the twenty-first channel"
    assert "<#C000>" not in rendered, "page two does not repeat page one"
    assert len(_blocks(view)) <= MAX_BLOCKS_PER_VIEW, "a full routing page fits the block budget"


def test_routing_view_lists_setup_conversations_separately() -> None:
    answering_map = _answering_map(
        channels=[
            ChannelConfigRow(
                tenant_id=_TENANT_ID, channel_id=_OTHER_CHANNEL_ID, agent_name="research-bot"
            )
        ]
    )
    links = [f"https://app.slack.com/client/T/{index}" for index in range(14)]
    view = build_routing_view(
        answering_map,
        page=paginate(answering_map.channel_overrides, page=0, page_size=PANEL_PAGE_SIZE),
        meta=_meta(),
        is_admin=True,
        attributions={},
        setup_links=links,
        channel_id=_CHANNEL_ID,
        unrouted_agent_name=None,
    )
    rendered = _joined(view)
    assert "*Setup conversations*" in rendered, "open setup threads get their own heading"
    assert links[9] in rendered, "the first ten links are listed"
    assert links[10] not in rendered, "the list is bounded at ten"
    assert f"*Channel:* <#{_OTHER_CHANNEL_ID}>\n*Agent:* research-bot" in rendered, (
        "setup threads are listed apart from the channel routing they sit beside"
    )


def test_routing_view_when_nothing_is_set_says_so() -> None:
    answering_map = _answering_map(deployment_default=None)
    view = build_routing_view(
        answering_map,
        page=paginate(answering_map.channel_overrides, page=0, page_size=PANEL_PAGE_SIZE),
        meta=_meta(),
        is_admin=True,
        attributions={},
        setup_links=[],
        channel_id=_CHANNEL_ID,
        unrouted_agent_name=None,
    )
    rendered = _joined(view)
    assert "*Workspace default:* Not assigned" in rendered, "an unset workspace default is stated"
    assert "_no deployment default_" in rendered, "an unset deployment default is stated"
    assert "_none open_" in rendered, "no open setup conversation is stated"


# ---------------------------------------------------------------------------
# Creating and the New agent form
# ---------------------------------------------------------------------------


def test_creating_view_names_the_agent_being_created() -> None:
    view = build_creating_view(agent_name="churn-explorer", meta=_meta(root_view_id="V1"))
    assert view["title"]["text"] == "New agent", "the placeholder keeps the form's title"
    rendered = _joined(view)
    assert "Creating *churn-explorer*…" in rendered, "the placeholder names what is being created"
    assert "This takes a few seconds." in rendered, "it sets an expectation rather than spinning"
    assert json.loads(view["private_metadata"])["r"] == "V1", (
        "the root view id survives so the roster can be refreshed after the create"
    )


def test_new_agent_form_keeps_legacy_block_and_action_ids() -> None:
    choices = list_model_choices(default="claude-sonnet-4-6")
    legacy = build_l3_new_agent_form(
        team_id=_TEAM_ID,
        channel_id=_CHANNEL_ID,
        model_options=[
            {"text": {"type": "plain_text", "text": choice.label}, "value": choice.id}
            for choice in choices
        ],
        initial_model_option={
            "text": {"type": "plain_text", "text": choices[0].label},
            "value": choices[0].id,
        },
    )
    current = build_new_agent_form(meta=_meta(), model_choices=choices)
    assert current["callback_id"] == CALLBACK_NEW_AGENT == legacy["callback_id"], (
        "the submission still arrives on the same callback id"
    )
    assert [block["block_id"] for block in _blocks(current)] == [
        block["block_id"] for block in _blocks(legacy)
    ], "the evaluator reads inputs by block_id; those must not move"
    assert _action_ids(current) == _action_ids(legacy), (
        "the evaluator reads inputs by action_id; those must not move either"
    )
    assert current["submit"]["text"] == "Create", "the form still submits as Create"


def test_new_agent_form_when_validation_failed_restores_inputs_and_shows_the_error() -> None:
    choices = list_model_choices(default="claude-sonnet-4-6")
    view = build_new_agent_form(
        meta=_meta(),
        model_choices=choices,
        initial_name="churn explorer",
        initial_purpose="explain churn",
        initial_model=choices[-1].id,
        error="Agent names cannot contain spaces.",
    )
    rendered = _joined(view)
    assert "Agent names cannot contain spaces." in rendered, "the reason is shown in the form"
    blocks = {block["block_id"]: block for block in _blocks(view) if "block_id" in block}
    assert blocks["new_agent__name"]["element"]["initial_value"] == "churn explorer", (
        "a rejected submission keeps what the person typed"
    )
    assert blocks["new_agent__prompt"]["element"]["initial_value"] == "explain churn", (
        "the other inputs survive too"
    )
    assert blocks["new_agent__model"]["element"]["initial_option"]["value"] == choices[-1].id, (
        "the chosen model survives the round trip"
    )


# ---------------------------------------------------------------------------
# private_metadata budget
# ---------------------------------------------------------------------------


def test_every_panel_view_private_metadata_under_limit_and_without_tenant_id() -> None:
    long_name = "n" * 64
    meta = _meta(expanded="connections", root_view_id="V0123456789")
    answering = _roster_agent(long_name, created_by_account_id=_MAKER)
    roster = _roster(answering, answering=answering)
    answering_map = _answering_map(
        channels=[
            ChannelConfigRow(tenant_id=_TENANT_ID, channel_id=_CHANNEL_ID, agent_name=long_name)
        ]
    )
    views = [
        build_agents_view(
            roster,
            page=_page(roster),
            meta=meta,
            is_admin=True,
            attributions={_MAKER: "<@U1>"},
            channel_id=_CHANNEL_ID,
        ),
        build_details_view(
            _details(name=long_name),
            meta=meta,
            is_admin=True,
            coding_tools_available=True,
            channel_id=_CHANNEL_ID,
            attribution="<@U1>",
        ),
        build_routing_view(
            answering_map,
            page=paginate(answering_map.channel_overrides, page=0, page_size=PANEL_PAGE_SIZE),
            meta=meta,
            is_admin=True,
            attributions={},
            setup_links=[],
            channel_id=_CHANNEL_ID,
            unrouted_agent_name=long_name,
        ),
        build_creating_view(agent_name=long_name, meta=meta),
        build_new_agent_form(
            meta=meta, model_choices=list_model_choices(default="claude-sonnet-4-6")
        ),
    ]
    for view in views:
        metadata = view["private_metadata"]
        assert len(metadata) < MAX_PRIVATE_METADATA_CHARS, (
            f"{view['callback_id']} must keep private_metadata inside Slack's budget"
        )
        assert str(_TENANT_ID) not in metadata, (
            f"{view['callback_id']} must not carry a tenant id — it is re-derived server-side"
        )
        assert "ag_" not in metadata, f"{view['callback_id']} must not carry an MA id either"


def test_every_panel_view_uses_the_channel_it_was_rendered_for() -> None:
    for view in _all_views():
        assert json.loads(view["private_metadata"])["c"] == _CHANNEL_ID, (
            "a view's next click must land back in the channel the panel was opened from"
        )


def test_details_and_routing_views_carry_their_own_callback_ids() -> None:
    agents, details, routing, creating = _all_views()
    assert agents["callback_id"] == "agent_setup", "the root keeps the slash command's callback id"
    assert details["callback_id"] == "agent_setup__details_view", "Details has its own"
    assert routing["callback_id"] == "agent_setup__routing_view", "routing has its own"
    assert creating["callback_id"] == "agent_setup__creating", "the placeholder has its own"


def test_new_agent_form_metadata_carries_the_page_the_reader_was_on() -> None:
    meta = _meta(page=3, root_view_id="V1")
    form = build_new_agent_form(
        meta=meta, model_choices=list_model_choices(default="claude-sonnet-4-6")
    )
    decoded = json.loads(form["private_metadata"])
    assert decoded["p"] == 3, (
        "the form hands the roster page back so the post-create refresh lands where the reader was"
    )
    assert decoded["r"] == "V1", "and names the root view it has to refresh"
    creating = build_creating_view(agent_name="churn-explorer", meta=meta)
    assert json.loads(creating["private_metadata"])["p"] == 3, (
        "the placeholder in between must not drop the page either"
    )
