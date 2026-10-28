from __future__ import annotations

from datetime import UTC, datetime

import pytest


@pytest.fixture
def workflow(load_workflow):
    return load_workflow("toggl-edits", "toggl_edits")


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
        "duration": -1,
        "stop": None,
    }


MEMBERS = [
    {"id": 99, "email": "key.owner@example.com", "admin": True},
    {"id": 20, "email": "member@example.com"},
]


def test_key_owner_never_substitutes_for_named_member(workflow, fake_api, edit_request):
    api = fake_api([MEMBERS, entry(), entry()])
    result = workflow.run(edit_request, api)
    method, path, body = api.calls[-1]
    assert method == "PUT" and path == "/api/v9/workspaces/10/time_entries/30"
    assert body == {"description": "corrected", "user_id": 20, "workspace_id": 10}
    assert result["user_id"] == 20 and result["user_id"] != 99


@pytest.mark.parametrize(
    "person", [{}, {"user_id": 20, "email": "member@example.com"}, {"user_id": True}]
)
def test_person_required_before_network(workflow, fake_api, edit_request, person):
    edit_request["person"] = person
    api = fake_api()
    with pytest.raises(workflow.Failure):
        workflow.run(edit_request, api)
    assert not api.calls


@pytest.mark.parametrize(
    "members",
    [[], [{"id": 20, "email": "member@example.com"}, {"id": 21, "email": "MEMBER@example.com"}]],
)
def test_missing_or_ambiguous_person_refused(workflow, fake_api, edit_request, members):
    api = fake_api([members])
    with pytest.raises(workflow.Failure, match="missing or ambiguous"):
        workflow.run(edit_request, api)
    assert all(call[0] == "GET" for call in api.calls)


def test_bulk_checks_every_owner_before_any_write(workflow, fake_api):
    api = fake_api([MEMBERS, entry(), entry(user=99, entry_id=31)])
    edit_request = {
        "operation": "bulk-edit",
        "workspace_id": 10,
        "person": {"user_id": 20},
        "entry_ids": [30, 31],
        "operations": [{"op": "replace", "path": "/description", "value": "x"}],
    }
    with pytest.raises(workflow.Failure, match="does not belong"):
        workflow.run(edit_request, api)
    assert len(api.calls) == 3 and all(call[0] == "GET" for call in api.calls)


def test_conflicting_legacy_owner_evidence_refused(workflow, fake_api, edit_request):
    api = fake_api([MEMBERS, {**entry(), "uid": 99}])
    with pytest.raises(workflow.Failure, match="Conflicting"):
        workflow.run(edit_request, api)
    assert all(call[0] == "GET" for call in api.calls)


def test_stop_is_put_with_duration_from_verified_start(workflow, fake_api):
    now = datetime(2026, 10, 28, 10, 30, tzinfo=UTC)
    stopped = {**entry(), "stop": now.isoformat(), "duration": 1800}
    api = fake_api([MEMBERS, entry(), stopped])
    result = workflow.run(
        {"operation": "stop", "workspace_id": 10, "person": {"user_id": 20}, "entry_ids": [30]},
        api,
        now,
    )
    assert api.calls[-1] == (
        "PUT",
        "/api/v9/workspaces/10/time_entries/30",
        {
            "start": "2026-10-28T10:00:00+00:00",
            "stop": "2026-10-28T10:30:00+00:00",
            "duration": 1800,
            "user_id": 20,
            "workspace_id": 10,
        },
    )
    assert result["duration"] == 1800
    assert not any("/stop" in call[1] for call in api.calls)


@pytest.mark.parametrize(
    "patch",
    [
        {"op": "replace", "path": "/user_id", "value": 99},
        {"op": "add", "path": "/description", "value": "x"},
        {"op": "replace", "path": "/workspace_id", "value": 12},
    ],
)
def test_no_reassignment_or_unknown_bulk_patch(workflow, fake_api, patch):
    api = fake_api()
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
    workflow, fake_api
):
    api = fake_api(
        [
            MEMBERS,
            entry(),
            entry(entry_id=31),
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


def test_bulk_size_and_admin_operations_refused(workflow, fake_api, edit_request):
    edit_request["entry_ids"] = list(range(1, 102))
    with pytest.raises(workflow.Failure, match="1–100"):
        workflow.run(edit_request, fake_api())
    edit_request["entry_ids"] = [30]
    edit_request["operation"] = "create-project"
    with pytest.raises(workflow.Failure, match="Only"):
        workflow.run(edit_request, fake_api())
