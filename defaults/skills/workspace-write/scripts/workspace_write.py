"""Explicit Google Workspace writes with short-lived delegated access."""

from __future__ import annotations

import base64
from email.message import EmailMessage
from email.policy import SMTP
from urllib.parse import quote

from workflow_io import Api, fields, identifier, main, require, text


def plan(request):
    fields(request, ("operation", "target", "body"), ("operation", "body"))
    operation = request["operation"]
    body = request["body"]
    target = request.get("target")
    if operation in ("gmail-draft", "gmail-send"):
        fields(body, ("to", "cc", "bcc", "subject", "text"), ("to", "subject", "text"))
        message = EmailMessage(policy=SMTP)
        for name in ("to", "cc", "bcc"):
            if name not in body:
                continue
            recipients = body[name]
            require(
                isinstance(recipients, list) and recipients, "Recipients must be an explicit list"
            )
            for address in recipients:
                require(
                    isinstance(address, str)
                    and "@" in address
                    and not any(c in address for c in "\r\n,<> "),
                    "Expected explicit email addresses",
                )
            message[name.title()] = ", ".join(recipients)
        message["Subject"] = text(body["subject"])
        require(isinstance(body["text"], str), "Expected message text")
        message.set_content(body["text"])
        encoded = base64.urlsafe_b64encode(message.as_bytes()).decode()
        if operation == "gmail-draft":
            return (
                "https://gmail.googleapis.com",
                "POST",
                "/gmail/v1/users/me/drafts",
                {"message": {"raw": encoded}},
            )
        return (
            "https://gmail.googleapis.com",
            "POST",
            "/gmail/v1/users/me/messages/send",
            {"raw": encoded},
        )
    if operation == "drive-create":
        fields(body, ("name", "mimeType", "parents", "description"), ("name",))
        text(body["name"])
        if "parents" in body:
            require(
                isinstance(body["parents"], list) and len(body["parents"]) == 1,
                "Name one parent folder",
            )
            identifier(body["parents"][0])
        return "https://www.googleapis.com", "POST", "/drive/v3/files?supportsAllDrives=true", body
    if operation in ("drive-copy", "drive-rename"):
        fields(body, ("name",), ("name",))
        text(body["name"])
        target = identifier(target)
        suffix = "/copy" if operation == "drive-copy" else ""
        return (
            "https://www.googleapis.com",
            "POST" if suffix else "PATCH",
            f"/drive/v3/files/{target}{suffix}?supportsAllDrives=true",
            body,
        )
    if operation == "docs-create":
        fields(body, ("title",), ("title",))
        text(body["title"])
        return "https://docs.googleapis.com", "POST", "/v1/documents", body
    if operation == "docs-edit":
        fields(body, ("requests",), ("requests",))
        require(
            isinstance(body["requests"], list) and 0 < len(body["requests"]) <= 100,
            "Expected 1–100 text requests",
        )
        for item in body["requests"]:
            fields(item, ("insertText", "replaceAllText", "deleteContentRange"))
            require(len(item) == 1, "Expected exactly one Docs text operation")
        return (
            "https://docs.googleapis.com",
            "POST",
            f"/v1/documents/{identifier(target)}:batchUpdate",
            body,
        )
    if operation in ("calendar-create", "calendar-update"):
        require(isinstance(target, dict), "Name the calendar and event explicitly")
        fields(target, ("calendar_id", "event_id"), ("calendar_id",))
        calendar = quote(identifier(target["calendar_id"]), safe="")
        fields(
            body,
            ("summary", "description", "start", "end", "location", "attendees"),
            ("summary", "start", "end") if operation == "calendar-create" else (),
        )
        require(body, "Name at least one Calendar field to update")
        if "summary" in body:
            text(body["summary"])
        for bound in ("start", "end"):
            if bound not in body:
                continue
            fields(body[bound], ("date", "dateTime", "timeZone"))
            require(
                ("date" in body[bound]) != ("dateTime" in body[bound]), "Supply date or dateTime"
            )
        if "attendees" in body:
            require(isinstance(body["attendees"], list), "Expected attendees list")
            for attendee in body["attendees"]:
                fields(attendee, ("email",), ("email",))
                require("@" in text(attendee["email"]), "Expected attendee email")
        path = f"/calendar/v3/calendars/{calendar}/events"
        if operation == "calendar-update":
            path += "/" + identifier(target.get("event_id"))
        return (
            "https://www.googleapis.com",
            "POST" if operation == "calendar-create" else "PATCH",
            path,
            body,
        )
    require(
        False,
        "Unsupported operation: auth, sharing, permissions, file deletion, "
        "Calendar deletion and Drive moves are not supported",
    )


def run(request, token, api_factory=Api):
    base, method, path, body = plan(request)
    api = api_factory(base, "Bearer " + token)
    result = api.request(method, path, body)
    # Never echo MIME message bodies, tokens or raw vendor responses.
    require(isinstance(result, dict), "Invalid provider response; reconcile target before retrying")
    resource_id = result.get("id") or result.get("documentId")
    require(
        isinstance(resource_id, str) and resource_id,
        "Write response has no resource ID; reconcile target before retrying",
    )
    return {
        "operation": request["operation"],
        "id": resource_id,
        "message_id": result.get("message", {}).get("id"),
    }


if __name__ == "__main__":
    raise SystemExit(main(run, "google"))
