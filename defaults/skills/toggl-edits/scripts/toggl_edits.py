"""Narrow shared-key Track edits with mandatory explicit member attribution."""

from __future__ import annotations

import base64
from datetime import UTC, datetime

from workflow_io import Api, Failure, fields, key, main, positive, require, text

EDIT_FIELDS = {
    "description",
    "project_id",
    "task_id",
    "start",
    "stop",
    "duration",
    "tags",
    "billable",
}
BULK_FIELDS = {"description", "project_id", "tags", "billable"}


def timestamp(value):
    try:
        result = datetime.fromisoformat(text(value).replace("Z", "+00:00"))
    except ValueError:
        raise Failure("Expected an ISO 8601 timestamp with timezone") from None
    require(result.tzinfo is not None, "Timestamp must include timezone")
    return result.astimezone(UTC)


def validate_changes(changes):
    fields(changes, EDIT_FIELDS)
    require(changes, "Name at least one change")
    for name, value in changes.items():
        if name == "description":
            require(isinstance(value, str), "Expected description text")
        elif name in ("project_id", "task_id"):
            if value is not None:
                positive(value)
        elif name in ("start", "stop"):
            timestamp(value)
        elif name == "duration":
            require(type(value) is int and value >= 0, "Duration must be nonnegative seconds")
        elif name == "billable":
            require(type(value) is bool, "Billable must be boolean")
        elif name == "tags":
            require(
                isinstance(value, list)
                and all(isinstance(tag, str) and tag.strip() for tag in value),
                "Tags must be strings",
            )
    return changes


def plan(request):
    fields(
        request,
        ("operation", "workspace_id", "person", "entry_ids", "changes", "operations"),
        ("operation", "workspace_id", "person", "entry_ids"),
    )
    require(
        request["operation"] in ("update", "stop", "bulk-edit"),
        "Only update, stop and bulk-edit are supported",
    )
    positive(request["workspace_id"])
    fields(request["person"], ("user_id", "email"))
    require(
        len(request["person"]) == 1,
        "Name exactly one person by user_id or email; never default to the key owner",
    )
    if "user_id" in request["person"]:
        positive(request["person"]["user_id"])
    else:
        require("@" in text(request["person"]["email"]), "Name a workspace member email")
    ids = request["entry_ids"]
    require(isinstance(ids, list) and 0 < len(ids) <= 100, "Expected 1–100 explicit entry IDs")
    for entry_id in ids:
        positive(entry_id)
    require(len(set(ids)) == len(ids), "Duplicate entry IDs")
    if request["operation"] != "bulk-edit":
        require(
            len(ids) == 1 and "operations" not in request,
            "Single-entry operation requires one ID and no patches",
        )
    if request["operation"] == "update":
        validate_changes(request.get("changes"))
    elif request["operation"] == "stop":
        require("changes" not in request, "Stop computes stop time and duration itself")
    else:
        require("changes" not in request, "Bulk edit uses explicit replace patches")
        patches = request.get("operations")
        require(
            isinstance(patches, list) and 0 < len(patches) <= 20, "Expected 1–20 replace patches"
        )
        for patch in patches:
            fields(patch, ("op", "path", "value"), ("op", "path", "value"))
            require(
                patch["op"] == "replace" and patch["path"] in {"/" + name for name in BULK_FIELDS},
                "Unsupported patch: ownership, workspace and administration cannot be changed",
            )
            validate_changes({patch["path"][1:]: patch["value"]})
    return request


def member(api, workspace, person):
    users = api.request("GET", f"/api/v9/workspaces/{workspace}/users")
    require(isinstance(users, list), "Cannot establish workspace membership")
    matches = []
    for user in users:
        if "user_id" in person:
            matched = user.get("id") == person["user_id"]
        else:
            matched = str(user.get("email", "")).casefold() == person["email"].strip().casefold()
        if matched:
            matches.append(user)
    require(
        len(matches) == 1, "Person is missing or ambiguous in this workspace; no entries changed"
    )
    require(not matches[0].get("inactive", False), "Workspace member is inactive")
    return positive(matches[0].get("id"))


def ownership(entry, entry_id, workspace, user):
    require(
        isinstance(entry, dict) and entry.get("id") == entry_id, "Cannot establish entry identity"
    )
    require(
        entry.get("user_id", entry.get("uid")) == user
        and entry.get("workspace_id", entry.get("wid")) == workspace,
        "Entry does not belong to the explicitly named member/workspace; no entries changed",
    )
    if "uid" in entry:
        require(entry["uid"] == user, "Conflicting entry ownership evidence")
    if "wid" in entry:
        require(entry["wid"] == workspace, "Conflicting workspace evidence")
    return entry


def workspace_entry(api, workspace, user, entry_id):
    # Reports are workspace-scoped. A one-ID query avoids pagination ambiguity:
    # require exactly one row and one entry, rather than trusting a truncated page.
    rows = api.request(
        "POST",
        f"/reports/api/v3/workspace/{workspace}/search/time_entries",
        {
            "time_entry_ids": [entry_id],
            "user_ids": [user],
            "grouped": False,
            "page_size": 2,
            "rounding": 0,
        },
    )
    require(
        isinstance(rows, list) and len(rows) == 1,
        "Entry is missing or ambiguous in the named member's workspace report; no entries changed",
    )
    row = rows[0]
    require(
        isinstance(row, dict) and row.get("user_id") == user,
        "Report entry does not belong to the explicitly named member; no entries changed",
    )
    children = row.get("time_entries")
    require(
        isinstance(children, list) and len(children) == 1,
        "Report entry is missing or ambiguous; no entries changed",
    )
    child = children[0]
    require(
        isinstance(child, dict) and child.get("id") == entry_id,
        "Report returned an unexpected entry ID; no entries changed",
    )
    for source in (row, child):
        for field, expected in (
            ("user_id", user),
            ("uid", user),
            ("workspace_id", workspace),
            ("wid", workspace),
        ):
            require(
                field not in source or source[field] == expected,
                "Conflicting report ownership/workspace evidence; no entries changed",
            )
    require(
        "start" in child and "stop" in child and type(child.get("seconds")) is int,
        "Report lacks entry timing evidence; no entries changed",
    )
    timestamp(child["start"])
    if child["stop"] is not None:
        timestamp(child["stop"])
        require(child["seconds"] >= 0, "Invalid stopped-entry duration")
    # The response omits workspace_id: its evidence is the authenticated route,
    # not a default account. A null stop explicitly denotes a running entry.
    entry = {
        **child,
        "user_id": user,
        "workspace_id": workspace,
        "duration": -1 if child["stop"] is None else child["seconds"],
    }
    return ownership(entry, entry_id, workspace, user)


def update_body(entry, changes):
    result = dict(changes)
    if set(result) & {"start", "stop", "duration"}:
        start = timestamp(result.get("start", entry.get("start")))
        stop_value = result.get("stop", entry.get("stop"))
        if "duration" in result and "stop" not in result:
            from datetime import timedelta

            stop_value = (start + timedelta(seconds=result["duration"])).isoformat()
        if stop_value is not None:
            stop = timestamp(stop_value)
            duration = int((stop - start).total_seconds())
            require(duration >= 0, "Stop precedes start")
            require(
                "duration" not in changes or changes["duration"] == duration,
                "Inconsistent duration and start/stop",
            )
            result.update(start=start.isoformat(), stop=stop.isoformat(), duration=duration)
        else:
            require("start" not in result, "Stop a running entry before changing its start")
    return result


def run(request, api=None, now=None):
    request = plan(request)  # Refusal paths need neither credentials nor network.
    if api is None:
        token = key("TOGGL_API_TOKEN")
        auth = base64.b64encode((token + ":api_token").encode()).decode()
        api = Api("https://api.track.toggl.com", "Basic " + auth)
    workspace = request["workspace_id"]
    user = member(api, workspace, request["person"])
    ids = request["entry_ids"]
    # Resolve ALL entries from the workspace report before ANY mutation.
    entries = [workspace_entry(api, workspace, user, entry_id) for entry_id in ids]
    prefix = f"/api/v9/workspaces/{workspace}/time_entries/"
    if request["operation"] == "bulk-edit":
        response = api.request("PATCH", prefix + ",".join(map(str, ids)), request["operations"])
        success, failures = response.get("success"), response.get("failure")
        require(
            isinstance(success, list) and isinstance(failures, list),
            "Bulk response is incomplete; reconcile every target before retrying",
        )
        failed = [item["id"] for item in failures]
        require(
            set(success + failed) == set(ids) and len(success + failed) == len(ids),
            "Bulk response IDs are incomplete; reconcile targets before retrying",
        )
        # Do not echo vendor messages, which can contain request or credential text.
        if failures:
            raise Failure(
                "Bulk edit partially failed; do not retry successful IDs",
                workspace_id=workspace,
                user_id=user,
                success=success,
                failure=failed,
            )
        return {"workspace_id": workspace, "user_id": user, "success": success, "failure": []}
    entry = entries[0]
    if request["operation"] == "stop":
        require(
            entry.get("stop") is None
            and type(entry.get("duration")) is int
            and entry["duration"] < 0,
            "Entry is not running; nothing changed",
        )
        # Same workspace PUT/admin authority as update, without relying on PATCH stop.
        stop = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
        changes = update_body(entry, {"stop": stop.isoformat()})
    else:
        changes = update_body(entry, request["changes"])
    # Explicitly retain verified attribution even if the API defaults omitted user_id.
    changes.update(user_id=user, workspace_id=workspace)
    response = api.request("PUT", prefix + str(ids[0]), changes)
    try:
        ownership(response, ids[0], workspace, user)
    except Failure:
        raise Failure(
            "Write returned unexpected attribution; reconcile target before retrying",
            entry_id=ids[0],
            workspace_id=workspace,
            user_id=user,
        ) from None
    return {
        "workspace_id": workspace,
        "user_id": user,
        "entry_id": ids[0],
        "stop": response.get("stop"),
        "duration": response.get("duration"),
    }


if __name__ == "__main__":
    raise SystemExit(main(run))
