"""The GitHub connect flow's selection parsing and views, independent of any page."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from daimon.core.github_connect_views import (
    ConnectInstallation,
    ConnectPage,
    ConnectRepo,
    ConnectSelection,
    agent_editor_view,
    selection_from_form,
)
from daimon.core.stores.github_access import AgentRepo


def test_selection_from_form_keeps_known_fields_and_retries_only_those() -> None:
    selection = selection_from_form(
        b"state=s&repo=1&repo=2&access_7=write&access_x=read&cookie=secret&working=keep"
    )

    assert isinstance(selection, ConnectSelection), "a well-formed body parses"
    assert selection.repo == ("1", "2"), "multi-valued fields keep every value in order"
    assert selection.repo_access == {"7": ("write",)}, "only access_<digits> is scoped access"
    assert selection.unknown_fields == ("access_x", "cookie"), "anything else is recorded"
    assert selection.form_fields() == {
        "state": ["s"],
        "repo": ["1", "2"],
        "working": ["keep"],
        "access_7": ["write"],
    }, "a retry re-posts the known fields and drops the rest"


def test_agent_editor_page_round_trips_through_json() -> None:
    installation = ConnectInstallation(
        1, 5, "acme", "selected", (ConnectRepo(10, 5, 1, "acme/data", "admin"),)
    )
    page = ConnectPage(
        view=agent_editor_view(
            root="https://app.example",
            state="state",
            invitation_hash="invitation",
            secret="secret",
            agent_name="Agent",
            workspace="Team",
            platform="slack",
            grants=[],
            installations=[installation],
            snapshot="snapshot",
            cancel_url="https://app.example/cancel",
            install_url="https://github.com/apps/x/installations/new",
            requested_repo="ACME/DATA",
        )
    )

    assert ConnectPage.model_validate_json(page.model_dump_json()) == page, (
        "a view is plain data another adapter can send as JSON"
    )
    assert page.view.kind == "agent_editor" and page.view.requested_repo_id == 10, (
        "the requested repo is matched without regard to case"
    )
    assert page.view.confirm_url == "https://app.example/oauth/github/confirm", (
        "URLs sit under the root the person is on"
    )


def test_agent_editor_view_json_leaves_out_a_hidden_grants_name_and_accounts() -> None:
    now = datetime.now(UTC)
    person = uuid.uuid4()

    def grant(repo_id: int, name: str) -> AgentRepo:
        return AgentRepo(
            repo_id=repo_id,
            full_name=name,
            scope="agent",
            max_access="write",
            baseline_access="write",
            ceiling_access="write",
            status="active",
            added_by_account_id=person,
            added_at=now,
            granted_by_account_id=person,
            granted_at=now,
        )

    installation = ConnectInstallation(
        1, 5, "acme", "selected", (ConnectRepo(10, 5, 1, "acme/open", "admin"),)
    )
    view = agent_editor_view(
        root="https://app.example",
        state="state",
        invitation_hash="invitation",
        secret="secret",
        agent_name="Agent",
        workspace="Team",
        platform="slack",
        grants=[grant(10, "acme/open"), grant(11, "acme/secret-repo")],
        installations=[installation],
        snapshot="snapshot",
        cancel_url="https://app.example/cancel",
        install_url="https://github.com/apps/x/installations/new",
    )

    body = ConnectPage(view=view).model_dump_json()
    assert "acme/open" in body, "a repo the person sees on GitHub keeps its name"
    assert "secret-repo" not in body, (
        "a grant outside their GitHub access must not reach the browser"
    )
    hidden = view.grants[1]
    assert (hidden.added_by_account_id, hidden.granted_by_account_id) == (None, None), (
        "a hidden grant's account ids are dropped"
    )
    assert [g.full_name for g in view.grants] == ["acme/open", "acme/"], (
        "the owner stays for grouping"
    )
