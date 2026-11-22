"""The GitHub connect flow's selection parsing and views, independent of any page."""

from __future__ import annotations

from daimon.core.github_connect_views import (
    ConnectInstallation,
    ConnectPage,
    ConnectRepo,
    ConnectSelection,
    agent_editor_view,
    selection_from_form,
)


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
