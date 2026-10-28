from __future__ import annotations

from datetime import UTC, datetime

import pytest


@pytest.fixture
def workflow(load_workflow, monkeypatch):
    module = load_workflow("toggl-edits", "toggl_edits")

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 28, 12, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr(module, "datetime", Clock)
    return module


@pytest.fixture
def edit_request():
    return {
        "operation": "update",
        "workspace_id": 10,
        "person": {"email": "member@example.com"},
        "entry_ids": [30],
        "changes": {"description": "corrected"},
    }


def entry(user=20, entry_id=30):
    return {
        "id": entry_id,
        "user_id": user,
        "workspace_id": 10,
        "start": "2026-10-28T10:00:00Z",
        "duration": 1800,
        "stop": "2026-10-28T10:30:00Z",
    }


def report(value):
    return [
        {
            "user_id": value["user_id"],
            "time_entries": [
                {
                    **{
                        k: v
                        for k, v in value.items()
                        if k not in ("user_id", "workspace_id", "duration")
                    },
                    "seconds": value["duration"],
                }
            ],
        }
    ]


@pytest.fixture
def toggl_api(fake_api):
    class WorkspaceApi(fake_api):
        def request(self, method, path, body=None):
            if method == "GET":
                assert path == "/api/v9/workspaces/10/users" and body is None
            elif path == "/reports/api/v3/workspace/10/search/time_entries":
                assert method == "POST"
                assert body["time_entry_ids"] in ([30], [31])
                assert body == {
                    "time_entry_ids": body["time_entry_ids"],
                    "start_date": "2025-10-28",
                    "end_date": "2026-10-28",
                    "user_ids": [20],
                    "grouped": False,
                    "page_size": 2,
                    "rounding": 0,
                }
            else:
                assert (method, path) in {
                    ("PUT", "/api/v9/workspaces/10/time_entries/30"),
                    ("PATCH", "/api/v9/workspaces/10/time_entries/30,31"),
                }
            return super().request(method, path, body)

    return WorkspaceApi


def mutations(api):
    return [call for call in api.calls if call[0] in ("PUT", "PATCH")]


MEMBERS = [
    {"id": 99, "email": "key.owner@example.com", "admin": True},
    {"id": 20, "email": "member@example.com"},
]


def test_key_owner_never_substitutes_for_named_member(workflow, toggl_api, edit_request):
    api = toggl_api([MEMBERS, report(entry()), entry()])
    result = workflow.run(edit_request, api)
    method, path, body = api.calls[-1]
    assert method == "PUT" and path == "/api/v9/workspaces/10/time_entries/30"
    assert body == {"description": "corrected", "user_id": 20, "workspace_id": 10}
    assert result["user_id"] == 20 and result["user_id"] != 99


@pytest.mark.parametrize(
    "person", [{}, {"user_id": 20, "email": "member@example.com"}, {"user_id": True}]
)
def test_person_required_before_network(workflow, toggl_api, edit_request, person):
    edit_request["person"] = person
    api = toggl_api()
    with pytest.raises(workflow.Failure):
        workflow.run(edit_request, api)
    assert not api.calls


@pytest.mark.parametrize(
    "members",
    [[], [{"id": 20, "email": "member@example.com"}, {"id": 21, "email": "MEMBER@example.com"}]],
)
def test_missing_or_ambiguous_person_refused(workflow, toggl_api, edit_request, members):
    api = toggl_api([members])
    with pytest.raises(workflow.Failure, match="missing or ambiguous"):
        workflow.run(edit_request, api)
    assert not mutations(api)


def test_bulk_checks_every_owner_before_any_write(workflow, toggl_api):
    api = toggl_api([MEMBERS, report(entry()), report(entry(user=99, entry_id=31))])
    edit_request = {
        "operation": "bulk-edit",
        "workspace_id": 10,
        "person": {"user_id": 20},
        "entry_ids": [30, 31],
        "operations": [{"op": "replace", "path": "/description", "value": "x"}],
    }
    with pytest.raises(workflow.Failure, match="does not belong"):
        workflow.run(edit_request, api)
    assert len(api.calls) == 3 and not mutations(api)


def test_conflicting_legacy_owner_evidence_refused(workflow, toggl_api, edit_request):
    api = toggl_api([MEMBERS, report({**entry(), "uid": 99})])
    with pytest.raises(workflow.Failure, match="Conflicting"):
        workflow.run(edit_request, api)
    assert not mutations(api)


def test_removed_stop_operation_refused_before_network(workflow, toggl_api):
    api = toggl_api()
    with pytest.raises(workflow.Failure, match="Only update and bulk-edit"):
        workflow.run(
            {"operation": "stop", "workspace_id": 10, "person": {"user_id": 20}, "entry_ids": [30]},
            api,
        )
    assert not api.calls


@pytest.mark.parametrize("bulk", [False, True])
def test_missing_running_entry_refused_with_zero_writes(workflow, toggl_api, edit_request, bulk):
    responses = [MEMBERS, []]
    if bulk:
        edit_request.pop("changes")
        edit_request.update(
            operation="bulk-edit",
            entry_ids=[30, 31],
            operations=[{"op": "replace", "path": "/description", "value": "x"}],
        )
        responses = [MEMBERS, report(entry()), []]
    api = toggl_api(responses)
    with pytest.raises(
        workflow.Failure, match=r"running entries and very recent edits \(Reports lag\)"
    ):
        workflow.run(edit_request, api)
    assert not mutations(api)


def test_every_report_search_has_explicit_utc_date_window(workflow, toggl_api, edit_request):
    api = toggl_api([MEMBERS, report(entry()), entry()])
    workflow.run(edit_request, api, datetime.fromisoformat("2026-10-29T00:30:00+02:00"))
    body = api.calls[1][2]
    assert body["start_date"] == "2025-10-28"
    assert body["end_date"] == "2026-10-28"
    # Exactly 365 elapsed days even across a leap day.
    start, end = workflow.report_window(datetime(2024, 3, 1, tzinfo=UTC))
    assert start.isoformat() == "2023-03-02" and (end - start).days == 365


def test_older_report_entry_refused_even_if_provider_returns_it(workflow, toggl_api, edit_request):
    old = {**entry(), "start": "2025-10-27T10:00:00Z", "stop": "2025-10-27T10:30:00Z"}
    api = toggl_api([MEMBERS, report(old)])
    with pytest.raises(workflow.Failure, match="older entries are not supported"):
        workflow.run(edit_request, api)
    assert not mutations(api)


@pytest.mark.parametrize("changes", [{"stop": None}, {"duration": -1}])
def test_cannot_reopen_stopped_entry(workflow, toggl_api, edit_request, changes):
    edit_request["changes"] = changes
    api = toggl_api()
    with pytest.raises(workflow.Failure):
        workflow.run(edit_request, api)
    assert not api.calls


def test_stopped_duration_change_has_matching_stop(workflow, toggl_api, edit_request):
    edit_request["changes"] = {"duration": 3600}
    api = toggl_api(
        [MEMBERS, report(entry()), {**entry(), "duration": 3600, "stop": "2026-10-28T11:00:00Z"}]
    )
    workflow.run(edit_request, api)
    assert api.calls[-1][2] == {
        "start": "2026-10-28T10:00:00+00:00",
        "stop": "2026-10-28T11:00:00+00:00",
        "duration": 3600,
        "user_id": 20,
        "workspace_id": 10,
    }


@pytest.mark.parametrize(
    "patch",
    [
        {"op": "replace", "path": "/user_id", "value": 99},
        {"op": "add", "path": "/description", "value": "x"},
        {"op": "replace", "path": "/workspace_id", "value": 12},
    ],
)
def test_no_reassignment_or_unknown_bulk_patch(workflow, toggl_api, patch):
    api = toggl_api()
    with pytest.raises(workflow.Failure):
        workflow.run(
            {
                "operation": "bulk-edit",
                "workspace_id": 10,
                "person": {"user_id": 20},
                "entry_ids": [30],
                "operations": [patch],
            },
            api,
        )
    assert not api.calls


def test_bulk_partial_result_names_success_and_failed_ids_without_vendor_message(
    workflow, toggl_api
):
    api = toggl_api(
        [
            MEMBERS,
            report(entry()),
            report(entry(entry_id=31)),
            {"success": [30], "failure": [{"id": 31, "message": "synthetic-secret"}]},
        ]
    )
    with pytest.raises(workflow.Failure) as error:
        workflow.run(
            {
                "operation": "bulk-edit",
                "workspace_id": 10,
                "person": {"user_id": 20},
                "entry_ids": [30, 31],
                "operations": [{"op": "replace", "path": "/description", "value": "x"}],
            },
            api,
        )
    assert error.value.details == {
        "workspace_id": 10,
        "user_id": 20,
        "success": [30],
        "failure": [31],
    }
    assert "synthetic-secret" not in str(error.value)


def test_403_names_workspace_admin_role_and_does_not_retry(load_workflow, monkeypatch):
    import io
    from urllib.error import HTTPError

    runtime = load_workflow("toggl-edits", "workflow_io")
    calls = []

    class Opener:
        def open(self, request, timeout):
            calls.append(request)
            raise HTTPError(
                request.full_url, 403, "synthetic-secret", {}, io.BytesIO(b"synthetic-secret")
            )

    monkeypatch.setattr(runtime, "build_opener", lambda *_: Opener())
    with pytest.raises(runtime.Failure, match="workspace administrator") as error:
        runtime.fetch(
            "PUT", "https://api.track.toggl.com/api/v9/workspaces/10/time_entries/30", {}, b"{}"
        )
    assert len(calls) == 1 and "synthetic-secret" not in str(error.value)


def test_bulk_size_and_admin_operations_refused(workflow, toggl_api, edit_request):
    edit_request["entry_ids"] = list(range(1, 102))
    with pytest.raises(workflow.Failure, match="1–100"):
        workflow.run(edit_request, toggl_api())
    edit_request["entry_ids"] = [30]
    edit_request["operation"] = "create-project"
    with pytest.raises(workflow.Failure, match="Only"):
        workflow.run(edit_request, toggl_api())


@pytest.mark.parametrize(
    "rows",
    [
        [],
        report(entry()) * 2,
        report(entry(user=99)),
        [{"user_id": 20, "time_entries": []}],
        [
            {
                "user_id": 20,
                "time_entries": [
                    {
                        "id": 31,
                        "seconds": 1,
                        "start": "2026-10-28T10:00:00Z",
                        "stop": "2026-10-28T10:00:01Z",
                    }
                ],
            }
        ],
        [
            {
                "user_id": 20,
                "time_entries": [{"id": 30, "seconds": 1, "start": "2026-10-28T10:00:00Z"}],
            }
        ],
        [{"user_id": 20, "workspace_id": 11, "time_entries": report(entry())[0]["time_entries"]}],
    ],
)
def test_incomplete_or_conflicting_workspace_report_refuses_all_writes(
    workflow, toggl_api, edit_request, rows
):
    api = toggl_api([MEMBERS, rows])
    with pytest.raises(workflow.Failure):
        workflow.run(edit_request, api)
    assert not mutations(api)
    assert api.calls[-1] == (
        "POST",
        "/reports/api/v3/workspace/10/search/time_entries",
        {
            "start_date": "2025-10-28",
            "end_date": "2026-10-28",
            "time_entry_ids": [30],
            "user_ids": [20],
            "grouped": False,
            "page_size": 2,
            "rounding": 0,
        },
    )
