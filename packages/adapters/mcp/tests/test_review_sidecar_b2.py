"""Independent re-review (sidecar-b @ f677e24a3). Each asserts the SECURE behaviour."""

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from daimon.adapters.mcp.tools.watches import (
    create_watch_impl,
    manage_watch_impl,
    rotate_watch_impl,
)
from daimon.adapters.mcp.watch_executor import create_poll_app
from daimon.adapters.mcp.watch_polling import run_poll
from daimon.core.continuity.continuation import ContinuationRequest
from daimon.core.continuity.poll_tools import require_distinct_source
from daimon.core.stores import watches
from daimon.core.stores.domain import Role
from daimon.core.turn_origin import turn_origin
from fastmcp.exceptions import ToolError
from sqlalchemy import text

from .test_watch_polling import attended_owner, lease, runtime_watch  # noqa: F401

pytestmark = pytest.mark.fresh_schema
SECRET = "s" * 40


# --- P2-1 re-check: resume/rotate from unattended origins and with no origin --------------


@pytest.mark.parametrize("action", ["resume", "rotate"])
@pytest.mark.parametrize("origin_kind", ["unattended", "none", "other_session"])
async def test_unattended_or_missing_origin_cannot_resume_or_rotate(
    runtime_watch,  # noqa: F811
    action,
    origin_kind,
):
    runtime, watch, auth = runtime_watch
    await manage_watch_impl(runtime, auth, watch_id=str(watch.id), action="pause")
    if action == "rotate":
        async with runtime.session_factory.begin() as db:
            await db.execute(
                text("UPDATE watches SET source='page_event' WHERE id=:id"), {"id": watch.id}
            )
    agent_auth = replace(auth, chat_session_id="routine-session")
    async with turn_origin(
        runtime.session_factory,
        tenant_id=watch.tenant_id,
        account_id=watch.owner_account_id,
        platform="discord",
        parent_channel_id=watch.parent_channel_id,
        thread_id=watch.thread_id,
        responder_ma_agent_id=watch.agent_id,
        responder_name=watch.agent_name,
        role=Role.USER,
        chat_session_id="other" if origin_kind == "other_session" else "routine-session",
        attended=origin_kind == "other_session",
        sender_foreign=False,
        shared_channel=False,
    ) as origin:
        origin_id = None if origin_kind == "none" else str(origin.id)
        deliver = AsyncMock()
        with (
            patch("daimon.adapters.mcp.tools.watches.private_key_delivery", deliver),
            pytest.raises(ToolError),
        ):
            if action == "resume":
                await manage_watch_impl(
                    runtime,
                    agent_auth,
                    watch_id=str(watch.id),
                    action="resume",
                    origin_context_id=origin_id,
                )
            else:
                await rotate_watch_impl(
                    runtime, agent_auth, watch_id=str(watch.id), origin_context_id=origin_id
                )
        deliver.assert_not_called()
    async with runtime.session_factory() as db:
        assert (await watches.get_watch(db, watch.id)).status == "paused"


# --- P2-2 re-check: alternate spellings of the reporting thread -------------------------


@pytest.mark.parametrize("spelling", ["04242", "004242", "0000000004242"])
def test_distinct_source_refuses_alternate_spellings_of_reporting_thread(spelling):
    # Discord resolves int("04242") == 4242: the same thread, so the same self-wake loop.
    with pytest.raises(ValueError):
        require_distinct_source({"thread_id": spelling}, "4242")


async def test_register_refuses_leading_zero_self_source(runtime_watch):  # noqa: F811
    runtime, watch, _ = runtime_watch
    binding = ContinuationRequest(
        tenant_id=watch.tenant_id,
        platform="discord",
        parent_channel_id=watch.parent_channel_id,
        thread_id=watch.thread_id,
        requester_account_id=watch.owner_account_id,
        requester_external_user_id=watch.owner_external_id,
        target_ma_agent_id=watch.agent_id,
        target_name=watch.agent_name,
        requested_work=None,
        reason="watch_fired",
        idempotency_key=uuid.uuid4(),
    )
    now = datetime.now(UTC)
    with pytest.raises(ValueError):
        async with runtime.session_factory.begin() as db:
            await watches.register_poll_watch(
                db,
                binding,
                instructions="Report changes.",
                tool="read_thread",
                arguments={"thread_id": "0" + watch.thread_id},
                projection=".rows",
                rule="change",
                interval=60,
                expires_at=now + timedelta(days=1),
                now=now,
            )


async def test_leading_zero_self_source_cannot_read(runtime_watch):  # noqa: F811
    """Even a stored row must be refused once the source resolves to the reporting thread."""
    runtime, watch, _ = runtime_watch
    async with runtime.session_factory.begin() as db:
        await db.execute(
            text(
                "UPDATE watches SET source_state=jsonb_set(source_state,'{arguments,thread_id}',"
                "to_jsonb(CAST(:t AS text))) WHERE id=:id"
            ),
            {"t": "0" + watch.thread_id, "id": watch.id},
        )
    # Real resolution canonicalises the ID, as discord.py's int(...) does.
    with (
        patch(
            "daimon.adapters.mcp.watch_polling.poll_read_place",
            AsyncMock(
                side_effect=lambda runtime, auth, resource_id, **kw: __import__(
                    "daimon.core.authz", fromlist=["Place"]
                ).Place(channel_id=str(int(resource_id)), parent_channel_id="4241")
            ),
        ),
        patch(
            "daimon.adapters.mcp.watch_polling._read_thread_impl",
            AsyncMock(
                return_value=SimpleNamespace(
                    model_dump=lambda **_: {"rows": [{"role": "user", "content": "1"}]}
                )
            ),
        ) as source,
    ):
        await run_poll(runtime, watch.id, await lease(runtime, watch))
        source.assert_not_called()


# --- P3-4 re-check: the card is only a session config; the server never checks it -------


async def test_create_watch_requires_server_side_card_proof(attended_owner, monkeypatch):  # noqa: F811
    """Other card-only tools (add_skill, publish, microsoft_send) call session_card_gap.

    A session that does not hold create_watch on always_ask (e.g. a backend whose
    profile lacks tool_confirmation, where run.py drops confirmed_tools) must be refused.
    """
    runtime, watch, auth, origin = attended_owner
    gap = AsyncMock(return_value="session_not_gated")
    monkeypatch.setattr(
        "daimon.adapters.mcp.tools._session_gate.session_card_gap", gap, raising=True
    )
    with pytest.raises(ToolError):
        await create_watch_impl(
            runtime,
            auth,
            origin_context_id=origin,
            instructions="Report a change.",
            source="poll",
            tool="read_thread",
            arguments={"thread_id": "4545"},
        )


# --- P2-3 re-check: private listener auth ordering -------------------------------------


class _NoDb:
    def __call__(self, *a, **k):
        raise AssertionError("database touched before authentication")

    begin = __call__


@pytest.mark.parametrize(
    "headers",
    [
        [],
        [("X-Watch-Executor-Key", "")],
        [("X-Watch-Executor-Key", SECRET[:-1])],
        [("X-Watch-Executor-Key", SECRET + "x")],
        [("X-Watch-Executor-Key", SECRET.upper())],
        [("X-Watch-Executor-Key", SECRET), ("X-Watch-Executor-Key", SECRET)],
        [("x-watch-executor-key", " " + SECRET)],
    ],
)
async def test_executor_auth_precedes_any_db_work(monkeypatch, headers):
    monkeypatch.setenv("DAIMON_WATCHES__ENABLED", "true")
    monkeypatch.setenv("DAIMON_WATCHES__EXECUTOR_SECRET", SECRET)
    app = create_poll_app(SimpleNamespace(session_factory=_NoDb()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://x") as c:
        r = await c.post(
            f"/internal/watches/{uuid.uuid4()}/poll",
            headers=[*headers, ("X-Watch-Lease", str(uuid.uuid4()))],
        )
    assert r.status_code == 403


async def test_executor_refuses_everything_when_secret_unset(monkeypatch):
    monkeypatch.setenv("DAIMON_WATCHES__ENABLED", "true")
    monkeypatch.delenv("DAIMON_WATCHES__EXECUTOR_SECRET", raising=False)
    app = create_poll_app(SimpleNamespace(session_factory=_NoDb()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://x") as c:
        for key in ("", "None", "null"):
            r = await c.post(
                f"/internal/watches/{uuid.uuid4()}/poll",
                headers={"X-Watch-Executor-Key": key, "X-Watch-Lease": str(uuid.uuid4())},
            )
            assert r.status_code == 403


def test_public_mcp_app_has_no_poll_route():
    import inspect

    from daimon.adapters.mcp import server

    assert "internal/watches" not in inspect.getsource(server)


# --- P3-1 re-check -----------------------------------------------------------------------


async def test_db_gate_flip_mid_poll_is_not_access_revoked(runtime_watch):  # noqa: F811
    runtime, watch, _ = runtime_watch
    token = await lease(runtime, watch)
    async with runtime.session_factory.begin() as db:
        await db.execute(
            text("INSERT INTO watch_controls(scope,enabled) VALUES(:s,false)"),
            {"s": f"tenant:{watch.tenant_id}"},
        )
    with patch("daimon.adapters.mcp.watch_polling._read_thread_impl", AsyncMock()) as source:
        assert not await run_poll(runtime, watch.id, token)
        source.assert_not_called()
    async with runtime.session_factory() as db:
        row = await watches.get_watch(db, watch.id)
    assert row.source_state.get("reason") != "access_revoked"
    assert row.lease_owner is None


# --- carried over from round 1 -----------------------------------------------------------


async def test_concurrent_executions_of_one_lease_fire_once(runtime_watch):  # noqa: F811
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker

    runtime, watch, _ = runtime_watch
    state = {"rows": [{"role": "user", "content": "1"}]}
    source = AsyncMock(
        side_effect=lambda *a, **k: SimpleNamespace(model_dump=lambda **_: dict(state))
    )
    with patch("daimon.adapters.mcp.watch_polling._read_thread_impl", source):
        assert await run_poll(runtime, watch.id, await lease(runtime, watch))
        state["rows"] = [{"role": "user", "content": "2"}]
        token = await lease(runtime, watch)
        independent = async_sessionmaker(
            runtime.session_factory.kw["bind"].engine, expire_on_commit=False
        )
        parallel = SimpleNamespace(**{**vars(runtime), "session_factory": independent})
        results = await asyncio.gather(*(run_poll(parallel, watch.id, token) for _ in range(4)))
    assert sum(results) == 1
    async with runtime.session_factory() as db:
        assert len(await watches.list_evidence(db, watch.id)) == 1
        assert (await watches.get_watch(db, watch.id)).status == "active"


@pytest.mark.parametrize(
    ("tool", "arguments", "projection", "rule"),
    [
        ("send_message", {"channel_id": "1"}, ".", "change"),
        ("artifact_db_query", {}, ".", "change"),
        ("READ_THREAD", {"thread_id": "1"}, ".", "change"),
        ("read_thread", {}, ".", "change"),
        ("read_thread", {"thread_id": "1", "before": "2"}, ".", "change"),
        ("read_thread", {"thread_id": "1", "url": "https://x"}, ".", "change"),
        ("read_thread", {"channel_id": "1"}, ".", "change"),
        ("read_thread", {"thread_id": "https://x"}, ".", "change"),
        ("read_thread", {"thread_id": "١٢٣"}, ".", "change"),
        ("read_thread", {"thread_id": 123}, ".", "change"),
        ("read_thread", {"thread_id": "1", "limit": True}, ".", "change"),
        ("read_thread", {"thread_id": "1", "limit": 500}, ".", "change"),
        ("read_thread", {"thread_id": "1"}, ".rows[] | select(.x)", "change"),
        ("read_thread", {"thread_id": "1"}, "..", "change"),
        ("read_thread", {"thread_id": "1"}, ".", "deleted"),
    ],
)
def test_poll_registry_refuses_smuggling(tool, arguments, projection, rule):
    from daimon.core.continuity.poll_tools import validate_poll

    with pytest.raises(ValueError):
        validate_poll(tool, arguments, projection=projection, rule=rule, interval=300)
