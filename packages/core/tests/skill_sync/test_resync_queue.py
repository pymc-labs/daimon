"""Real-Postgres recovery and same-repository ordering tests for push resync."""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from anthropic import AsyncAnthropic
from cryptography.fernet import Fernet, MultiFernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from daimon.core._models import GitHubPushResync
from daimon.core.config import GithubSettings
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.skill_sync import resync_queue
from daimon.core.skill_sync.resync import ResyncReport, resync_bound_repo
from daimon.core.stores import agent_repo_binding as binding_store
from daimon.core.stores import github_app_installations as install_store
from daimon.core.stores import github_push_resync as store
from daimon.core.stores.domain import RepoAccessProof
from daimon.testing.crypto import make_fernet
from daimon.testing.factories import make_cli_principal
from daimon.testing.ma import build_fake_anthropic, make_fake_ma_handler
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


@pytest.mark.parametrize(
    ("status_code", "response_body", "response_headers", "expected_state", "wait_seconds"),
    [
        (
            403,
            {"message": "You have exceeded a secondary rate limit."},
            {},
            "pending",
            59,
        ),
        (
            403,
            {"message": "API rate limit exceeded"},
            {"retry-after": "90"},
            "pending",
            89,
        ),
        (
            403,
            {"message": "Forbidden"},
            {
                "retry-after": "30",
                "x-ratelimit-remaining": "0",
                "x-ratelimit-reset": "{reset}",
            },
            "pending",
            119,
        ),
        (429, {"message": "rate limited"}, {"retry-after": "80"}, "pending", 79),
        (403, {"message": "Resource not accessible by integration"}, {}, "done", 0),
    ],
)
async def test_queue_classifies_rate_limit_and_permission_responses(
    db_session: AsyncSession,
    db_nullpool_engine: AsyncEngine,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    response_body: dict[str, str],
    response_headers: dict[str, str],
    expected_state: str,
    wait_seconds: int,
) -> None:
    repo_full_name = f"owner/repo-{uuid.uuid4().hex}"
    cli = await make_cli_principal(db_session, os_user="resync-rate-limit-queue")
    tenant_id = cli.tenant_id
    ma_handler = make_fake_ma_handler()
    agent_ids: list[uuid.UUID] = []
    async with build_fake_anthropic(ma_handler) as anthropic_client:
        for agent_name in ("rate-limit-queue-agent-a", "rate-limit-queue-agent-b"):
            agent = await anthropic_client.beta.agents.create(
                name=agent_name,
                model="claude-sonnet-4-6",
                metadata={
                    "daimon_tenant": str(tenant_id),
                    "daimon_name": agent_name,
                },
            )
            agent_ids.append(derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=agent.id))
    for agent_id in agent_ids:
        await binding_store.set_binding(
            db_session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            repo_url=repo_full_name,
            default_branch="main",
            ma_secret_ref="stub",
            proof=RepoAccessProof(kind="public", at=datetime.now(UTC), account_id=None),
        )
    await db_session.commit()
    await _enqueue(
        db_session_factory,
        delivery_id="rate-limit-not-before",
        repo_full_name=repo_full_name,
    )

    if "{reset}" in response_headers.get("x-ratelimit-reset", ""):
        response_headers["x-ratelimit-reset"] = str(int(time.time()) + 120)

    github_request_count = 0

    def github_rate_limit(request: httpx.Request) -> httpx.Response:
        nonlocal github_request_count
        github_request_count += 1
        return httpx.Response(
            status_code,
            headers=response_headers,
            json=response_body,
        )

    async def rate_limited_binding(
        *,
        repo_full_name: str,
        ref: str,
        sessionmaker: async_sessionmaker[AsyncSession],
        fernet: MultiFernet,
        anthropic_client: AsyncAnthropic,
        github_settings: GithubSettings,
        http_client: object | None = None,
    ) -> ResyncReport:
        async with httpx.AsyncClient(transport=httpx.MockTransport(github_rate_limit)) as client:
            return await resync_bound_repo(
                repo_full_name=repo_full_name,
                ref=ref,
                sessionmaker=sessionmaker,
                fernet=make_fernet(),
                anthropic_client=anthropic_client,
                github_settings=github_settings,
                http_client=client,
            )

    monkeypatch.setattr(resync_queue, "resync_bound_repo", rate_limited_binding)
    started_at = datetime.now(UTC)
    async with build_fake_anthropic(ma_handler) as anthropic_client:
        processed = await resync_queue.drain_github_push_resync_queue(
            engine=db_nullpool_engine,
            sessionmaker=db_session_factory,
            fernet=_fernet(),
            anthropic_client=anthropic_client,
            github_settings=GithubSettings(),
        )

    async with db_session_factory() as session:
        row = await store.get_for_repo_ref(
            session, repo_full_name=repo_full_name, ref="refs/heads/main"
        )
    assert processed == 1, "the scheduler should persist the provider response classification"
    assert row is not None and row.state == expected_state, (
        "rate limits retry durably while permission failures complete"
    )
    assert github_request_count == (1 if expected_state == "pending" else 2), (
        "a rate-limited batch must defer its later binding, while a permission failure may continue"
    )
    if wait_seconds:
        assert row.available_at >= started_at + timedelta(seconds=wait_seconds), (
            "the next claim must respect GitHub's retry-after, reset, or minimum wait"
        )
        async with db_session_factory.begin() as session:
            early = await store.claim_due(
                session,
                lease_owner=uuid.uuid4(),
                lease_for=timedelta(minutes=2),
                now=row.available_at - timedelta(microseconds=1),
            )
        assert early is None, "the durable queue must not claim before the provider deadline"


@pytest.mark.parametrize(
    ("status_code", "body", "headers", "expected_state", "minimum_wait"),
    [
        (
            403,
            {"message": "You have exceeded a secondary rate limit."},
            {"retry-after": "75"},
            "pending",
            74,
        ),
        (
            403,
            {"message": "API rate limit exceeded"},
            {
                "retry-after": "30",
                "x-ratelimit-remaining": "0",
                "x-ratelimit-reset": "{reset}",
            },
            "pending",
            119,
        ),
        (429, {"message": "rate limited"}, {"retry-after": "65"}, "pending", 64),
        (403, {"message": "Resource not accessible by integration"}, {}, "done", 0),
    ],
)
async def test_app_mint_rate_limits_retry_the_bound_queue_job(
    db_session: AsyncSession,
    db_nullpool_engine: AsyncEngine,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    body: dict[str, str],
    headers: dict[str, str],
    expected_state: str,
    minimum_wait: int,
) -> None:
    """App-token mint rate limits defer durable work; permission 403s complete it."""
    repo_full_name = f"owner/app-mint-{uuid.uuid4().hex}"
    cli = await make_cli_principal(db_session, os_user="resync-app-mint-rate-limit")
    tenant_id = cli.tenant_id
    ma_handler = make_fake_ma_handler()
    agent_ids: list[uuid.UUID] = []
    async with build_fake_anthropic(ma_handler) as client:
        for agent_name in ("app-mint-agent-a", "app-mint-agent-b"):
            agent = await client.beta.agents.create(
                name=agent_name,
                model="claude-sonnet-4-6",
                metadata={"daimon_tenant": str(tenant_id), "daimon_name": agent_name},
            )
            agent_ids.append(derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=agent.id))
    for agent_id in agent_ids:
        await binding_store.set_binding(
            db_session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            repo_url=repo_full_name,
            default_branch="main",
            ma_secret_ref="stub",
            proof=RepoAccessProof(kind="pat", at=datetime.now(UTC), account_id=None),
        )
    await install_store.upsert(
        db_session,
        installation_id=9001,
        account_login="owner",
        repo_full_names=[repo_full_name],
    )
    await db_session.commit()
    await _enqueue(db_session_factory, delivery_id="app-mint-rate", repo_full_name=repo_full_name)

    if headers.get("x-ratelimit-reset") == "{reset}":
        headers["x-ratelimit-reset"] = str(int(time.time()) + 120)
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_key_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    app_settings = GithubSettings(app_id="123456", app_private_key=private_key_pem)
    app_mint_requests = 0

    def github_transport(request: httpx.Request) -> httpx.Response:
        nonlocal app_mint_requests
        if request.url.path.endswith("/access_tokens"):
            app_mint_requests += 1
            return httpx.Response(status_code, headers=headers, json=body)
        raise AssertionError(f"unexpected GitHub request: {request.url.path}")

    async def resync_with_app_mint(
        *,
        repo_full_name: str,
        ref: str,
        sessionmaker: async_sessionmaker[AsyncSession],
        fernet: MultiFernet,
        anthropic_client: AsyncAnthropic,
        github_settings: GithubSettings,
        http_client: object | None = None,
    ) -> ResyncReport:
        async with httpx.AsyncClient(transport=httpx.MockTransport(github_transport)) as client:
            return await resync_bound_repo(
                repo_full_name=repo_full_name,
                ref=ref,
                sessionmaker=sessionmaker,
                fernet=make_fernet(),
                anthropic_client=anthropic_client,
                github_settings=app_settings,
                http_client=client,
            )

    monkeypatch.setattr(resync_queue, "resync_bound_repo", resync_with_app_mint)
    started_at = datetime.now(UTC)
    async with build_fake_anthropic(ma_handler) as client:
        processed = await resync_queue.drain_github_push_resync_queue(
            engine=db_nullpool_engine,
            sessionmaker=db_session_factory,
            fernet=_fernet(),
            anthropic_client=client,
            github_settings=app_settings,
        )
    async with db_session_factory() as session:
        row = await store.get_for_repo_ref(
            session, repo_full_name=repo_full_name, ref="refs/heads/main"
        )
    assert processed == 1, "the durable queue should classify the App-mint response"
    assert row is not None and row.state == expected_state, (
        "rate-limited App token minting retries while true permission failures complete"
    )
    assert app_mint_requests == (1 if expected_state == "pending" else 2), (
        "the first rate-limited App mint stops the batch; a permanent permission error does not"
    )
    if minimum_wait:
        assert row.available_at >= started_at + timedelta(seconds=minimum_wait), (
            "queue availability must include the App-mint provider deadline"
        )
        async with db_session_factory.begin() as session:
            early_claim = await store.claim_due(
                session,
                lease_owner=uuid.uuid4(),
                lease_for=timedelta(minutes=2),
                now=row.available_at - timedelta(microseconds=1),
            )
        assert early_claim is None, "the queue cannot claim before the App-mint deadline"


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
