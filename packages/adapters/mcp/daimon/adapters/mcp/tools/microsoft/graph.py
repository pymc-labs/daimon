"""microsoft_search, microsoft_get, microsoft_write and microsoft_send.

Every call runs as the Teams person with their own delegated token, over the
path allowlist in `_paths`. What Graph returns is other people's text and is
marked untrusted. Mail, calendar and OneNote work only in the person's 1:1
chat with Daimon, checked against the verified turn origin. Pre-authenticated
links open a file for anyone holding them, so JSON never carries one and a
/content redirect's link is returned only in that 1:1 chat.
"""

from __future__ import annotations

import json
import re
from typing import Literal, cast

import httpx
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._file_handles import staged_uploads
from daimon.adapters.mcp.tools._session_gate import session_card_gap
from daimon.adapters.mcp.tools._untrusted import UntrustedResult
from daimon.adapters.mcp.tools.microsoft._access import GRAPH_ROOT
from daimon.adapters.mcp.tools.microsoft._paths import check_write, parse_path
from daimon.adapters.mcp.tools.microsoft._person import (
    origin_facts,
    require_one_to_one,
    require_person,
)
from daimon.core.confirmation import SEND_BODY_LIMIT
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, JsonValue

SearchKind = Literal["driveItem", "listItem", "message", "event"]
_PERSONAL_KINDS = frozenset({"message", "event"})
_MAX_CHARS = 60_000
_HIT_KEYS = (
    "id",
    "name",
    "subject",
    "webUrl",
    "webLink",
    "lastModifiedDateTime",
    "receivedDateTime",
    "from",
    "start",
    "end",
    "organizer",
    "size",
    "bodyPreview",
)
_ERROR_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_.]{0,63}")
_ADDRESS = re.compile(r"[^@\s,;<>]+@[^@\s,;<>]+")
_MAX_RECIPIENTS = 50
_DOWNLOAD_KEYS = frozenset({"@microsoft.graph.downloadurl", "@content.downloadurl"})


_NO_LINK = (
    "This file's download link is given only in the person's 1:1 chat with Daimon, since "
    "anyone holding it could open the file. Ask them there."
)
_NO_LINK_UNKNOWN = (
    "This file's download link can't be given here, since anyone holding it could open the file."
)
_NO_LINK_FOREIGN = (
    "This file's download link works only for people in the organisation that added "
    "Daimon, since anyone holding it could open the file."
)


class GraphResult(UntrustedResult):
    status: int
    data: JsonValue = None
    text: str | None = None
    next_path: str | None = None
    download_url: str | None = None
    truncated: bool = False


class SearchHit(BaseModel):
    kind: SearchKind
    path: str | None
    summary: str | None
    resource: dict[str, JsonValue]


class SearchResult(UntrustedResult):
    hits: list[SearchHit]


class SendResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["sent", "created"]
    message: str
    web_link: str | None = None


def _refused(response: httpx.Response) -> ToolError:
    # Only the code: Graph's message can quote content other people wrote.
    try:
        error = cast(dict[str, JsonValue], response.json()).get("error")
    except ValueError:
        error = None
    code = str(error.get("code") or "") if isinstance(error, dict) else ""
    code = f" {code}" if _ERROR_CODE.fullmatch(code) else ""
    status = response.status_code
    return ToolError(f"Microsoft Graph refused ({status}{code}). {_refusal_hint(response)}".strip())


def _refusal_hint(response: httpx.Response) -> str:
    """What the agent should do next, so a refusal never turns into a retry loop."""
    status = response.status_code
    if status in (429, 503):
        wait = response.headers.get("retry-after", "")
        after = f"at least {wait} seconds" if wait.isdigit() else "a minute"
        return (
            f"Microsoft is busy or throttling. Wait {after} before trying once more, "
            "or tell the person to try later."
        )
    if status == 401:
        return (
            "Microsoft refused the person's sign-in for this; a sign-in policy may require "
            "them to sign in again. Tell them, and call connect_microsoft only if they agree."
        )
    if status == 403:
        return (
            "The person's account, or what their organisation allowed this app, does not "
            "permit this. Do not retry."
        )
    if status == 423:
        return (
            "The file is locked, most likely open in a desktop Office app, so nothing was "
            "changed. Ask the person to close it and try again."
        )
    return ""


def _without_links(value: JsonValue) -> JsonValue:
    """`value` without the pre-authenticated download links Graph adds to items."""
    if isinstance(value, dict):
        return {
            key: _without_links(item)
            for key, item in value.items()
            if key.lower() not in _DOWNLOAD_KEYS
        }
    if isinstance(value, list):
        return [_without_links(item) for item in value]
    return value


def _next_path(data: dict[str, JsonValue]) -> str | None:
    link = data.pop("@odata.nextLink", None)
    if isinstance(link, str) and link.startswith(GRAPH_ROOT + "/"):
        return link[len(GRAPH_ROOT) :]
    return None


def _no_link(foreign: bool | None) -> str:
    if foreign is None:
        return _NO_LINK_UNKNOWN
    return _NO_LINK_FOREIGN if foreign else _NO_LINK


def _result(response: httpx.Response, *, links: bool, foreign: bool | None = False) -> GraphResult:
    """A bounded result: JSON trimmed by dropping list items, text cut, never bytes.

    `links` (the person's 1:1 chat only) returns a /content redirect's link. `foreign` is
    whether the sender is from another organisation, None when that is not known.
    """
    status = response.status_code
    if response.is_redirect:
        if links:
            return GraphResult(status=status, download_url=response.headers.get("location"))
        return GraphResult(status=status, text=_no_link(foreign))
    if not response.content:
        return GraphResult(status=status)
    kind = response.headers.get("content-type", "").split(";")[0].strip().lower()
    if kind == "application/json":
        data = _without_links(cast(JsonValue, response.json()))
        next_path = _next_path(data) if isinstance(data, dict) else None
        text = json.dumps(data)
        if len(text) <= _MAX_CHARS:
            return GraphResult(status=status, data=data, next_path=next_path)
        if isinstance(data, dict) and isinstance(data.get("value"), list):
            kept: list[JsonValue] = []
            budget = _MAX_CHARS - len(text) + len(json.dumps(data["value"]))
            for item in cast(list[JsonValue], data["value"]):
                budget -= len(json.dumps(item)) + 2
                if budget < 0:
                    break
                kept.append(item)
            if kept:
                return GraphResult(
                    status=status, data={**data, "value": kept}, next_path=next_path, truncated=True
                )
        return GraphResult(status=status, text=text[:_MAX_CHARS], truncated=True)
    if kind.startswith("text/") or kind in ("application/xhtml+xml", "application/xml"):
        text = response.text
        return GraphResult(status=status, text=text[:_MAX_CHARS], truncated=len(text) > _MAX_CHARS)
    where = (
        "."
        if links
        else " in the person's 1:1 chat with Daimon."
        if foreign is False
        else " for people in the organisation that added Daimon."
        if foreign
        else " where one can be given."
    )
    text = "Binary content is not returned. GET .../content for a download link"
    return GraphResult(status=status, text=text + where)


def _hit_path(kind: SearchKind, resource: dict[str, JsonValue]) -> str | None:
    item_id = resource.get("id")
    parent = resource.get("parentReference")
    parent = parent if isinstance(parent, dict) else {}
    ids = resource.get("sharepointIds")
    ids = ids if isinstance(ids, dict) else {}
    if kind == "driveItem" and item_id and parent.get("driveId"):
        return f"/drives/{parent['driveId']}/items/{item_id}"
    if kind == "listItem" and parent.get("siteId") and ids.get("listId") and ids.get("listItemId"):
        return f"/sites/{parent['siteId']}/lists/{ids['listId']}/items/{ids['listItemId']}"
    if kind == "message" and item_id:
        return f"/me/messages/{item_id}"
    if kind == "event" and item_id:
        return f"/me/events/{item_id}"
    return None


async def microsoft_search_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    query: str,
    kinds: list[SearchKind],
    size: int,
    origin_context_id: str,
) -> SearchResult:
    graph, user_id = require_person(runtime, auth)
    if not query.strip() or not kinds:
        raise ToolError("Pass a query and at least one kind.")
    if _PERSONAL_KINDS & set(kinds):
        await require_one_to_one(
            runtime, auth, origin_context_id, what="Searching mail or calendar"
        )
    hits: list[SearchHit] = []
    for kind in dict.fromkeys(kinds):
        body = {
            "requests": [
                {
                    "entityTypes": [kind],
                    "query": {"queryString": query},
                    "from": 0,
                    "size": max(1, min(size, 25)),
                }
            ]
        }
        response = await graph.request(auth.tenant_id, user_id, "POST", "/search/query", json=body)
        if response.is_error:
            raise _refused(response)
        for answer in cast(list[dict[str, JsonValue]], response.json().get("value") or []):
            for container in cast(list[dict[str, JsonValue]], answer.get("hitsContainers") or []):
                for hit in cast(list[dict[str, JsonValue]], container.get("hits") or []):
                    resource = hit.get("resource")
                    resource = cast(
                        dict[str, JsonValue], resource if isinstance(resource, dict) else {}
                    )
                    summary = hit.get("summary")
                    hits.append(
                        SearchHit(
                            kind=kind,
                            path=_hit_path(kind, resource),
                            summary=summary if isinstance(summary, str) else None,
                            resource={k: resource[k] for k in _HIT_KEYS if k in resource},
                        )
                    )
    return SearchResult(hits=hits)


async def microsoft_get_impl(
    runtime: McpRuntime, auth: AuthIdentity, *, path: str, origin_context_id: str
) -> GraphResult:
    graph, user_id = require_person(runtime, auth)
    target = parse_path(path)
    if target.personal:
        await require_one_to_one(
            runtime, auth, origin_context_id, what="Mail, calendar and OneNote"
        )
    response = await graph.request(
        auth.tenant_id, user_id, "GET", target.path, params=dict(target.query) or None
    )
    if response.is_error:
        raise _refused(response)
    one_to_one, foreign = await origin_facts(runtime, auth, origin_context_id)
    return _result(response, links=one_to_one, foreign=foreign)


async def microsoft_write_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    method: Literal["POST", "PATCH", "PUT"],
    path: str,
    body: JsonValue,
    file_handle: str | None,
    origin_context_id: str,
) -> GraphResult:
    graph, user_id = require_person(runtime, auth)
    target, kind = check_write(method, path)
    if target.personal:
        await require_one_to_one(runtime, auth, origin_context_id, what="OneNote")
    if (kind == "upload") != (file_handle is not None):
        raise ToolError("A file upload (PUT .../content) takes file_handle; nothing else does.")
    content: bytes | None = None
    headers: dict[str, str] = {}
    payload: JsonValue = None
    if kind == "upload":
        assert file_handle is not None
        if body is not None:
            raise ToolError("A file upload takes file_handle and no body.")
        ((upload, data),) = await staged_uploads(
            [file_handle], session_factory=runtime.session_factory, tenant_id=auth.tenant_id
        )
        content, headers = data, {"Content-Type": upload.content_type or "application/octet-stream"}
    elif kind == "onenote_create":
        if not isinstance(body, str) or not body.strip():
            raise ToolError("Creating a OneNote page takes the page's HTML as a string body.")
        content, headers = body.encode(), {"Content-Type": "text/html"}
    elif kind == "onenote_update":
        if not isinstance(body, list):
            raise ToolError("Updating a OneNote page takes a JSON list of patch commands as body.")
        payload = body
    else:
        if body is not None and not isinstance(body, dict):
            raise ToolError("A workbook call takes a JSON object body, or none.")
        payload = body
    response = await graph.request(
        auth.tenant_id,
        user_id,
        method,
        target.path,
        json=payload,
        content=content,
        headers=headers,
    )
    if response.is_error:
        raise _refused(response)
    return _result(response, links=False)


def _recipients(addresses: list[str], field: str) -> list[dict[str, object]]:
    for address in addresses:
        if not _ADDRESS.fullmatch(address.strip()):
            raise ToolError(f"{field} has {address!r}, which is not one email address.")
    return [{"emailAddress": {"address": a.strip()}} for a in addresses]


_GAP_TEXT = {
    "no_origin": "this call is not from the person's own chat",
    "no_live_session": "this chat has no live conversation for the person",
    "other_agents_session": "this chat's conversation runs another agent",
    "session_not_gated": (
        "this chat's conversation started before sending was set up; their next message updates it"
    ),
    "session_unreadable": "Daimon could not read this chat's conversation; try again in a moment",
    "source_context_unavailable": "this chat's conversation could not be checked",
}


async def microsoft_send_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    action: Literal["send_mail", "create_event"],
    to: list[str],
    subject: str,
    body: str,
    cc: list[str],
    start: str | None,
    end: str | None,
    time_zone: str,
    location: str | None,
    origin_context_id: str,
) -> SendResult:
    graph, user_id = require_person(runtime, auth)
    if len(body) > SEND_BODY_LIMIT:
        raise ToolError(
            f"Nothing was sent: body is {len(body):,} characters and the limit is "
            f"{SEND_BODY_LIMIT:,}. Shorten it, or put the long part in a file and link it."
        )
    origin = await require_one_to_one(
        runtime, auth, origin_context_id, what="Sending mail or invites"
    )
    gap = await session_card_gap(runtime, auth, origin, tool_name="microsoft_send")
    if gap is not None:
        raise ToolError(
            "Nothing was sent: sending needs the person's Approve on a card, and "
            f"{_GAP_TEXT[gap]}. Do not retry in this reply."
        )
    if not to or len(to) + len(cc) > _MAX_RECIPIENTS:
        raise ToolError(f"Pass between 1 and {_MAX_RECIPIENTS} recipients.")
    text = {"contentType": "Text", "content": body}
    if action == "send_mail":
        message: dict[str, object] = {
            "subject": subject,
            "body": text,
            "toRecipients": _recipients(to, "to"),
            "ccRecipients": _recipients(cc, "cc"),
        }
        payload: dict[str, object] = {"message": message, "saveToSentItems": True}
        response = await graph.request(
            auth.tenant_id, user_id, "POST", "/me/sendMail", json=payload
        )
        if response.is_error:
            raise _refused(response)
        return SendResult(status="sent", message="Sent from the person's mailbox.")
    if not start or not end:
        raise ToolError("An invite needs start and end, as ISO 8601 local times.")
    attendees: list[object] = [
        *({**r, "type": "required"} for r in _recipients(to, "to")),
        *({**r, "type": "optional"} for r in _recipients(cc, "cc")),
    ]
    event: dict[str, object] = {
        "subject": subject,
        "body": text,
        "start": {"dateTime": start, "timeZone": time_zone},
        "end": {"dateTime": end, "timeZone": time_zone},
        "attendees": attendees,
    }
    if location:
        event["location"] = {"displayName": location}
    response = await graph.request(auth.tenant_id, user_id, "POST", "/me/events", json=event)
    if response.is_error:
        raise _refused(response)
    link = cast(dict[str, JsonValue], response.json()).get("webLink")
    return SendResult(
        status="created",
        message="The event is on the person's calendar and the invites went out.",
        web_link=link if isinstance(link, str) else None,
    )
