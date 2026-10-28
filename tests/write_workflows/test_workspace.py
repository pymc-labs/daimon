from __future__ import annotations

import base64
from email import policy
from email.parser import BytesParser

import pytest


@pytest.fixture
def workflow(load_workflow):
    module = load_workflow("workspace-write", "workspace_write")
    from workflow_io import Failure

    module.Failure = Failure
    return module


@pytest.mark.parametrize(
    "operation",
    ["auth", "gws auth", "drive-share", "drive-delete", "drive-move", "calendar-delete"],
)
def test_refused_before_transport(workflow, operation):
    with pytest.raises(workflow.Failure, match="Unsupported operation"):
        workflow.run(
            {"operation": operation, "body": {}}, "synthetic", lambda *_: pytest.fail("network")
        )


def test_gmail_draft_preserves_newlines_and_explicit_recipient(workflow, fake_api):
    api = fake_api([{"id": "draft-1", "message": {"id": "message-1"}}])
    request = {
        "operation": "gmail-draft",
        "body": {"to": ["test@example.com"], "subject": "Test", "text": "first\nsecond"},
    }
    result = workflow.run(request, "synthetic", lambda *_: api)
    method, path, body = api.calls[0]
    message = BytesParser(policy=policy.default).parsebytes(
        base64.urlsafe_b64decode(body["message"]["raw"])
    )
    assert method == "POST" and path.endswith("/drafts")
    assert message["To"] == "test@example.com"
    assert message.get_content().splitlines() == ["first", "second"]
    assert result["id"] == "draft-1"
    assert "synthetic" not in str(result)


@pytest.mark.parametrize(
    "body",
    [
        {"to": [], "subject": "x", "text": "x"},
        {"to": ["a@example.com\nBcc:b@example.com"], "subject": "x", "text": "x"},
        {"to": ["a@example.com"], "subject": "x", "text": "x", "raw": "bypass"},
    ],
)
def test_mail_refuses_missing_recipient_header_injection_and_raw_bypass(workflow, body):
    with pytest.raises(workflow.Failure):
        workflow.run(
            {"operation": "gmail-send", "body": body},
            "synthetic",
            lambda *_: pytest.fail("network"),
        )


def test_drive_rename_cannot_smuggle_sharing_or_move(workflow):
    for body in (
        {"name": "new", "parents": ["other"]},
        {"name": "new", "permissions": []},
        {"name": "new", "trashed": True},
    ):
        with pytest.raises(workflow.Failure):
            workflow.plan({"operation": "drive-rename", "target": "file-1", "body": body})


def test_existing_document_edit_and_calendar_target(workflow):
    assert (
        workflow.plan(
            {
                "operation": "docs-edit",
                "target": "existing",
                "body": {
                    "requests": [
                        {"insertText": {"endOfSegmentLocation": {}, "text": "hello\nworld"}}
                    ]
                },
            }
        )[2]
        == "/v1/documents/existing:batchUpdate"
    )
    with pytest.raises(workflow.Failure):
        workflow.plan(
            {
                "operation": "calendar-update",
                "target": {"calendar_id": "primary"},
                "body": {
                    "summary": "x",
                    "start": {"date": "2026-10-28"},
                    "end": {"date": "2026-10-29"},
                },
            }
        )


def test_google_credentials_not_needed_for_validation_cli(workflow, tmp_path, monkeypatch, capsys):
    import io

    import workflow_io

    request = tmp_path / "request.json"
    request.write_text('{"operation":"auth","body":{}}')
    monkeypatch.setattr("sys.argv", ["workspace_write.py", str(request)])
    monkeypatch.setattr("sys.stdin", io.StringIO("synthetic-secret"))
    assert workflow_io.main(workflow.run, "google") == 1
    output = capsys.readouterr().out
    assert "synthetic-secret" not in output and '"ok": false' in output


def test_http_errors_and_redirects_never_echo_vendor_body(load_workflow, monkeypatch):
    import io
    from urllib.error import HTTPError

    runtime = load_workflow("workspace-write", "workflow_io")

    class Opener:
        def open(self, request, timeout):
            raise HTTPError(
                request.full_url, 403, "synthetic-secret", {}, io.BytesIO(b"synthetic-secret")
            )

    monkeypatch.setattr(runtime, "build_opener", lambda *_: Opener())
    with pytest.raises(runtime.Failure, match="HTTP 403") as error:
        runtime.fetch("POST", "https://docs.googleapis.com/v1/documents", {}, b"{}")
    assert "synthetic-secret" not in str(error.value)
    assert (
        runtime.NoRedirect().redirect_request(None, None, 302, None, None, "https://evil.example")
        is None
    )


def test_missing_response_id_is_unknown_outcome_not_success(workflow, fake_api):
    api = fake_api([{}])
    with pytest.raises(workflow.Failure, match="reconcile"):
        workflow.run(
            {"operation": "docs-create", "body": {"title": "Test"}}, "synthetic", lambda *_: api
        )


def test_cli_redacts_unexpected_provider_exception(workflow, tmp_path, monkeypatch, capsys):
    import io

    import workflow_io

    request = tmp_path / "request.json"
    request.write_text("{}")
    monkeypatch.setattr("sys.argv", ["workspace_write.py", str(request)])
    monkeypatch.setattr("sys.stdin", io.StringIO("synthetic-token"))

    def fail(*_):
        raise AttributeError("synthetic-secret")

    assert workflow_io.main(fail, "google") == 1
    assert "synthetic-secret" not in capsys.readouterr().out
