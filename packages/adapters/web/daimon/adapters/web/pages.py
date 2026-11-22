"""`/a/<public_id>/<slug>`: artifact pages, their sandboxed content and their API.

The shell is a small page on this origin that holds the sign-in and frames the page's
content in a sandbox with an opaque origin, on a ticket. Page code reaches Daimon only
through the shell's postMessage bridge, which calls `_api`; every call re-admits the
viewer and re-checks the page's capabilities. Not-found, expired and no-access answer
alike: a sign-in page to someone signed out, a not-found page to anyone else.
"""

from __future__ import annotations

import hmac
import html
import json
import secrets
import time
import uuid
from collections import deque
from datetime import timedelta
from importlib.resources import files
from string import Template
from typing import Final, Literal
from urllib.parse import quote

import structlog
from daimon.adapters.web.chat import chattable_agents, open_thread, post_message
from daimon.adapters.web.runtime import WebRuntime
from daimon.adapters.web.viewer import PageVisitor, page_on, person_viewer, resolve_viewer
from daimon.core.artifact_pages.access import Role, admit, can_edit, is_link, sender_of
from daimon.core.artifact_pages.content import SANDBOX, content_policy, inject_runtime, shell_policy
from daimon.core.artifact_pages.documents import (
    DocumentWrite,
    build_list_query,
    check_doc_id,
    check_list_size,
    check_page_collection,
)
from daimon.core.artifact_pages.events import build_event
from daimon.core.artifact_pages.keys import new_key_id, page_event_key
from daimon.core.artifact_pages.model import ArtifactError, ErrorCode, Limits, page_url
from daimon.core.artifact_pages.tickets import Ticket, link_digest, open_ticket, seal
from daimon.core.config import origin_of
from daimon.core.continuity.page_events import sign_page_event
from daimon.core.stores import artifact_pages as artifact_store
from daimon.core.stores import watches as watches_store
from daimon.core.stores import web_chat as chat_store
from daimon.core.stores.domain import ArtifactRow
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)

_ASSETS: Final = files("daimon.adapters.web") / "page_assets"
_SHELL: Final = Template((_ASSETS / "shell.html").read_text(encoding="utf-8"))
_RUNTIME_JS: Final = (_ASSETS / "runtime.js").read_text(encoding="utf-8")
# As long as a tab stays open on a page that reloads its frame; each load re-admits.
CONTENT_TTL: Final = timedelta(hours=12)
SOURCE_TTL: Final = timedelta(minutes=15)
MAX_API_BODY: Final = 256 * 1024
LINK_HEADER: Final = "x-daimon-link"
MAX_THREAD: Final = 50
_STATUS: Final[dict[ErrorCode, int]] = {
    "invalid": 422,
    "unauthenticated": 401,
    "not_found": 404,
    "forbidden": 403,
    "too_large": 413,
    "rate_limited": 429,
    "unavailable": 503,
}
_CODE_OF: Final[dict[int, ErrorCode]] = {
    401: "unauthenticated",
    403: "forbidden",
    404: "not_found",
    409: "rate_limited",
    429: "rate_limited",
    503: "unavailable",
}
_HEADERS: Final = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "SAMEORIGIN",
}
_WRITES: Final = frozenset({"db.set", "db.add", "db.delete"})
_NO_PAGE = "There is no page here, or you can't open it."

_METHODS: Final = frozenset(
    {"db.get", "db.list", "db.set", "db.add", "db.delete", "db.revision", "notify", "thread"}
)


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _Doc(_Params):
    path: str = Field(max_length=200)


class _List(_Params):
    collection: str = Field(max_length=64)
    orderBy: str | None = None  # the page API's name
    direction: Literal["asc", "desc"] = "asc"
    limit: int | None = None


class _Set(_Doc):
    data: dict[str, JsonValue]
    write: Literal["set", "merge", "update"] = "set"


class _Add(_Params):
    collection: str = Field(max_length=64)
    data: dict[str, JsonValue]


class _Notify(_Params):
    event: str = Field(max_length=64)
    data: JsonValue = None


class _Ask(_Params):
    text: str = Field(min_length=1, max_length=100_000)


class RateLimiter:
    """A sliding one-minute window per key, in this one web process."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = {}

    def allow(self, key: str, *, limit: int, now: float) -> bool:
        if len(self._hits) > 10_000:
            self._hits = {k: v for k, v in self._hits.items() if v and v[-1] > now - 60}
        hits = self._hits.setdefault(key, deque())
        while hits and hits[0] <= now - 60:
            hits.popleft()
        if len(hits) >= limit:
            return False
        hits.append(now)
        return True


def _page(title: str, text: str, status: int) -> HTMLResponse:
    return HTMLResponse(
        f'<!doctype html><meta charset="utf-8"><meta name="viewport" '
        f'content="width=device-width"><title>{title}</title><body style="font:16px/1.5 '
        f'system-ui,sans-serif;max-width:32rem;margin:15vh auto;padding:0 1rem">'
        f"<h1>{title}</h1><p>{html.escape(text)}</p>",
        status_code=status,
        headers={
            **_HEADERS,
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
        },
    )


def _error(error: ArtifactError) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": error.code, "message": str(error)}},
        status_code=_STATUS[error.code],
        headers=_HEADERS,
    )


def _capabilities(artifact: ArtifactRow) -> list[str]:
    on = (("db", artifact.data != "off"), ("events", artifact.events), ("ask", artifact.ask))
    return [name for name, enabled in on if enabled]


def _check_capability(artifact: ArtifactRow, method: str) -> None:
    """What the page may do, the same for every viewer; runtime.js refuses the same."""
    if method == "notify":
        if not artifact.events:
            raise ArtifactError("forbidden", "This page sends no events.")
    elif method in ("ask", "thread"):
        if not artifact.ask:
            raise ArtifactError("forbidden", "This page cannot ask the agent.")
    elif artifact.data == "off":
        raise ArtifactError("forbidden", "This page has no data.")
    elif method in _WRITES and artifact.data != "write":
        raise ArtifactError("forbidden", "This page's data is read-only.")


def _page_doc(path: str) -> tuple[str, str]:
    collection, slash, doc_id = path.partition("/")
    if not slash:
        raise ArtifactError("invalid", "A document path is collection/id.")
    return check_page_collection(collection), check_doc_id(doc_id)


async def _read_capped(request: Request) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_API_BODY:
            raise ArtifactError("too_large", "The call is too large.")
    return bytes(body) or b"{}"


def pages_router(runtime: WebRuntime) -> APIRouter:
    router = APIRouter()
    origin = origin_of(str(runtime.web.public_url))
    limiter = RateLimiter()

    async def open_page(
        db: AsyncSession, request: Request, public_id: str, link: str | None
    ) -> tuple[ArtifactRow | None, PageVisitor, Role]:
        """The page, its viewer and their role: `none` when the page is missing, expired,
        switched off or not theirs."""
        artifact = await artifact_store.get_by_public_id(db, public_id)
        visitor = await resolve_viewer(runtime, db, request, artifact, link=link)
        if artifact is None or not await page_on(runtime, db, artifact):
            return artifact, visitor, "none"
        return artifact, visitor, admit(artifact, visitor.viewer, runtime.now())

    async def admitted(
        db: AsyncSession, request: Request, public_id: str
    ) -> tuple[ArtifactRow, PageVisitor]:
        """For `_api`: the page and viewer, else the refusal everyone not let in gets."""
        artifact, visitor, role = await open_page(
            db, request, public_id, request.headers.get(LINK_HEADER)
        )
        if artifact is None or role == "none":
            if not visitor.signed_in:
                raise ArtifactError("unauthenticated", "Sign in to use this page.")
            raise ArtifactError("not_found", _NO_PAGE)
        return artifact, visitor

    def throttle(
        request: Request, artifact: ArtifactRow, visitor: PageVisitor, *, ask: bool
    ) -> None:
        """Per account; an anonymous viewer per page and address."""
        account = visitor.viewer.account_id
        client = request.client.host if request.client else ""
        key = str(account) if account else f"{artifact.public_id}:{client}"
        now = time.monotonic()
        allowed = limiter.allow(key, limit=Limits.API_PER_MINUTE, now=now) and (
            not ask or limiter.allow(f"ask:{key}", limit=Limits.ASK_PER_MINUTE, now=now)
        )
        if not allowed:
            raise ArtifactError("rate_limited", "Too many calls; slow down.")

    @router.get("/a/{public_id}/_content", response_model=None)
    async def content(public_id: str, t: str = "") -> Response:  # pyright: ignore[reportUnusedFunction]
        """On the shell's ticket, never the cookie: the frame's origin is opaque."""
        now = runtime.now()
        ticket = open_ticket(runtime.fernet, t, purpose="content", ttl=CONTENT_TTL, now=now)
        async with runtime.sessionmaker() as db:
            artifact = await artifact_store.get_by_public_id(db, public_id)
            if ticket is None or artifact is None or ticket.artifact_id != artifact.id:
                return _page("Page not found", _NO_PAGE, 404)
            # The link as it is now: replacing it retires the tickets minted on the old one.
            current = artifact.link_secret
            link = (
                current
                if current is not None
                and ticket.link_digest is not None
                and hmac.compare_digest(link_digest(current), ticket.link_digest)
                else None
            )
            viewer = await person_viewer(db, artifact, ticket.person_id, link=link)
            version = await artifact_store.get_version(db, artifact.id, ticket.version)
            role = admit(artifact, viewer, now)
            # An older version only for those who manage the page, as the shell mints it.
            old = ticket.version != artifact.current_version and not can_edit(role)
            if version is None or role == "none" or old or not await page_on(runtime, db, artifact):
                return _page("Page not found", _NO_PAGE, 404)
        capabilities = _capabilities(artifact)
        shown = artifact.access != "public" and capabilities and viewer.account_id is not None
        boot: dict[str, JsonValue] = {
            "artifact": {
                "id": artifact.public_id,
                "slug": artifact.slug,
                "title": artifact.title,
                "version": version.version,
                "data": artifact.data,
                "events": artifact.events,
                "ask": artifact.ask,
            },
            "capabilities": list(capabilities),
            "viewer": {"name": viewer.name, "kind": viewer.kind} if shown else None,
            "shellOrigin": origin,
        }
        return HTMLResponse(
            inject_runtime(version.html, _RUNTIME_JS, boot),
            headers={**_HEADERS, "Content-Security-Policy": content_policy()},
        )

    @router.get("/a/{public_id}/_source", response_model=None)
    async def source(public_id: str, t: str = "") -> Response:  # pyright: ignore[reportUnusedFunction]
        """A version's HTML as text, behind the short-lived link the source tool gives."""
        now = runtime.now()
        ticket = open_ticket(runtime.fernet, t, purpose="source", ttl=SOURCE_TTL, now=now)
        async with runtime.sessionmaker() as db:
            artifact = await artifact_store.get_by_public_id(db, public_id)
            version = (
                None
                if ticket is None or artifact is None or ticket.artifact_id != artifact.id
                else await artifact_store.get_version(db, artifact.id, ticket.version)
            )
        if version is None:
            return PlainTextResponse(
                "This link expired; ask for the source again.", 404, headers=_HEADERS
            )
        return PlainTextResponse(
            version.html,
            headers={**_HEADERS, "Content-Security-Policy": "default-src 'none'; sandbox"},
        )

    @router.post("/a/{public_id}/_api/ask", response_model=None)
    async def ask(public_id: str, request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        """A turn as the viewer in their own thread for this page, streamed as chat is."""
        try:
            params = _Ask.model_validate_json(await _read_capped(request))
            async with runtime.sessionmaker() as db:
                artifact, visitor = await admitted(db, request, public_id)
            _check_capability(artifact, "ask")
            caller = visitor.caller
            if caller is None or "chat.use" not in caller.capabilities:
                raise ArtifactError("forbidden", "You can't chat here. Ask an admin.")
            throttle(request, artifact, visitor, ask=True)
            agents = await chattable_agents(runtime, caller, only=artifact.agent_id)
            if not agents:
                raise ArtifactError("forbidden", "This page's agent isn't available to you.")
            async with runtime.sessionmaker.begin() as db:
                thread = await chat_store.get_ask_thread(
                    db, person_id=caller.person.id, artifact_id=artifact.id
                )
                if thread is None:
                    thread = await open_thread(
                        db, caller, agents[0], title=artifact.title, artifact_id=artifact.id
                    )
            return await post_message(
                runtime, caller, thread, message_id=uuid.uuid4(), content=params.text
            )
        except ArtifactError as exc:
            return _error(exc)
        except ValidationError:
            return _error(ArtifactError("invalid", "Ask takes {text}."))
        except HTTPException as exc:
            return _error(ArtifactError(_CODE_OF.get(exc.status_code, "invalid"), str(exc.detail)))

    @router.post("/a/{public_id}/_api/{method}", response_model=None)
    async def api(public_id: str, method: str, request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        try:
            body = await _read_capped(request)
            async with runtime.sessionmaker.begin() as db:
                artifact, visitor = await admitted(db, request, public_id)
                throttle(request, artifact, visitor, ask=False)
                return JSONResponse(
                    await call(db, artifact, visitor, method, body), headers=_HEADERS
                )
        except ArtifactError as exc:
            return _error(exc)
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(p) for p in first["loc"])
            return _error(
                ArtifactError("invalid", f"Bad arguments to {method}: {where}: {first['msg']}")
            )

    async def call(
        db: AsyncSession, artifact: ArtifactRow, visitor: PageVisitor, method: str, body: bytes
    ) -> dict[str, JsonValue]:
        if method not in _METHODS:
            raise ArtifactError("not_found", f"No method {method}.")
        _check_capability(artifact, method)
        viewer, now = visitor.viewer, runtime.now()
        # Reads report the revision read with the page, before their data.
        revision = artifact.data_revision
        match method:
            case "db.get":
                collection, doc_id = _page_doc(_Doc.model_validate_json(body).path)
                doc = await artifact_store.get_doc(db, artifact.id, collection, doc_id)
                return {
                    "id": doc_id,
                    "data": None if doc is None else doc.data,
                    "revision": revision,
                }
            case "db.list":
                p = _List.model_validate_json(body)
                query = build_list_query(
                    check_page_collection(p.collection),
                    order_by=p.orderBy,
                    descending=p.direction == "desc",
                    limit=p.limit,
                )
                docs = await artifact_store.list_docs(db, artifact.id, query)
                check_list_size(sum(d.size_bytes for d in docs))
                return {
                    "docs": [{"id": d.doc_id, "data": d.data} for d in docs],
                    "revision": revision,
                }
            case "db.set" | "db.add":
                if method == "db.set":
                    p = _Set.model_validate_json(body)
                    (collection, doc_id), write = (
                        _page_doc(p.path),
                        DocumentWrite(mode=p.write, data=p.data),
                    )
                else:
                    a = _Add.model_validate_json(body)
                    collection, doc_id = check_page_collection(a.collection), secrets.token_hex(10)
                    write = DocumentWrite(data=a.data)
                _, revision = await artifact_store.write_doc(
                    db,
                    artifact,
                    collection,
                    doc_id,
                    write,
                    by=viewer.name,
                    account_id=viewer.account_id,
                    now=now,
                )
                return {"id": doc_id, "revision": revision}
            case "db.delete":
                collection, doc_id = _page_doc(_Doc.model_validate_json(body).path)
                return {
                    "revision": await artifact_store.delete_doc(db, artifact, collection, doc_id)
                }
            case "db.revision":
                return {"revision": revision}
            case "notify":
                p = _Notify.model_validate_json(body)
                return {"id": await notify(db, artifact, visitor, p.event, p.data)}
            case _:  # thread
                return {"messages": await thread_of(db, artifact, visitor)}

    async def notify(
        db: AsyncSession, artifact: ArtifactRow, visitor: PageVisitor, name: str, data: JsonValue
    ) -> str:
        """Save the event, then hand it to each of the page's watches, signed with the
        key this process derives for it. A watch that refuses it is counted, not shown."""
        viewer, now = visitor.viewer, runtime.now()
        event = build_event(
            slug=artifact.slug,
            name=name,
            data=data,
            sender=sender_of(viewer),
            by=viewer.name,
            visible_outside_workspace=await artifact_store.visible_outside_workspace(db, artifact),
            event_id=uuid.uuid4(),
            now=now,
        )
        await artifact_store.add_event(db, artifact, event, account_id=viewer.account_id, now=now)
        keys = tuple(k.get_secret_value() for k in runtime.settings.crypto.keys)
        delivered = 0
        for watch in await watches_store.page_watches(
            db, tenant_id=artifact.tenant_id, page_id=artifact.id
        ):
            try:
                async with db.begin_nested():
                    key_id = await watches_store.live_key_id(db, watch.id, now=now)
                    key = None if key_id is None else page_event_key(keys, watch.id, key_id)
                    if key_id is None or key is None:
                        # Expired, or derived from a crypto key no longer listed: enroll anew.
                        key_id = new_key_id(keys)
                        key = page_event_key(keys, watch.id, key_id)
                        assert key is not None
                        end = now + watches_store.MAX_LIFETIME
                        await watches_store.rotate_signing_key(
                            db,
                            watch.id,
                            key_id=key_id,
                            key=key,
                            expires_at=end
                            if watch.expires_at is None
                            else min(end, watch.expires_at),
                            now=now,
                        )
                    signed = sign_page_event(
                        key=key,
                        watch_id=watch.id,
                        page_id=artifact.id,
                        key_id=key_id,
                        body=event.body,
                        now=now,
                        event_id=event.id,
                    )
                    await watches_store.accept_page_event(db, signed, key=key)
                delivered += 1
            except ValueError as exc:
                log.info("web.artifacts.event_refused", watch_id=str(watch.id), reason=str(exc))
        await artifact_store.mark_event_delivered(db, artifact.id, event.id, delivered=delivered)
        return str(event.id)

    async def thread_of(
        db: AsyncSession, artifact: ArtifactRow, visitor: PageVisitor
    ) -> list[JsonValue]:
        if visitor.caller is None:
            raise ArtifactError("forbidden", "Sign in to ask the agent.")
        thread = await chat_store.get_ask_thread(
            db, person_id=visitor.caller.person.id, artifact_id=artifact.id
        )
        if thread is None:
            return []
        messages = await chat_store.list_messages(db, thread_id=thread.id, limit=MAX_THREAD)
        return [
            {"role": m.role, "text": m.content, "at": m.created_at.isoformat()} for m in messages
        ]

    @router.get("/a/{public_id}", response_model=None)
    @router.get("/a/{public_id}/{slug}", response_model=None)
    async def shell(  # pyright: ignore[reportUnusedFunction]
        public_id: str,
        request: Request,
        slug: str | None = None,
        k: str | None = None,
        v: int | None = None,
        download: str | None = None,
    ) -> Response:
        now = runtime.now()
        async with runtime.sessionmaker() as db:
            artifact, visitor, role = await open_page(db, request, public_id, k)
            if artifact is None or role == "none":
                if visitor.signed_in:
                    return _page("Page not found", _NO_PAGE, 404)
                path = quote(f"/a/{public_id}" + (f"/{slug}" if slug else ""), safe="")
                return RedirectResponse(f"/signin?next={path}", 303, headers=_HEADERS)
            if slug != artifact.slug:
                query = f"?{request.url.query}" if request.url.query else ""
                return RedirectResponse(
                    page_url("", public_id, artifact.slug) + query, 308, headers=_HEADERS
                )
            # An older version is for those who manage the page (its version history).
            number = v if v is not None and can_edit(role) else artifact.current_version
            version = await artifact_store.get_version(db, artifact.id, number)
        if version is None:
            return _page("Page not found", "There is no such version.", 404)
        if download is not None:
            return Response(
                version.html,
                media_type="text/html; charset=utf-8",
                headers={
                    **_HEADERS,
                    "Content-Disposition": f'attachment; filename="{artifact.slug}.html"',
                    "Content-Security-Policy": "sandbox; default-src 'none'",
                },
            )
        link = k if is_link(artifact, k) else None
        ticket = Ticket(
            purpose="content",
            artifact_id=artifact.id,
            version=version.version,
            person_id=visitor.viewer.person_id,
            link_digest=None if link is None else link_digest(link),
        )
        content_path = f"/a/{public_id}/_content"
        nonce = secrets.token_urlsafe(16)
        bridge = {
            "api": f"/a/{public_id}/_api/",
            "link": link,
            "share": page_url(origin, public_id, artifact.slug, link),
        }
        body = _SHELL.substitute(
            title=html.escape(artifact.title),
            nonce=nonce,
            src=html.escape(f"{content_path}?t={seal(runtime.fernet, ticket, now=now)}"),
            sandbox=SANDBOX,
            signin=html.escape(
                f"/signin?next={quote(page_url('', public_id, artifact.slug), safe='')}"
            ),
            bridge=json.dumps(bridge).replace("<", "\\u003c"),
        )
        return HTMLResponse(
            body,
            headers={
                **_HEADERS,
                "Content-Security-Policy": shell_policy(nonce, origin + content_path),
                # Another page opened from this one cannot reach into its window.
                "Cross-Origin-Opener-Policy": "same-origin",
            },
        )

    return router
