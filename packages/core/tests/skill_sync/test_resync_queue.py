"""Real-Postgres recovery and same-repository ordering tests for push resync."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from anthropic import AsyncAnthropic
from cryptography.fernet import Fernet, MultiFernet
from daimon.core._models import GitHubPushResync
from daimon.core.config import GithubSettings
from daimon.core.skill_sync import resync_queue
from daimon.core.skill_sync.resync import ResyncReport
from daimon.core.stores import github_push_resync as store
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


def _fernet() -> MultiFernet:
    return MultiFernet([Fernet(Fernet.generate_key())])


async def _enqueue(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    delivery_id: str,
    repo_full_name: str = "owner/repo",
) -> None:
    async with sessionmaker.begin() as session:
        await store.enqueue(
            session,
            repo_full_name=repo_full_name,
            ref="refs/heads/main",
            delivery_id=delivery_id,
        )


async def _run_drain(
    *,
    engine: AsyncEngine,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> int:
    async with AsyncAnthropic(api_key="sk-test") as client:
        return await resync_queue.drain_github_push_resync_queue(
            engine=engine,
            sessionmaker=sessionmaker,
            fernet=_fernet(),
            anthropic_client=client,
            github_settings=GithubSettings(),
        )


async def test_acknowledged_work_survives_worker_death_before_resync_starts(
    db_nullpool_engine: AsyncEngine,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_full_name = f"owner/repo-{uuid.uuid4().hex}"
    await _enqueue(
        db_session_factory,
        delivery_id="ack-before-start",
        repo_full_name=repo_full_name,
    )
    published: list[str] = []

    async def recover_on_next_process(
        *,
        repo_full_name: str,
        ref: str,
        sessionmaker: async_sessionmaker[AsyncSession],
        fernet: MultiFernet,
        anthropic_client: AsyncAnthropic,
        github_settings: GithubSettings,
        http_client: object | None = None,
    ) -> ResyncReport:
        published.append(f"{repo_full_name}:{ref}")
        return ResyncReport(failed_bindings=0)

    monkeypatch.setattr(resync_queue, "resync_bound_repo", recover_on_next_process)
    processed = await _run_drain(engine=db_nullpool_engine, sessionmaker=db_session_factory)

    async with db_session_factory() as session:
        row = await store.get_for_repo_ref(
            session, repo_full_name=repo_full_name, ref="refs/heads/main"
        )
    assert processed == 1, "a later scheduler process should find acknowledged durable work"
    assert published == [f"{repo_full_name}:refs/heads/main"], (
        "the recovered job should execute once"
    )
    assert row is not None and row.state == "done", "successful recovery should complete the job"


@pytest.mark.parametrize(
    ("retryable_bindings", "expected_state"),
    [(0, "done"), (1, "pending")],
)
async def test_queue_retries_only_retryable_binding_failures(
    db_nullpool_engine: AsyncEngine,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    retryable_bindings: int,
    expected_state: str,
) -> None:
    repo_full_name = f"owner/repo-{uuid.uuid4().hex}"
    await _enqueue(
        db_session_factory,
        delivery_id=f"categorized-{retryable_bindings}",
        repo_full_name=repo_full_name,
    )

    async def categorized_result(
        *,
        repo_full_name: str,
        ref: str,
        sessionmaker: async_sessionmaker[AsyncSession],
        fernet: MultiFernet,
        anthropic_client: AsyncAnthropic,
        github_settings: GithubSettings,
        http_client: object | None = None,
    ) -> ResyncReport:
        return ResyncReport(failed_bindings=1, retryable_bindings=retryable_bindings)

    monkeypatch.setattr(resync_queue, "resync_bound_repo", categorized_result)
    processed = await _run_drain(engine=db_nullpool_engine, sessionmaker=db_session_factory)

    async with db_session_factory() as session:
        row = await store.get_for_repo_ref(
            session, repo_full_name=repo_full_name, ref="refs/heads/main"
        )
    assert processed == 1, "the scheduler should process one categorized repository result"
    assert row is not None and row.state == expected_state, (
        "only transient binding failures should retain durable queue work"
    )


async def test_expired_lease_recovers_after_process_dies_mid_binding_batch(
    db_nullpool_engine: AsyncEngine,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_full_name = f"owner/repo-{uuid.uuid4().hex}"
    await _enqueue(
        db_session_factory,
        delivery_id="death-mid-batch",
        repo_full_name=repo_full_name,
    )
    visible_skills: dict[str, str] = {}
    write_counts = {"agent-a": 0, "agent-b": 0}
    calls = 0

    async def die_after_first_binding(
        *,
        repo_full_name: str,
        ref: str,
        sessionmaker: async_sessionmaker[AsyncSession],
        fernet: MultiFernet,
        anthropic_client: AsyncAnthropic,
        github_settings: GithubSettings,
        http_client: object | None = None,
    ) -> ResyncReport:
        nonlocal calls
        calls += 1
        write_counts["agent-a"] += 1
        visible_skills["agent-a"] = "version-1"
        if calls == 1:
            raise SystemExit("simulated process death during binding batch")
        visible_skills["agent-b"] = "version-1"
        write_counts["agent-b"] += 1
        return ResyncReport(failed_bindings=0)

    monkeypatch.setattr(resync_queue, "resync_bound_repo", die_after_first_binding)
    with pytest.raises(SystemExit, match="simulated process death"):
        await _run_drain(engine=db_nullpool_engine, sessionmaker=db_session_factory)

    async with db_session_factory.begin() as session:
        result = await session.execute(
            select(GitHubPushResync).where(
                GitHubPushResync.repo_full_name == repo_full_name,
                GitHubPushResync.ref == "refs/heads/main",
            )
        )
        row = result.scalar_one()
        row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)

    processed = await _run_drain(engine=db_nullpool_engine, sessionmaker=db_session_factory)
    async with db_session_factory() as session:
        row = await store.get_for_repo_ref(
            session, repo_full_name=repo_full_name, ref="refs/heads/main"
        )
    assert processed == 1, "a later process should reclaim the expired batch lease"
    assert calls == 2, "the retry should repeat the batch after process death"
    assert visible_skills == {"agent-a": "version-1", "agent-b": "version-1"}, (
        "the retry should converge every binding after a partial first batch"
    )
    assert write_counts == {"agent-a": 2, "agent-b": 1}, (
        "a binding completed before process death may receive a repeated external write"
    )
    assert row is not None and row.state == "done", "the replayed batch should finish the job"
    assert row.attempts == 2, "the persisted lease history should expose the retry"


async def test_later_push_converges_after_stalled_old_generation(
    db_nullpool_engine: AsyncEngine,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_full_name = f"owner/repo-{uuid.uuid4().hex}"
    await _enqueue(
        db_session_factory,
        delivery_id="push-s1",
        repo_full_name=repo_full_name,
    )
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    published: list[str] = []
    calls = 0
    monkeypatch.setattr(resync_queue, "_LEASE_FOR", timedelta(milliseconds=100))
    monkeypatch.setattr(resync_queue, "_LEASE_RENEW_INTERVAL", timedelta(hours=1))

    async def stalled_sync(
        *,
        repo_full_name: str,
        ref: str,
        sessionmaker: async_sessionmaker[AsyncSession],
        fernet: MultiFernet,
        anthropic_client: AsyncAnthropic,
        github_settings: GithubSettings,
        http_client: object | None = None,
    ) -> ResyncReport:
        nonlocal calls
        calls += 1
        if calls == 1:
            first_started.set()
            await release_first.wait()
        published.append("S1" if calls == 1 else "S2")
        return ResyncReport(failed_bindings=0)

    monkeypatch.setattr(resync_queue, "resync_bound_repo", stalled_sync)
    first_worker = asyncio.create_task(
        _run_drain(engine=db_nullpool_engine, sessionmaker=db_session_factory)
    )
    await asyncio.wait_for(first_started.wait(), timeout=5)
    await _enqueue(
        db_session_factory,
        delivery_id="push-s2",
        repo_full_name=repo_full_name,
    )
    await asyncio.sleep(0.15)  # let S1's short lease expire while its repo lock is held
    second_worker_processed = await _run_drain(
        engine=db_nullpool_engine, sessionmaker=db_session_factory
    )
    assert second_worker_processed == 1, (
        "the second process should observe and defer the expired claim"
    )
    assert calls == 1, "the per-repository advisory lock must prevent overlapping binding batches"

    release_first.set()
    await first_worker
    async with db_session_factory() as session:
        row = await store.get_for_repo_ref(
            session, repo_full_name=repo_full_name, ref="refs/heads/main"
        )
    assert published == ["S1", "S2"], "the current branch state must run after the stalled push"
    assert row is not None and row.state == "done", "the latest coalesced push should complete"
    assert row.delivery_id == "push-s2", "the queue should retain the latest delivery identity"
    assert row.generation >= 3, "stale-owner completion should schedule an extra convergence pass"
