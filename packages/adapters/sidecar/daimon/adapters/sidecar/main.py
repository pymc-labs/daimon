"""Capability-only ingress. No turn runner, credential reader or provider client."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator

import structlog
from aiohttp import ClientError, ClientSession, ClientTimeout, web
from daimon.adapters.sidecar.settings import SidecarSettings
from daimon.core.config import load_watches_settings
from daimon.core.continuity.page_events import MAX_EVENT_BYTES, SignedPageEvent
from daimon.core.stores.watches import (
    accept_page_event,
    audit_sidecar_identity,
    claim_poll_watches,
    prune_evidence,
)
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

log = structlog.get_logger()


def create_app(
    sessions: async_sessionmaker[AsyncSession],
    *,
    signing_keys: dict[str, SecretStr] | None = None,
    max_concurrent: int = 8,
    request_timeout_s: float = 10,
    max_requests_per_minute: int = 1200,
) -> web.Application:
    """Only callback POST paths accept input; health is private at the proxy."""
    app = web.Application(client_max_size=MAX_EVENT_BYTES)
    active = 0
    recent: deque[float] = deque()

    async def health(_: web.Request) -> web.Response:
        try:
            async with asyncio.timeout(2), sessions() as session:
                await session.execute(text("SELECT 1"))
            return web.json_response({"status": "ok"})
        except (TimeoutError, DBAPIError):
            return web.json_response({"status": "unavailable"}, status=503)

    async def callback(request: web.Request) -> web.Response:
        nonlocal active
        if not load_watches_settings().enabled:
            return web.Response(status=503)
        now = time.monotonic()
        while recent and recent[0] <= now - 60:
            recent.popleft()
        if len(recent) >= max_requests_per_minute:
            return web.Response(status=429)
        recent.append(now)
        # No wait queue: requests awaiting body reads also consume a slot.
        if active >= max_concurrent:
            return web.Response(status=429)
        active += 1
        try:
            async with asyncio.timeout(request_timeout_s):
                try:
                    watch_id = uuid.UUID(request.match_info["watch_id"])
                except ValueError:
                    return web.Response(status=404)

                def header(name: str) -> str:
                    values = request.headers.getall(name, [])
                    if len(values) != 1:
                        raise ValueError("Missing or duplicate signed header")
                    return values[0]

                try:
                    page = uuid.UUID(header("X-Page-ID"))
                    event = uuid.UUID(header("X-Event-ID"))
                    stamp = int(header("X-Timestamp"))
                    key_id = header("X-Watch-Key-ID")
                    signature = header("X-Signature")
                except ValueError:
                    return web.Response(status=400)
                key = (signing_keys or {}).get(key_id)
                if key is None or request.headers.get("Content-Encoding", "identity") != "identity":
                    return web.Response(status=403)
                # Streaming bounds work without Content-Length, including chunked senders.
                body = bytearray()
                async for chunk in request.content.iter_chunked(4096):
                    body.extend(chunk)
                    if len(body) > MAX_EVENT_BYTES:
                        return web.Response(status=413)
                try:
                    body.decode("utf-8")
                except UnicodeDecodeError:
                    return web.Response(status=400)
                delivery = SignedPageEvent(
                    watch_id, page, key_id, stamp, event, bytes(body), signature
                )
                async with sessions.begin() as session:
                    key = await accept_page_event(session, delivery, key=key.get_secret_value())
                # The context commits before success is observable by the sender.
                return web.json_response(
                    {"accepted": key is not None, "wake_id": None if key is None else str(key)},
                    status=202,
                )
        except DBAPIError as exc:
            # Never log exception text/parameters, authorization headers or bodies.
            state = getattr(exc.orig, "sqlstate", None)
            return web.Response(
                status=409
                if state == "23505"
                else 429
                if state == "54000"
                else 403
                if state in ("42501", "22023")
                else 503
            )
        except (ValueError, UnicodeError):
            return web.Response(status=403)
        except TimeoutError:
            return web.Response(status=503)
        finally:
            active -= 1

    app.router.add_get("/healthz", health)
    app.router.add_post("/callbacks/watches/{watch_id}", callback)
    return app


def run() -> None:
    settings = SidecarSettings()  # pyright: ignore[reportCallIssue]
    engine = create_async_engine(
        settings.database_url.get_secret_value(),
        pool_size=2,
        max_overflow=2,
        pool_timeout=2,
        hide_parameters=True,
        connect_args={"server_settings": {"statement_timeout": "8000", "lock_timeout": "3000"}},
    )
    app = create_app(
        async_sessionmaker(engine, expire_on_commit=False),
        signing_keys=settings.signing_keys,
        max_concurrent=settings.max_concurrent,
        request_timeout_s=settings.request_timeout_s,
    )

    poll_failed = False

    async def cleanup(_: web.Application) -> AsyncIterator[None]:
        async with async_sessionmaker(engine).begin() as session:
            await audit_sidecar_identity(session)

        async def prune() -> None:
            while True:
                try:
                    async with asyncio.timeout(10), async_sessionmaker(engine).begin() as db:
                        await prune_evidence(db)
                except (DBAPIError, TimeoutError):
                    pass  # Retry bounded cleanup; no exception/parameter logging.
                await asyncio.sleep(3600)

        def poll_done(task: asyncio.Task[None]) -> None:
            nonlocal poll_failed
            if not task.cancelled() and (exc := task.exception()) is not None:
                poll_failed = True
                log.error("sidecar.poll_stopped", error_type=type(exc).__name__)
                raise web.GracefulExit() from exc

        poll_task = asyncio.create_task(
            poll_loop(async_sessionmaker(engine), settings), name="sidecar.polls"
        )
        poll_task.add_done_callback(poll_done)
        task = asyncio.create_task(prune(), name="sidecar.retention")
        try:
            yield
        finally:
            poll_task.cancel()
            task.cancel()
            await asyncio.gather(poll_task, task, return_exceptions=True)
            await engine.dispose()

    app.cleanup_ctx.append(cleanup)
    # Access logs are deliberately disabled: capabilities belong in headers only.
    web.run_app(
        app,
        host=settings.host,
        port=settings.port,
        access_log=None,
        handler_args={"request_timeout": settings.request_timeout_s},
    )

    if poll_failed:
        raise SystemExit(1)


async def poll_loop(sessions: async_sessionmaker[AsyncSession], settings: SidecarSettings) -> None:
    """Retry transient scheduling failures; configuration and code errors escape."""
    async with ClientSession(timeout=ClientTimeout(total=30), trust_env=False) as http:
        while True:
            try:
                await poll_once(
                    sessions,
                    http,
                    origin=settings.os_executor_origin,
                    executor_secret=settings.executor_secret.get_secret_value()
                    if settings.executor_secret
                    else None,
                )
            except (DBAPIError, ClientError, TimeoutError) as exc:
                log.warning("sidecar.poll_failed", error_type=type(exc).__name__)
            await asyncio.sleep(settings.poll_interval_s)


async def poll_once(
    sessions: async_sessionmaker[AsyncSession],
    http: ClientSession,
    *,
    origin: str,
    executor_secret: str | None = None,
) -> int:
    """Schedule at most four reads; only a registered OS executor receives leases."""
    if not load_watches_settings().enabled:
        return 0
    if executor_secret is None:
        return 0
    if origin not in {"http://watch-executor:8767", "http://127.0.0.1:8767"}:
        raise ValueError("Poll executor must be the private OS service")
    async with sessions.begin() as db:
        leases = await claim_poll_watches(db)

    async def check(item: dict[str, str]) -> None:
        if not load_watches_settings().enabled:
            return
        async with http.post(
            f"{origin}/internal/watches/{item['watch_id']}/poll",
            headers={"X-Watch-Lease": item["lease"], "X-Watch-Executor-Key": executor_secret},
            allow_redirects=False,
        ) as response:
            response.release()

    await asyncio.gather(*(check(item) for item in leases))
    return len(leases)
