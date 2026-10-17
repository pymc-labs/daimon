"""The keep-alive sweep spends refresh tokens that are close to expiry, once."""

from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import httpx
import pytest
from cryptography.fernet import Fernet, MultiFernet
from daimon.core import github_requester_access
from daimon.core._models import GitHubUserLink
from daimon.core.github_credentials import build_multifernet, decrypt_token, encrypt_token
from daimon.core.github_link_refresh import LinkRefreshCounts, refresh_expiring_links
from daimon.core.github_requester_access import linked_access_token_with_generation
from daimon.core.stores import github_links
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

pytestmark = pytest.mark.asyncio


@pytest.fixture
def fernet() -> MultiFernet:
    return build_multifernet((Fernet.generate_key().decode(),))


async def _add_user(
    sessionmaker: async_sessionmaker[AsyncSession],
    fernet: MultiFernet,
    user_id: int,
    *,
    refresh_in: timedelta | None = timedelta(days=10),
    access_in: timedelta = timedelta(hours=5),
    status: Literal["active", "broken"] = "active",
    with_refresh_token: bool = True,
) -> None:
    now = datetime.now(UTC)
    async with sessionmaker.begin() as session:
        session.add(
            GitHubUserLink(
                github_user_id=user_id,
                login=f"user{user_id}",
                encrypted_access_token=encrypt_token(fernet, f"access-{user_id}"),
                encrypted_refresh_token=(
                    encrypt_token(fernet, f"refresh-{user_id}") if with_refresh_token else None
                ),
                access_expires_at=now + access_in,
                refresh_expires_at=None if refresh_in is None else now + refresh_in,
                status=status,
            )
        )


def _github(
    seen: list[str], *, fail_for: dict[str, httpx.Response] | None = None
) -> httpx.AsyncClient:
    """Fake GitHub refresh endpoint; rotates ``refresh-N`` to ``refresh-N-new``."""

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/login/oauth/access_token"
        body = dict(pair.split("=") for pair in request.content.decode().split("&"))
        spent = body["refresh_token"]
        seen.append(spent)
        if fail_for and spent in fail_for:
            return fail_for[spent]
        return httpx.Response(
            200,
            json={
                "access_token": f"{spent}-access",
                "refresh_token": f"{spent}-new",
                "expires_in": 28800,
                "refresh_token_expires_in": 15897600,
            },
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _sweep(
    sessionmaker: async_sessionmaker[AsyncSession],
    client: httpx.AsyncClient,
    fernet: MultiFernet,
    limit: int = 50,
    **kwargs: Any,
) -> LinkRefreshCounts:
    kwargs.setdefault("now", datetime.now(UTC))
    return await refresh_expiring_links(
        sessionmaker,
        client,
        fernet=fernet,
        client_id="client",
        client_secret="secret",
        limit=limit,
        **kwargs,
    )


async def test_sweep_refreshes_a_row_inside_the_window_and_rotates_tokens(
    db_session_factory: async_sessionmaker[AsyncSession],
    fernet: MultiFernet,
) -> None:
    await _add_user(db_session_factory, fernet, 1)
    seen: list[str] = []
    async with _github(seen) as client:
        counts = await _sweep(db_session_factory, client, fernet)
    assert counts == LinkRefreshCounts(refreshed=1, broken=0, failed=0)
    assert seen == ["refresh-1"]
    async with db_session_factory() as session:
        row = await github_links.get_user(session, github_user_id=1)
    assert row is not None and row.encrypted_refresh_token is not None
    assert row.status == "active" and row.token_generation == 2 and row.link_generation == 1
    assert decrypt_token(fernet, row.encrypted_access_token) == "refresh-1-access"
    assert decrypt_token(fernet, row.encrypted_refresh_token) == "refresh-1-new"
    assert row.refresh_expires_at is not None
    assert row.refresh_expires_at > datetime.now(UTC) + timedelta(days=150)
    # The row has left the window, so the next sweep leaves it alone.
    async with _github(seen) as client:
        again = await _sweep(db_session_factory, client, fernet)
    assert again == LinkRefreshCounts() and seen == ["refresh-1"]


async def test_sweep_skips_rows_it_must_not_touch(
    db_session_factory: async_sessionmaker[AsyncSession],
    fernet: MultiFernet,
) -> None:
    await _add_user(db_session_factory, fernet, 1, refresh_in=timedelta(days=90))  # outside
    await _add_user(db_session_factory, fernet, 2, refresh_in=timedelta(days=-1))  # expired
    await _add_user(db_session_factory, fernet, 3, status="broken")
    await _add_user(db_session_factory, fernet, 4, with_refresh_token=False)
    await _add_user(db_session_factory, fernet, 5, refresh_in=None)
    seen: list[str] = []
    async with _github(seen) as client:
        counts = await _sweep(db_session_factory, client, fernet)
    assert counts == LinkRefreshCounts()
    assert seen == []


async def test_sweep_takes_the_soonest_expiring_rows_up_to_the_limit(
    db_session_factory: async_sessionmaker[AsyncSession],
    fernet: MultiFernet,
) -> None:
    await _add_user(db_session_factory, fernet, 1, refresh_in=timedelta(days=20))
    await _add_user(db_session_factory, fernet, 2, refresh_in=timedelta(days=2))
    await _add_user(db_session_factory, fernet, 3, refresh_in=timedelta(days=9))
    seen: list[str] = []
    async with _github(seen) as client:
        counts = await _sweep(db_session_factory, client, fernet, limit=2)
    assert counts.refreshed == 2
    assert seen == ["refresh-2", "refresh-3"]


async def test_bad_refresh_token_marks_the_link_broken(
    db_session_factory: async_sessionmaker[AsyncSession],
    fernet: MultiFernet,
) -> None:
    await _add_user(db_session_factory, fernet, 1)
    seen: list[str] = []
    bad = httpx.Response(200, json={"error": "bad_refresh_token"})
    async with _github(seen, fail_for={"refresh-1": bad}) as client:
        counts = await _sweep(db_session_factory, client, fernet)
    assert counts == LinkRefreshCounts(refreshed=0, broken=1, failed=0)
    async with db_session_factory() as session:
        row = await github_links.get_user(session, github_user_id=1)
    assert row is not None and row.status == "broken" and row.link_generation == 2


async def test_a_server_error_for_one_user_does_not_stop_the_next(
    db_session_factory: async_sessionmaker[AsyncSession],
    fernet: MultiFernet,
) -> None:
    await _add_user(db_session_factory, fernet, 1, refresh_in=timedelta(days=1))
    await _add_user(db_session_factory, fernet, 2, refresh_in=timedelta(days=2))
    await _add_user(db_session_factory, fernet, 3, refresh_in=timedelta(days=3))
    seen: list[str] = []
    fail = {
        "refresh-1": httpx.Response(500, text="boom"),
        "refresh-2": httpx.Response(429, text="slow down"),
    }
    async with _github(seen, fail_for=fail) as client:
        counts = await _sweep(db_session_factory, client, fernet)
    assert counts == LinkRefreshCounts(refreshed=1, broken=0, failed=2)
    assert seen == ["refresh-1", "refresh-2", "refresh-3"]
    async with db_session_factory() as session:
        failed = await github_links.get_user(session, github_user_id=1)
        done = await github_links.get_user(session, github_user_id=3)
    # A failed refresh leaves the stored credential untouched, ready for the next sweep.
    assert failed is not None and failed.status == "active" and failed.token_generation == 1
    assert failed.encrypted_refresh_token is not None
    assert decrypt_token(fernet, failed.encrypted_refresh_token) == "refresh-1"
    assert done is not None and done.token_generation == 2


async def test_sweep_and_connect_never_spend_the_same_refresh_token(
    db_engine: AsyncEngine, db_clean: None, fernet: MultiFernet
) -> None:
    sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False)
    # The access token is expired, so Connect and the sweep both want a refresh.
    await _add_user(sessionmaker, fernet, 1, access_in=timedelta(minutes=-5))
    seen: list[str] = []
    gate = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        body = dict(pair.split("=") for pair in request.content.decode().split("&"))
        seen.append(body["refresh_token"])
        await asyncio.sleep(0.1)  # hold the refresh open so the other caller must queue
        gate.set()
        return httpx.Response(
            200,
            json={
                "access_token": "rotated-access",
                "refresh_token": "rotated-refresh",
                "expires_in": 28800,
                "refresh_token_expires_in": 15897600,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        connect, counts = await asyncio.gather(
            linked_access_token_with_generation(
                sessionmaker,
                client,
                user_id=1,
                fernet=fernet,
                client_id="client",
                client_secret="secret",
            ),
            _sweep(sessionmaker, client, fernet),
        )
    assert seen == ["refresh-1"], "the refresh token must be spent exactly once"
    assert connect[0] == "rotated-access"
    assert counts == LinkRefreshCounts(refreshed=1, broken=0, failed=0)
    async with sessionmaker() as session:
        row = await github_links.get_user(session, github_user_id=1)
    assert row is not None and row.token_generation == 2 and row.status == "active"


async def test_sweep_does_not_respend_a_token_connect_just_rotated(
    db_engine: AsyncEngine, db_clean: None, fernet: MultiFernet
) -> None:
    """A valid access token and a near-expiry refresh token: two sweeps, one POST."""
    sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False)
    await _add_user(sessionmaker, fernet, 1)
    seen: list[str] = []
    async with _github(seen) as client:
        first, second = await asyncio.gather(
            _sweep(sessionmaker, client, fernet), _sweep(sessionmaker, client, fernet)
        )
    assert seen == ["refresh-1"]
    # The second sweep either reuses the rotated token or selects nothing because
    # the first already moved the refresh expiry out of the window.
    assert first.refreshed + second.refreshed in (1, 2)
    assert first.failed + second.failed + first.broken + second.broken == 0
    async with sessionmaker() as session:
        row = await github_links.get_user(session, github_user_id=1)
    assert row is not None and row.token_generation == 2


async def test_failing_users_are_backed_off_so_later_healthy_rows_are_refreshed(
    db_session_factory: async_sessionmaker[AsyncSession],
    fernet: MultiFernet,
) -> None:
    for user_id, days in ((7001, 1), (7002, 2), (7003, 3), (7004, 4)):
        await _add_user(db_session_factory, fernet, user_id, refresh_in=timedelta(days=days))
    fail = {f"refresh-{n}": httpx.Response(500, text="boom") for n in (7001, 7002, 7003)}
    seen: list[str] = []
    backoff: dict[int, datetime] = {}
    async with _github(seen, fail_for=fail) as client:
        first = await _sweep(db_session_factory, client, fernet, limit=2, backoff=backoff)
        assert first == LinkRefreshCounts(failed=2)
        assert set(backoff) == {7001, 7002}
        second = await _sweep(db_session_factory, client, fernet, limit=2, backoff=backoff)
    # Users 1 and 2 are left out, so the slots go to 3 (fails) and 4 (healthy).
    assert seen == ["refresh-7001", "refresh-7002", "refresh-7003", "refresh-7004"]
    assert second == LinkRefreshCounts(refreshed=1, failed=1)
    assert set(backoff) == {7001, 7002, 7003}


async def test_without_a_backoff_persistent_failures_hold_every_slot(
    db_session_factory: async_sessionmaker[AsyncSession],
    fernet: MultiFernet,
) -> None:
    """Control: no memory between sweeps, so the healthy row never gets a slot."""
    for user_id, days in ((7001, 1), (7002, 2), (7003, 3)):
        await _add_user(db_session_factory, fernet, user_id, refresh_in=timedelta(days=days))
    fail = {f"refresh-{n}": httpx.Response(500, text="boom") for n in (7001, 7002)}
    seen: list[str] = []
    async with _github(seen, fail_for=fail) as client:
        for _ in range(2):
            await _sweep(db_session_factory, client, fernet, limit=2)
    assert seen == ["refresh-7001", "refresh-7002", "refresh-7001", "refresh-7002"]


async def test_a_backoff_that_has_passed_lets_the_user_be_tried_again(
    db_session_factory: async_sessionmaker[AsyncSession],
    fernet: MultiFernet,
) -> None:
    await _add_user(db_session_factory, fernet, 7001)
    backoff = {
        7001: datetime.now(UTC) - timedelta(seconds=1),
        7002: datetime.now(UTC) + timedelta(hours=1),
    }
    seen: list[str] = []
    async with _github(seen) as client:
        counts = await _sweep(db_session_factory, client, fernet, backoff=backoff)
    assert counts.refreshed == 1 and seen == ["refresh-7001"]
    assert set(backoff) == {7002}


def _rotating_handler(seen: list[str], *, on_request: Any = None) -> Any:
    async def handler(request: httpx.Request) -> httpx.Response:
        body = dict(pair.split("=") for pair in request.content.decode().split("&"))
        spent = body["refresh_token"]
        seen.append(spent)
        if on_request is not None:
            await on_request(spent)
        return httpx.Response(
            200,
            json={
                "access_token": f"{spent}-access",
                "refresh_token": f"{spent}-new",
                "expires_in": 28800,
                "refresh_token_expires_in": 15897600,
            },
        )

    return handler


async def test_a_cancel_while_the_github_post_is_in_flight_still_stores_the_rotation(
    db_session_factory: async_sessionmaker[AsyncSession],
    fernet: MultiFernet,
) -> None:
    await _add_user(db_session_factory, fernet, 7001, refresh_in=timedelta(days=1))
    seen: list[str] = []
    in_flight = asyncio.Event()
    release = asyncio.Event()

    async def hold(_spent: str) -> None:
        in_flight.set()
        await release.wait()

    transport = httpx.MockTransport(_rotating_handler(seen, on_request=hold))
    async with httpx.AsyncClient(transport=transport) as client:
        sweep = asyncio.create_task(_sweep(db_session_factory, client, fernet))
        await asyncio.wait_for(in_flight.wait(), timeout=5)
        # Shutdown cancels the sweep after GitHub took the request but before it answered.
        sweep.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sweep
        release.set()
        await asyncio.wait_for(
            asyncio.gather(*github_requester_access._detached_refreshes),  # pyright: ignore[reportPrivateUsage]
            timeout=5,
        )
    assert seen == ["refresh-7001"]
    async with db_session_factory() as session:
        row = await github_links.get_user(session, github_user_id=7001)
    assert row is not None and row.encrypted_refresh_token is not None
    assert row.token_generation == 2 and row.status == "active"
    assert decrypt_token(fernet, row.encrypted_access_token) == "refresh-7001-access"
    assert decrypt_token(fernet, row.encrypted_refresh_token) == "refresh-7001-new"


def _client_that_fails_once_closed(
    seen: list[str], *, on_request: Any
) -> tuple[httpx.AsyncClient, dict[str, bool]]:
    """A fake GitHub whose answer needs the client still open when it comes."""
    state = {"closed": False}
    rotate = _rotating_handler(seen, on_request=on_request)

    async def handler(request: httpx.Request) -> httpx.Response:
        response = await rotate(request)
        if state["closed"]:
            raise httpx.ConnectError("the client was closed under the refresh")
        return response

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    real_aclose = client.aclose

    async def aclose() -> None:
        state["closed"] = True
        await real_aclose()

    client.aclose = aclose  # type: ignore[method-assign]
    return client, state


async def _stored_pair(
    sessionmaker: async_sessionmaker[AsyncSession], fernet: MultiFernet, user_id: int
) -> tuple[str, str, int]:
    async with sessionmaker() as session:
        row = await github_links.get_user(session, github_user_id=user_id)
    assert row is not None and row.encrypted_refresh_token is not None
    return (
        decrypt_token(fernet, row.encrypted_access_token),
        decrypt_token(fernet, row.encrypted_refresh_token),
        row.token_generation,
    )


async def test_shutdown_order_drains_the_detached_refresh_before_closing_the_client(
    db_session_factory: async_sessionmaker[AsyncSession],
    fernet: MultiFernet,
) -> None:
    """Cancel the sweep mid-call, then drain, then close the client: the rotation lands."""
    await _add_user(db_session_factory, fernet, 7001, refresh_in=timedelta(days=1))
    seen: list[str] = []
    in_flight = asyncio.Event()
    release = asyncio.Event()

    async def hold(_spent: str) -> None:
        in_flight.set()
        await release.wait()

    client, state = _client_that_fails_once_closed(seen, on_request=hold)
    sweep = asyncio.create_task(_sweep(db_session_factory, client, fernet))
    await asyncio.wait_for(in_flight.wait(), timeout=5)
    sweep.cancel()
    with pytest.raises(asyncio.CancelledError):
        await sweep
    # GitHub answers a moment after the cancel: the drain has to wait for it.
    asyncio.get_running_loop().call_later(0.1, release.set)
    assert await github_requester_access.drain_detached_refreshes(5) == 0
    await client.aclose()
    assert state["closed"]
    assert seen == ["refresh-7001"]
    assert await _stored_pair(db_session_factory, fernet, 7001) == (
        "refresh-7001-access",
        "refresh-7001-new",
        2,
    )


async def test_closing_the_client_before_the_drain_loses_the_rotation(
    db_session_factory: async_sessionmaker[AsyncSession],
    fernet: MultiFernet,
) -> None:
    """Control: the order the old shutdown had, which the drain exists to prevent."""
    await _add_user(db_session_factory, fernet, 7001, refresh_in=timedelta(days=1))
    seen: list[str] = []
    in_flight = asyncio.Event()
    release = asyncio.Event()

    async def hold(_spent: str) -> None:
        in_flight.set()
        await release.wait()

    client, _ = _client_that_fails_once_closed(seen, on_request=hold)
    sweep = asyncio.create_task(_sweep(db_session_factory, client, fernet))
    await asyncio.wait_for(in_flight.wait(), timeout=5)
    sweep.cancel()
    with pytest.raises(asyncio.CancelledError):
        await sweep
    await client.aclose()
    release.set()
    await github_requester_access.drain_detached_refreshes(5)
    # GitHub spent the old token and answered; nothing stored the new pair.
    assert seen == ["refresh-7001"]
    assert await _stored_pair(db_session_factory, fernet, 7001) == (
        "access-7001",
        "refresh-7001",
        1,
    )


async def test_a_cancelled_caller_holds_its_client_open_until_the_refresh_is_stored(
    db_session_factory: async_sessionmaker[AsyncSession],
    fernet: MultiFernet,
) -> None:
    """`settle_detached_refreshes` in the client's `async with`: the cancel waits for the store."""
    await _add_user(db_session_factory, fernet, 7001, refresh_in=timedelta(days=1))
    seen: list[str] = []
    in_flight = asyncio.Event()
    release = asyncio.Event()

    async def hold(_spent: str) -> None:
        in_flight.set()
        await release.wait()

    client, state = _client_that_fails_once_closed(seen, on_request=hold)

    async def sweep_like_the_scheduler() -> None:
        # The client's own `async with`, spelled out so its close is observable.
        try:
            async with github_requester_access.settle_detached_refreshes(5.0):
                await _sweep(db_session_factory, client, fernet)
        finally:
            await client.aclose()

    task = asyncio.create_task(sweep_like_the_scheduler())
    await asyncio.wait_for(in_flight.wait(), timeout=5)
    task.cancel()
    asyncio.get_running_loop().call_later(0.1, release.set)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert state["closed"], "the client closes only after the settle"
    assert await _stored_pair(db_session_factory, fernet, 7001) == (
        "refresh-7001-access",
        "refresh-7001-new",
        2,
    )


async def test_a_callable_settle_timeout_is_read_when_the_cancel_arrives() -> None:
    async def refresh() -> tuple[str | None, int, int]:
        await asyncio.sleep(30)
        return None, 0, 0

    reads: list[float] = []

    def remaining() -> float:
        reads.append(0.05)
        return 0.05

    async def body() -> None:
        async with github_requester_access.settle_detached_refreshes(remaining):
            await github_requester_access._shielded(refresh())  # pyright: ignore[reportPrivateUsage]

    task = asyncio.create_task(body())
    await asyncio.sleep(0.05)
    assert reads == [], "not read before a cancel"
    task.cancel()
    try:
        started = asyncio.get_running_loop().time()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
        assert asyncio.get_running_loop().time() - started < 1.0
        assert reads == [0.05]
    finally:
        for left in list(github_requester_access._detached_refreshes):  # pyright: ignore[reportPrivateUsage]
            left.cancel()
            await asyncio.gather(left, return_exceptions=True)


async def test_drain_gives_up_at_its_timeout_and_reports_what_is_left(
    db_session_factory: async_sessionmaker[AsyncSession],
    fernet: MultiFernet,
) -> None:
    await _add_user(db_session_factory, fernet, 7001, refresh_in=timedelta(days=1))
    in_flight = asyncio.Event()
    release = asyncio.Event()

    async def hold(_spent: str) -> None:
        in_flight.set()
        await release.wait()

    transport = httpx.MockTransport(_rotating_handler([], on_request=hold))
    async with httpx.AsyncClient(transport=transport) as client:
        sweep = asyncio.create_task(_sweep(db_session_factory, client, fernet))
        await asyncio.wait_for(in_flight.wait(), timeout=5)
        sweep.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sweep
        assert await github_requester_access.drain_detached_refreshes(0.05) == 1
        release.set()
        assert await github_requester_access.drain_detached_refreshes(5) == 0
    assert await github_requester_access.drain_detached_refreshes(0.05) == 0


async def test_the_budget_stops_starting_people_without_cancelling_the_current_one(
    db_session_factory: async_sessionmaker[AsyncSession],
    fernet: MultiFernet,
) -> None:
    for user_id, days in ((7001, 1), (7002, 2), (7003, 3)):
        await _add_user(db_session_factory, fernet, user_id, refresh_in=timedelta(days=days))
    clock = [0.0]
    seen: list[str] = []

    async def spend_the_budget(_spent: str) -> None:
        clock[0] += 100.0  # the first person's refresh is what runs the budget out

    backoff: dict[int, datetime] = {}
    transport = httpx.MockTransport(_rotating_handler(seen, on_request=spend_the_budget))
    async with httpx.AsyncClient(transport=transport) as client:
        counts = await _sweep(
            db_session_factory,
            client,
            fernet,
            backoff=backoff,
            budget_s=10.0,
            monotonic=lambda: clock[0],
        )
    assert counts == LinkRefreshCounts(refreshed=1, not_attempted=2)
    assert seen == ["refresh-7001"], "the person under way finished; nobody else started"
    assert backoff == {}, "people not attempted are not backed off"
    async with db_session_factory() as session:
        done = await github_links.get_user(session, github_user_id=7001)
        waiting = await github_links.get_user(session, github_user_id=7002)
    assert done is not None and done.token_generation == 2
    assert waiting is not None and waiting.token_generation == 1


async def test_a_stop_request_leaves_the_rest_untouched_and_unbacked_off(
    db_session_factory: async_sessionmaker[AsyncSession],
    fernet: MultiFernet,
) -> None:
    for user_id, days in ((7001, 1), (7002, 2)):
        await _add_user(db_session_factory, fernet, user_id, refresh_in=timedelta(days=days))
    stop = asyncio.Event()
    seen: list[str] = []

    async def ask_to_stop(_spent: str) -> None:
        stop.set()

    backoff: dict[int, datetime] = {}
    transport = httpx.MockTransport(_rotating_handler(seen, on_request=ask_to_stop))
    async with httpx.AsyncClient(transport=transport) as client:
        counts = await _sweep(
            db_session_factory, client, fernet, backoff=backoff, should_stop=stop.is_set
        )
    assert counts == LinkRefreshCounts(refreshed=1, not_attempted=1)
    assert seen == ["refresh-7001"] and backoff == {}


async def test_the_lock_timeout_gives_up_on_a_held_refresh_and_backs_the_user_off(
    db_engine: AsyncEngine, db_clean: None, fernet: MultiFernet
) -> None:
    sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False)
    await _add_user(sessionmaker, fernet, 7005, refresh_in=timedelta(days=1))
    await _add_user(sessionmaker, fernet, 7006, refresh_in=timedelta(days=2))
    seen: list[str] = []
    backoff: dict[int, datetime] = {}
    started = asyncio.get_running_loop().time()
    async with sessionmaker.begin() as holder:
        # Another worker is mid-refresh for user 7005 and keeps the lock.
        await holder.execute(
            text(
                "SELECT pg_advisory_xact_lock("
                "hashtextextended('github-refresh:' || CAST(:user_id AS text), 0))"
            ),
            {"user_id": "7005"},
        )
        async with _github(seen) as client:
            counts = await _sweep(
                sessionmaker, client, fernet, backoff=backoff, lock_timeout="300ms"
            )
    assert asyncio.get_running_loop().time() - started < 5
    assert counts == LinkRefreshCounts(refreshed=1, failed=1)
    assert seen == ["refresh-7006"], "no call to GitHub for the person whose lock was held"
    assert set(backoff) == {7005}
    async with sessionmaker() as session:
        row = await github_links.get_user(session, github_user_id=7005)
    assert row is not None and row.token_generation == 1


async def test_a_failure_is_backed_off_for_a_day(
    db_session_factory: async_sessionmaker[AsyncSession],
    fernet: MultiFernet,
) -> None:
    await _add_user(db_session_factory, fernet, 7001)
    seen: list[str] = []
    backoff: dict[int, datetime] = {}
    now = datetime.now(UTC)
    async with _github(seen, fail_for={"refresh-7001": httpx.Response(500)}) as client:
        await _sweep(db_session_factory, client, fernet, backoff=backoff, now=now)
    assert backoff == {7001: now + timedelta(hours=24)}


async def test_persistent_failures_at_the_front_do_not_starve_a_healthy_later_user(
    db_session_factory: async_sessionmaker[AsyncSession],
    fernet: MultiFernet,
) -> None:
    failing = 5
    capacity = 2  # people one sweep can start (the `limit` here; the budget in production)
    for n in range(failing):
        await _add_user(db_session_factory, fernet, 7100 + n, refresh_in=timedelta(days=1 + n))
    healthy = 7200
    await _add_user(db_session_factory, fernet, healthy, refresh_in=timedelta(days=20))
    fail = {f"refresh-{7100 + n}": httpx.Response(500, text="boom") for n in range(failing)}
    seen: list[str] = []
    backoff: dict[int, datetime] = {}
    start = datetime.now(UTC)
    allowed = math.ceil(failing / capacity) + 1
    reached_at: int | None = None
    async with _github(seen, fail_for=fail) as client:
        for sweep_number in range(1, allowed + 3):
            before = len(seen)
            await _sweep(
                db_session_factory,
                client,
                fernet,
                limit=capacity,
                backoff=backoff,
                now=start + timedelta(hours=sweep_number),
            )
            if f"refresh-{healthy}" in seen[before:]:
                reached_at = sweep_number
                break
    assert reached_at is not None and reached_at <= allowed
    failing_attempts = [name for name in seen if name != f"refresh-{healthy}"]
    assert sorted(failing_attempts) == sorted(f"refresh-{7100 + n}" for n in range(failing)), (
        "each failing person was tried once; none cycled back before the healthy one"
    )
