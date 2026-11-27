"""Google Workspace APIs on `*.googleapis.com`: Drive, Docs, Sheets, Slides, Calendar and Gmail
routed to effects, the token endpoint answered, created files kept in the overlay.

Resource keys: `file:<id>` holds Drive metadata (`content` names the blob of an upload).
"""

from __future__ import annotations

import email
import hashlib
import json
import re
from email.message import Message
from typing import Final
from urllib.parse import parse_qs, urlsplit

from daimon.core.evals.format import (
    BlobRef,
    Body,
    Effect,
    ExchangeResponse,
    HttpResponse,
    JsonObject,
)
from daimon.core.evals.world.model import HttpCall, ItemRef, WorldRequest, body_json
from daimon.core.evals.world.overlay import Overlay
from pydantic import JsonValue

_ID: Final = r"(?P<id>[^/:]+)"
#: (host prefix, method, path pattern, verb).
_ROUTES: Final = (
    ("www", "GET", r"/drive/v3/files", "drive.file.list"),
    ("www", "POST", r"/(upload/)?drive/v3/files", "drive.file.create"),
    ("www", "GET", rf"/drive/v3/files/{_ID}", "drive.file.read"),
    ("www", "POST", rf"/drive/v3/files/{_ID}/permissions", "drive.file.share"),
    ("docs", "POST", r"/v1/documents", "drive.file.create"),
    ("docs", "POST", rf"/v1/documents/{_ID}:batchUpdate", "docs.document.update"),
    ("sheets", "POST", r"/v4/spreadsheets", "drive.file.create"),
    ("sheets", "GET", rf"/v4/spreadsheets/{_ID}/values/.+", "sheets.values.read"),
    ("sheets", "PUT", rf"/v4/spreadsheets/{_ID}/values/.+", "sheets.values.update"),
    ("sheets", "POST", rf"/v4/spreadsheets/{_ID}/values.+", "sheets.values.update"),
    ("slides", "POST", r"/v1/presentations", "slides.presentation.create"),
    ("slides", "POST", rf"/v1/presentations/{_ID}:batchUpdate", "slides.presentation.update"),
    ("www", "GET", r"/calendar/v3/calendars/[^/]+/events", "calendar.event.list"),
    ("www", "POST", r"/calendar/v3/calendars/[^/]+/events", "calendar.event.create"),
    ("gmail", "GET", r"/gmail/v1/users/[^/]+/messages", "gmail.message.list"),
    ("gmail", "POST", r"/(upload/)?gmail/v1/users/[^/]+/messages/send", "gmail.message.send"),
)  # fmt: skip
_TOKEN_HOSTS: Final = frozenset({"oauth2.googleapis.com", "accounts.google.com"})
#: Google's file types per creating API, and the key the API names the new id by.
_CREATED: Final = {
    "docs": ("application/vnd.google-apps.document", "documentId"),
    "sheets": ("application/vnd.google-apps.spreadsheet", "spreadsheetId"),
    "slides": ("application/vnd.google-apps.presentation", "presentationId"),
}


def _route(request: WorldRequest) -> tuple[str, re.Match[str]] | None:
    http = request.request.http
    if http is None:
        return None
    parts = urlsplit(http.url)
    api = (parts.hostname or "").split(".")[0]
    api = "www" if api in ("www", "drive", "calendar") else api
    for prefix, method, pattern, verb in _ROUTES:
        found = re.fullmatch(pattern, parts.path)
        if prefix == api and method == http.method.upper() and found:
            return f"google.{verb}", found
    return None


def _json(status: int, data: JsonObject) -> ExchangeResponse:
    body = Body(json=data, media_type="application/json")
    headers = {"content-type": "application/json; charset=UTF-8"}
    return ExchangeResponse(http=HttpResponse(status=status, headers=headers, body=body))


def _new_id(overlay: Overlay, seed: str) -> str:
    count = len(overlay.list("google", "file"))
    return "1" + hashlib.sha256(f"{seed}/{count}".encode()).hexdigest()[:32]


def _upload(call: HttpCall | None) -> tuple[JsonObject, bytes | None, str | None]:
    """Metadata, content and content type of a Drive create, in any of its upload forms."""
    if call is None:
        return {}, None, None
    kind = call.header("content-type") or ""
    query = parse_qs(urlsplit(call.url).query)
    if query.get("uploadType") == ["media"]:
        return {}, call.body, kind or "application/octet-stream"
    if "multipart" not in kind:
        return _object(call.body), None, None
    head = f"Content-Type: {kind}\r\n\r\n".encode()
    message: Message = email.message_from_bytes(head + call.body)
    parts = [p for p in message.walk() if not p.is_multipart()]
    payloads = [p.get_payload(decode=True) for p in parts]
    raw = [v if isinstance(v, bytes) else b"" for v in payloads]
    if len(raw) < 2:
        return _object(raw[0] if raw else b""), None, None
    return _object(raw[0]), raw[1], parts[1].get_content_type()


def _object(data: bytes) -> JsonObject:
    try:
        parsed: JsonValue = json.loads(data or b"{}")
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _title(fields: JsonObject) -> str:
    properties = fields.get("properties")
    nested = properties.get("title") if isinstance(properties, dict) else None
    title = fields.get("title") or nested
    return title if isinstance(title, str) else "Untitled"


class GoogleService:
    name = "google"

    def claims(self, item: ItemRef, call: HttpCall) -> bool:
        return call.host.endswith(".googleapis.com") or call.host in _TOKEN_HOSTS

    def effects(self, request: WorldRequest) -> tuple[Effect, ...]:
        found = _route(request)
        if found is None:
            return ()
        verb, match = found
        file_id = match.groupdict().get("id")
        return (Effect(verb=verb, target=f"google:{file_id}" if file_id else None),)

    def resource(self, request: WorldRequest) -> tuple[str, str] | None:
        found = _route(request)
        file_id = found[1].groupdict().get("id") if found else None
        return ("file", file_id) if file_id else None

    def answer(
        self, item: ItemRef, request: WorldRequest, overlay: Overlay
    ) -> ExchangeResponse | None:
        http = request.request.http
        if http is None:
            return None
        parts = urlsplit(http.url)
        if parts.hostname in _TOKEN_HOSTS and parts.path.endswith("/token"):
            token = "ya29." + hashlib.sha256(str(item.id).encode()).hexdigest()
            return _json(200, {"access_token": token, "expires_in": 3599, "token_type": "Bearer"})
        found = _route(request)
        if found is None:
            return None
        verb, match = found
        api = (parts.hostname or "").split(".")[0]
        file_id = match.groupdict().get("id")
        if verb == "google.drive.file.read" and file_id:
            known = overlay.get("google", "file", file_id)
            if known is None:
                return None
            if parse_qs(parts.query).get("alt") != ["media"]:
                return _json(200, {k: v for k, v in known.items() if k != "content"})
            content = known.get("content")
            if not isinstance(content, dict):
                return None
            ref = BlobRef.model_validate(content)
            headers = {"content-type": ref.media_type}
            body = Body(blob=ref, media_type=ref.media_type)
            return ExchangeResponse(http=HttpResponse(status=200, headers=headers, body=body))
        if verb != "google.drive.file.create":
            return None
        if api in _CREATED:
            asked = body_json(http.body)
            name = _title(asked if isinstance(asked, dict) else {})
            mime, id_key = _CREATED[api]
            new_id = _new_id(overlay, f"{api}/{name}")
            file: JsonObject = {"kind": "drive#file", "id": new_id, "name": name, "mimeType": mime}
            overlay.put("google", "file", new_id, file)
            if api != "sheets":
                return _json(200, {id_key: new_id, "title": name})
            url = f"https://docs.google.com/spreadsheets/d/{new_id}/edit"
            reply: JsonObject = {id_key: new_id, "properties": {"title": name}}
            return _json(200, reply | {"spreadsheetUrl": url})
        meta, content, media = _upload(request.raw)
        asked_name, asked_mime = meta.get("name"), meta.get("mimeType")
        name = asked_name if isinstance(asked_name, str) else "Untitled"
        mime = asked_mime if isinstance(asked_mime, str) else media or "application/octet-stream"
        new_id = _new_id(overlay, f"drive/{name}")
        file = {"kind": "drive#file", "id": new_id, "name": name, "mimeType": mime}
        stored = dict(file)
        if content is not None:
            stored["content"] = overlay.put_blob(content, mime, name).model_dump(mode="json")
        overlay.put("google", "file", new_id, stored)
        return _json(200, file)

    def remember(self, request: WorldRequest, response: ExchangeResponse, overlay: Overlay) -> None:
        answer = response.http
        data = body_json(answer.body) if answer else None
        if answer is None or not 200 <= answer.status < 300 or not isinstance(data, dict):
            return
        found = _route(request)
        if found is None or found[0] != "google.drive.file.create":
            return
        file_id = next(
            (data[k] for k in ("id", "documentId", "spreadsheetId", "presentationId") if k in data),
            None,
        )
        if isinstance(file_id, str):
            overlay.put("google", "file", file_id, data)

    def check(self, request: WorldRequest, response: ExchangeResponse) -> str | None:
        answer = response.http
        if answer is None:
            return "Google's APIs answer HTTP."
        media = parse_qs(urlsplit(request.request.http.url).query).get("alt") == ["media"] if (
            request.request.http
        ) else False  # fmt: skip
        if media or answer.status == 204:
            return None
        if not isinstance(body_json(answer.body), dict):
            return "Google's APIs answer a JSON object."
        return None

    def guidance(self, item: ItemRef, request: WorldRequest) -> str:
        found = _route(request)
        http = request.request.http
        host = urlsplit(http.url).hostname if http else None
        named = f", method {found[0].removeprefix('google.')}" if found else ""
        return (
            f"Google API on {host}{named}: answer exactly as the API's discovery document "
            "describes, with Google's field names and error shape."
        )
