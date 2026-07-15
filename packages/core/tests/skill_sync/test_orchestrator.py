"""Transport-level tests for daimon.core.skill_sync.orchestrator (first half).

Patterns enforced:
- Real `AsyncAnthropic` backed by `httpx.MockTransport` via `MARouter` — no
  AsyncMock on `client.beta.*`.
- SDK response objects constructed inline at every call site via real
  constructors (`SkillListResponse`, `VersionCreateResponse`) — no
  `model_construct`, no factory wrappers.
- Real Postgres via `db_session_factory`.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import re
import tarfile
import uuid

import httpx
import pytest
from anthropic import AsyncAnthropic
from anthropic.types.beta import (
    BetaManagedAgentsAgent,
    BetaManagedAgentsCustomSkill,
    SkillListResponse,
)
from anthropic.types.beta.skills import VersionCreateResponse
from cryptography.fernet import Fernet, MultiFernet
from daimon.core.defaults.metadata import tenant_scoped_display_title
from daimon.core.github_credentials import encrypt_token
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.skill_sync import orchestrator as orch_mod
from daimon.core.skill_sync.orchestrator import sync_agent_skills
from daimon.core.skill_zip import canonical_zip_bytes
from daimon.core.specs import SkillRepo
from daimon.core.stores.github_credentials import upsert_credential
from daimon.core.stores.user_skills import (
    list_user_skills_for_agent,
    load_user_skill,
    upsert_user_skill,
)
from daimon.testing.factories import make_cli_principal
from daimon.testing.ma import MARouter, list_response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# ---------------------------------------------------------------------------
# Helpers (intentionally minimal — no SDK constructor wrappers)
# ---------------------------------------------------------------------------


def _make_tarball(files: dict[str, bytes]) -> bytes:
    """Build a tar.gz with the given path → content mapping."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for path, content in files.items():
            info = tarfile.TarInfo(name=path)
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
    return buf.getvalue()


async def _seed_pat(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    fernet: MultiFernet,
    principal_id: uuid.UUID,
    plaintext: str = "test-pat",
) -> None:
    async with sessionmaker() as session, session.begin():
        await upsert_credential(
            session,
            principal_id=principal_id,
            github_login="tester",
            encrypted_token=encrypt_token(fernet, plaintext),
            scopes=("repo",),
        )


def _make_fernet() -> MultiFernet:
    return MultiFernet([Fernet(Fernet.generate_key())])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_tenant_scoped_display_title_uses_tenant_prefix_and_agent_slash_name() -> None:
    """Title scheme: {t8}-{agent_name}/{name}, tenant-prefixed (D-01/D-03)."""
    tenant_id = uuid.UUID("12345678-0000-0000-0000-000000000000")
    title = tenant_scoped_display_title(
        tenant_id=tenant_id, name="brainstorming", agent_name="my-agent"
    )
    assert title == "12345678-my-agent/brainstorming", "title must be {t8}-{agent_name}/{name}"


def test_tenant_scoped_display_title_truncates_over_64_chars() -> None:
    """Over-64-char titles are truncated+hashed, not hard-errored (D-03)."""
    tenant_id = uuid.UUID("abcdef12-0000-0000-0000-000000000000")
    long_agent = "a" * 50
    long_name = "n" * 20
    title = tenant_scoped_display_title(tenant_id=tenant_id, name=long_name, agent_name=long_agent)
    assert len(title) == 64, "truncated title must be exactly 64 chars"
    assert title.endswith(title[-5:]), "title must end with ~XXXX hash suffix"
    assert "~" in title, "mangle marker must be present"


def test_tenant_scoped_display_title_accepts_exact_64_char_boundary() -> None:
    """Exactly 64 chars passes through verbatim; 65 chars triggers mangle."""
    tenant_id = uuid.UUID("00000000-0000-0000-0000-000000000000")
    # prefix = "00000000-" (9 chars); body must fill exactly 55 chars for total 64
    body = "a" * 55
    title = tenant_scoped_display_title(tenant_id=tenant_id, name=body)
    assert len(title) == 64, "exactly 64 chars must pass through unchanged"
    assert "~" not in title, "no mangle for exactly 64 chars"

    # 56-char body → 9 + 56 = 65 → triggers mangle
    long_body = "a" * 56
    mangled = tenant_scoped_display_title(tenant_id=tenant_id, name=long_body)
    assert len(mangled) == 64, "mangled title must still be 64 chars"
    assert "~" in mangled, "mangle marker must appear when over 64 chars"


async def test_pat_missing_falls_back_to_unauthenticated_fetch(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """No credential row → fetcher proceeds without Authorization header.

    Public repos must work without a PAT (matches the chat path's
    daimon.core.skills.fetch behavior). Private repos return 404 from GitHub
    without a PAT, which the per-repo loop already records as `skipped_repos`.
    """
    cli = await make_cli_principal(db_session, os_user="alice")
    await db_session.commit()

    fernet = _make_fernet()
    router = MARouter()
    router.add("GET", r"/v1/agents", lambda req, _m: list_response([]))
    anthropic_client = _build_anthropic(router)

    captured_requests: list[httpx.Request] = []

    def tarball_handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(404)  # treat as private/missing; orchestrator should skip

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(tarball_handler))

    report = await sync_agent_skills(
        principal_id=cli.id,
        tenant_id=cli.tenant_id,
        agent_name="agent",
        repos=[SkillRepo(url="https://github.com/o/r", branch="main", split=False)],
        sessionmaker=db_session_factory,
        fernet=fernet,
        http_client=http_client,
        anthropic_client=anthropic_client,
    )

    assert len(captured_requests) == 1, "fetcher should be invoked even with no PAT"
    assert "Authorization" not in captured_requests[0].headers, (
        "missing PAT must NOT produce an Authorization header — that would 401 on public repos"
    )
    assert len(report.skipped_repos) == 1, "404-without-PAT should record skipped_repos"
    assert report.synced == 0, "no skill should be created when fetch fails"


async def test_first_sync_creates_new_skill(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Empty user_skills → POST /v1/skills called → row written with returned id."""
    cli = await make_cli_principal(db_session, os_user="alice")
    await db_session.commit()
    fernet = _make_fernet()
    await _seed_pat(sessionmaker=db_session_factory, fernet=fernet, principal_id=cli.id)

    tarball = _make_tarball({"r-main/SKILL.md": b"---\nname: r\ndescription: d\n---\nbody"})
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, content=tarball))
    )

    create_calls: list[httpx.Request] = []

    def on_create(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        create_calls.append(req)
        return httpx.Response(
            200,
            json=SkillListResponse(
                id="sk_new",
                type="custom",
                display_title="anything",
                latest_version="1",
                created_at="2026-04-21T00:00:00Z",
                updated_at="2026-04-21T00:00:00Z",
                source="custom",
            ).model_dump(mode="json"),
        )

    router = MARouter()
    router.add("POST", r"/v1/skills", on_create)
    # Attach step lookup — empty list = no agent on MA → attach skipped.
    router.add("GET", r"/v1/agents", lambda req, _m: list_response([]))
    anthropic_client = _build_anthropic(router)

    report = await sync_agent_skills(
        principal_id=cli.id,
        tenant_id=cli.tenant_id,
        agent_name="agent",
        repos=[SkillRepo(url="https://github.com/o/r", branch="main", split=False)],
        sessionmaker=db_session_factory,
        fernet=fernet,
        http_client=http_client,
        anthropic_client=anthropic_client,
    )

    assert report.synced == 1, f"expected one created skill, got report={report}"
    assert report.updated == 0
    assert report.failed_uploads == []
    assert len(create_calls) == 1, "skills.create should have been called once"

    # Row should reflect the returned id.
    # Bundled name is owner-qualified: github.com/o/r → "o-r" (Phase 45-01 fix).
    async with db_session_factory() as s, s.begin():
        row = await load_user_skill(
            s, tenant_id=cli.tenant_id, principal_id=cli.id, agent_name="agent", name="o-r"
        )
    assert row is not None, "user_skills row must exist after first-create"
    assert row.anthropic_id == "sk_new"
    assert row.anthropic_latest_version == "1"


async def test_subsequent_sync_with_changed_content_uploads_new_version(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Existing anthropic_id + new hash → POST /v1/skills/{id}/versions, row updated."""
    cli = await make_cli_principal(db_session, os_user="alice")
    await db_session.commit()
    fernet = _make_fernet()
    await _seed_pat(sessionmaker=db_session_factory, fernet=fernet, principal_id=cli.id)

    # Seed an existing row with a stale content_hash.
    # Bundled name is owner-qualified: github.com/o/r → "o-r" (Phase 45-01 fix).
    async with db_session_factory() as s, s.begin():
        await upsert_user_skill(
            s,
            tenant_id=cli.tenant_id,
            principal_id=cli.id,
            agent_name="agent",
            name="o-r",
            source_repo_url="https://github.com/o/r",
            source_repo_branch="main",
            source_path="",
            content_hash="hash_old_does_not_match",
            anthropic_id="sk_old",
            anthropic_latest_version="1",
        )

    tarball = _make_tarball(
        {"r-main/SKILL.md": b"---\nname: r\ndescription: changed\n---\nnew body"}
    )
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, content=tarball))
    )

    version_calls: list[httpx.Request] = []

    def on_version(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        version_calls.append(req)
        return httpx.Response(
            200,
            json=VersionCreateResponse(
                id="ver_2",
                skill_id="sk_old",
                version="2",
                type="skill_version",
                name="SKILL.zip",
                directory="/",
                description="",
                created_at="2026-04-21T00:00:00Z",
            ).model_dump(mode="json"),
        )

    router = MARouter()
    router.add("POST", r"/v1/skills/sk_old/versions", on_version)
    router.add("GET", r"/v1/agents", lambda req, _m: list_response([]))
    anthropic_client = _build_anthropic(router)

    report = await sync_agent_skills(
        principal_id=cli.id,
        tenant_id=cli.tenant_id,
        agent_name="agent",
        repos=[SkillRepo(url="https://github.com/o/r", branch="main", split=False)],
        sessionmaker=db_session_factory,
        fernet=fernet,
        http_client=http_client,
        anthropic_client=anthropic_client,
    )

    assert report.updated == 1, f"expected one version-create, got report={report}"
    assert report.synced == 0
    assert len(version_calls) == 1, "versions.create should have been called once"

    async with db_session_factory() as s, s.begin():
        row = await load_user_skill(
            s, tenant_id=cli.tenant_id, principal_id=cli.id, agent_name="agent", name="o-r"
        )
    assert row is not None
    assert row.anthropic_id == "sk_old", "anthropic_id must remain stable on version-create"
    assert row.anthropic_latest_version == "2"
    assert row.content_hash != "hash_old_does_not_match", "content_hash must update"


async def test_dedup_skips_upload_when_content_hash_matches(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Existing row's content_hash equals new build's hash → no MA call at all."""
    cli = await make_cli_principal(db_session, os_user="alice")
    await db_session.commit()
    fernet = _make_fernet()
    await _seed_pat(sessionmaker=db_session_factory, fernet=fernet, principal_id=cli.id)

    # Build the tarball + compute the resulting bundled-zip hash that the
    # orchestrator will see. We mirror what bundler.extract_and_bundle does for
    # split=False: build_bundled_zip on the repo root.
    tarball = _make_tarball({"r-main/SKILL.md": b"---\nname: r\ndescription: d\n---\nbody"})

    # Pre-compute the content_hash by running the same code path the orchestrator
    # uses, so we can seed an exact match.
    from pathlib import Path

    from daimon.core.skill_sync.bundler import extract_and_bundle

    extract_root = Path("/tmp") / f"daimon-test-dedup-{uuid.uuid4().hex}"
    extract_root.mkdir(parents=True, exist_ok=True)
    # Use owner-qualified repo_name to match the orchestrator's derivation for
    # github.com/o/r (Phase 45-01 fix): owner="o", repo="r" → repo_name="o-r".
    entries = await extract_and_bundle(
        tarball_bytes=tarball, extract_root=extract_root, repo_name="o-r", split=False
    )
    assert len(entries) == 1
    entry = entries[0]
    if entry.prebuilt_zip is not None:
        zip_bytes = entry.prebuilt_zip
    else:
        zip_bytes = canonical_zip_bytes(entry.skill_dir, arcname_prefix=entry.name)
    seed_hash = hashlib.sha256(zip_bytes).hexdigest()

    async with db_session_factory() as s, s.begin():
        await upsert_user_skill(
            s,
            tenant_id=cli.tenant_id,
            principal_id=cli.id,
            agent_name="agent",
            name=entry.name,
            source_repo_url="https://github.com/o/r",
            source_repo_branch="main",
            source_path="",
            content_hash=seed_hash,
            anthropic_id="sk_existing",
            anthropic_latest_version="1",
        )

    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, content=tarball))
    )

    ma_calls: list[httpx.Request] = []

    def record(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        ma_calls.append(req)
        return httpx.Response(500, json={"error": "should not be called"})

    router = MARouter()
    router.add("POST", r"/v1/skills", record)
    router.add("POST", r"/v1/skills/.*/versions", record)
    # Attach step (41-02) will look up the agent because a user_skill row
    # exists; return no match so the attach step skips without an update call.
    router.add("GET", r"/v1/agents", lambda req, _m: list_response([]))
    anthropic_client = _build_anthropic(router)

    report = await sync_agent_skills(
        principal_id=cli.id,
        tenant_id=cli.tenant_id,
        agent_name="agent",
        repos=[SkillRepo(url="https://github.com/o/r", branch="main", split=False)],
        sessionmaker=db_session_factory,
        fernet=fernet,
        http_client=http_client,
        anthropic_client=anthropic_client,
    )

    assert ma_calls == [], "dedup must short-circuit before any skill upload MA call"
    assert report.synced == 0
    assert report.updated == 0
    assert report.deleted == 0
    assert report.failed_uploads == []


async def test_re_run_no_changes_yields_zero_synced_zero_updated(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Idempotency: first run creates, second run no-ops via dedup."""
    cli = await make_cli_principal(db_session, os_user="alice")
    await db_session.commit()
    fernet = _make_fernet()
    await _seed_pat(sessionmaker=db_session_factory, fernet=fernet, principal_id=cli.id)

    tarball = _make_tarball({"r-main/SKILL.md": b"---\nname: r\ndescription: d\n---\nbody"})

    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, content=tarball))
    )

    create_count = 0

    def on_create(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        nonlocal create_count
        create_count += 1
        return httpx.Response(
            200,
            json=SkillListResponse(
                id="sk_first",
                type="custom",
                display_title="anything",
                latest_version="1",
                created_at="2026-04-21T00:00:00Z",
                updated_at="2026-04-21T00:00:00Z",
                source="custom",
            ).model_dump(mode="json"),
        )

    router = MARouter()
    router.add("POST", r"/v1/skills", on_create)
    router.add("GET", r"/v1/agents", lambda req, _m: list_response([]))
    anthropic_client = _build_anthropic(router)

    repos = [SkillRepo(url="https://github.com/o/r", branch="main", split=False)]
    first = await sync_agent_skills(
        principal_id=cli.id,
        tenant_id=cli.tenant_id,
        agent_name="agent",
        repos=repos,
        sessionmaker=db_session_factory,
        fernet=fernet,
        http_client=http_client,
        anthropic_client=anthropic_client,
    )
    assert first.synced == 1, "first run should create"

    second = await sync_agent_skills(
        principal_id=cli.id,
        tenant_id=cli.tenant_id,
        agent_name="agent",
        repos=repos,
        sessionmaker=db_session_factory,
        fernet=fernet,
        http_client=http_client,
        anthropic_client=anthropic_client,
    )
    assert second.synced == 0, "re-run must not create"
    assert second.updated == 0, "re-run must not version-create"
    assert second.deleted == 0, "orphan-delete is no-op until 33-07b"
    assert create_count == 1, "POST /v1/skills must have fired exactly once across both runs"


async def test_upload_concurrency_capped_at_six(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Twelve split-mode skills → at most 6 _process_one tasks in flight at once."""
    cli = await make_cli_principal(db_session, os_user="alice")
    await db_session.commit()
    fernet = _make_fernet()
    await _seed_pat(sessionmaker=db_session_factory, fernet=fernet, principal_id=cli.id)

    # 12 skills via split-mode tarball — one SKILL.md per directory.
    files: dict[str, bytes] = {}
    for i in range(12):
        files[f"r-main/skill_{i:02d}/SKILL.md"] = (
            f"---\nname: skill_{i:02d}\ndescription: d\n---\n".encode()
        )
    tarball = _make_tarball(files)

    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, content=tarball))
    )

    def on_create(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        return httpx.Response(
            200,
            json=SkillListResponse(
                id="sk_x",
                type="custom",
                display_title="x",
                latest_version="1",
                created_at="2026-04-21T00:00:00Z",
                updated_at="2026-04-21T00:00:00Z",
                source="custom",
            ).model_dump(mode="json"),
        )

    router = MARouter()
    router.add("POST", r"/v1/skills", on_create)
    router.add("GET", r"/v1/agents", lambda req, _m: list_response([]))
    anthropic_client = _build_anthropic(router)

    in_flight = 0
    peak = 0
    invocations = 0
    lock = asyncio.Lock()

    # Stub _process_one entirely. We're testing _upload_all's semaphore cap,
    # not _process_one's DB work. Driving 12 concurrent real DB writes through
    # the test fixture's single-connection session_factory would serialize on
    # asyncpg ("another operation is in progress"), masking the orchestration
    # concurrency we want to observe.
    async def stubbed(**kwargs: object) -> None:
        nonlocal in_flight, peak, invocations
        async with lock:
            in_flight += 1
            invocations += 1
            peak = max(peak, in_flight)
        try:
            await asyncio.sleep(0.05)
        finally:
            async with lock:
                in_flight -= 1

    monkeypatch.setattr(orch_mod, "_process_one", stubbed)

    report = await sync_agent_skills(
        principal_id=cli.id,
        tenant_id=cli.tenant_id,
        agent_name="agent",
        repos=[SkillRepo(url="https://github.com/o/r", branch="main", split=True)],
        sessionmaker=db_session_factory,
        fernet=fernet,
        http_client=http_client,
        anthropic_client=anthropic_client,
    )

    assert peak <= 6, f"concurrency cap of 6 must hold, got peak={peak}"
    assert peak >= 2, f"with 12 skills + 50ms sleep we should observe >1 in flight, got {peak}"
    assert invocations == 12, f"all 12 skills must be processed, got {invocations}"
    assert report.failed_uploads == [], f"stub should not raise, got {report.failed_uploads}"


async def test_per_skill_timeout_isolates_failure(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One hang → that skill lands in failed_uploads; siblings still complete.

    Stubs _process_one (same pattern as the concurrency test): the timeout
    contract under test lives in _upload_all (`asyncio.wait_for` + the
    failed_uploads append on TimeoutError). Driving it with the real
    _process_one would mix in DB-fixture serialization noise without making
    the timeout assertion any stronger.
    """
    cli = await make_cli_principal(db_session, os_user="alice")
    await db_session.commit()
    fernet = _make_fernet()
    await _seed_pat(sessionmaker=db_session_factory, fernet=fernet, principal_id=cli.id)

    monkeypatch.setattr(orch_mod, "_PER_SKILL_TIMEOUT_S", 0.05)

    # Two split-mode skills.
    tarball = _make_tarball(
        {
            "r-main/skill-hangs/SKILL.md": b"---\nname: skill-hangs\ndescription: d\n---\n",
            "r-main/skill-fast/SKILL.md": b"---\nname: skill-fast\ndescription: d\n---\n",
        }
    )
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, content=tarball))
    )

    async def stubbed(**kwargs: object) -> None:
        from daimon.core.skill_sync.orchestrator import _PendingSkill

        pending = kwargs["pending"]
        assert isinstance(pending, _PendingSkill)
        if pending.name == "skill-hangs":
            await asyncio.sleep(1.0)  # > 0.05 timeout
        # skill-fast falls through and returns immediately → counts as success
        # (no failed_uploads append)

    monkeypatch.setattr(orch_mod, "_process_one", stubbed)

    # _process_one is stubbed; only the per-agent get_pat lookup hits MA.
    router = MARouter()
    router.add("GET", r"/v1/agents", lambda req, _m: list_response([]))
    anthropic_client = _build_anthropic(router)

    report = await sync_agent_skills(
        principal_id=cli.id,
        tenant_id=cli.tenant_id,
        agent_name="agent",
        repos=[SkillRepo(url="https://github.com/o/r", branch="main", split=True)],
        sessionmaker=db_session_factory,
        fernet=fernet,
        http_client=http_client,
        anthropic_client=anthropic_client,
    )

    assert len(report.failed_uploads) == 1, (
        f"exactly one skill should time out; got failed_uploads={report.failed_uploads}"
    )
    failed_name, failed_msg = report.failed_uploads[0]
    assert failed_name == "skill-hangs", f"the hanging skill must be flagged, got {failed_name}"
    assert failed_msg == "timeout after 0s", (
        f"timeout message must use the formatted constant, got {failed_msg!r}"
    )


# ---------------------------------------------------------------------------
# Local helpers — no SDK constructor wrapping; transport assembly only
# ---------------------------------------------------------------------------


def _build_anthropic(router: MARouter) -> AsyncAnthropic:
    transport = httpx.MockTransport(router.dispatch)
    http_client = httpx.AsyncClient(transport=transport, base_url="https://api.anthropic.com")
    return AsyncAnthropic(api_key="test", http_client=http_client)


def _unreachable_handler(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"http_client should not have been used (request to {request.url})")


# Verify the in-listed signature (existing tests above use list_user_skills_for_agent
# implicitly only on re-run path; keep the import so future 07b tests inherit it).
_ = list_user_skills_for_agent


# ---------------------------------------------------------------------------
# 33-07b: duplicate-display-title recovery + orphan-delete
# ---------------------------------------------------------------------------


def _conflict_response(_req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
    """Duplicate-display_title rejection in the real MA shape.

    Verified by scripts/probes/managed_agents/dup_display_title.py (2026-05-09):
    status_code=400, body['error']['type']='invalid_request_error',
    message='Skill cannot reuse an existing display_title: <title>'.
    """
    return httpx.Response(
        400,
        json={
            "type": "error",
            "error": {
                "type": "invalid_request_error",
                "message": (
                    "Skill cannot reuse an existing display_title: "
                    "test-principal id:test-agent/widget"
                ),
            },
        },
    )


def _list_envelope(items: list[SkillListResponse]) -> httpx.Response:
    """MA list envelope: {data: [...], next_page: null}."""
    return httpx.Response(
        200,
        json={
            "data": [item.model_dump(mode="json") for item in items],
            "next_page": None,
        },
    )


async def test_duplicate_display_title_recovery_lands_a_new_version(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """409 on skills.create → find by display_title → versions.create on recovered id."""
    cli = await make_cli_principal(db_session, os_user="alice")
    await db_session.commit()
    fernet = _make_fernet()
    await _seed_pat(sessionmaker=db_session_factory, fernet=fernet, principal_id=cli.id)

    tarball = _make_tarball({"r-main/SKILL.md": b"---\nname: r\ndescription: d\n---\nbody"})
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, content=tarball))
    )

    # The display_title that recovery will look up — tenant-scoped (D-01/D-03).
    # Bundled name for github.com/o/r is "o-r" after Phase 45-01 owner-qualified fix.
    formatted_title = tenant_scoped_display_title(
        tenant_id=cli.tenant_id, name="o-r", agent_name="agent"
    )

    create_calls: list[httpx.Request] = []
    list_calls: list[httpx.Request] = []
    version_calls: list[httpx.Request] = []

    def on_create(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        create_calls.append(req)
        return _conflict_response(req, _m)

    def on_list(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        list_calls.append(req)
        return _list_envelope(
            [
                SkillListResponse(
                    id="sk_orphan",
                    type="custom",
                    display_title=formatted_title,
                    latest_version="3",
                    created_at="2026-04-21T00:00:00Z",
                    updated_at="2026-04-21T00:00:00Z",
                    source="custom",
                )
            ]
        )

    def on_version(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        version_calls.append(req)
        return httpx.Response(
            200,
            json=VersionCreateResponse(
                id="ver_4",
                skill_id="sk_orphan",
                version="4",
                type="skill_version",
                name="SKILL.zip",
                directory="/",
                description="",
                created_at="2026-04-21T00:00:00Z",
            ).model_dump(mode="json"),
        )

    router = MARouter()
    router.add("POST", r"/v1/skills", on_create)
    router.add("GET", r"/v1/skills", on_list)
    router.add("POST", r"/v1/skills/sk_orphan/versions", on_version)
    router.add("GET", r"/v1/agents", lambda req, _m: list_response([]))
    anthropic_client = _build_anthropic(router)

    report = await sync_agent_skills(
        principal_id=cli.id,
        tenant_id=cli.tenant_id,
        agent_name="agent",
        repos=[SkillRepo(url="https://github.com/o/r", branch="main", split=False)],
        sessionmaker=db_session_factory,
        fernet=fernet,
        http_client=http_client,
        anthropic_client=anthropic_client,
    )

    assert report.synced == 0, "duplicate-title recovery is a version-create, not a sync"
    assert report.updated == 1, f"expected one recovered version, got {report}"
    assert report.failed_uploads == [], f"recovery must succeed, got {report.failed_uploads}"
    assert len(create_calls) >= 1, "skills.create must have been attempted (SDK may retry on 400)"
    assert len(list_calls) >= 1, "find_skill_by_display_title must page skills.list"
    assert len(version_calls) == 1, "versions.create must fire on the recovered id"

    async with db_session_factory() as s, s.begin():
        row = await load_user_skill(
            s, tenant_id=cli.tenant_id, principal_id=cli.id, agent_name="agent", name="o-r"
        )
    assert row is not None, "user_skills row must exist after recovery"
    assert row.anthropic_id == "sk_orphan", "recovered id must be persisted"
    assert row.anthropic_latest_version == "4"


async def test_recovery_raises_skills_list_truncated_error_on_full_page(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Full skills page during recovery raises SkillsListTruncatedError → lands in failed_uploads.

    With on_truncation="raise", find_skill_by_display_title raises SkillsListTruncatedError
    when the page is full (D-13). The _upload_all except-Exception boundary records it in
    report.failed_uploads, so the failure is observable without silent data corruption.
    """
    cli = await make_cli_principal(db_session, os_user="alice")
    await db_session.commit()
    fernet = _make_fernet()
    await _seed_pat(sessionmaker=db_session_factory, fernet=fernet, principal_id=cli.id)

    tarball = _make_tarball({"r-main/SKILL.md": b"---\nname: r\ndescription: d\n---\nbody"})
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, content=tarball))
    )

    # 100 rows (a full page) — triggers SkillsListTruncatedError in strict mode.
    full_page = [
        SkillListResponse(
            id=f"sk_{i:03d}",
            type="custom",
            display_title=f"unrelated-{i}",
            latest_version="1",
            created_at="2026-04-21T00:00:00Z",
            updated_at="2026-04-21T00:00:00Z",
            source="custom",
        )
        for i in range(100)
    ]

    def on_create(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        return _conflict_response(req, _m)

    def on_list(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        return _list_envelope(full_page)

    router = MARouter()
    router.add("POST", r"/v1/skills", on_create)
    router.add("GET", r"/v1/skills", on_list)
    router.add("GET", r"/v1/agents", lambda req, _m: list_response([]))
    anthropic_client = _build_anthropic(router)

    report = await sync_agent_skills(
        principal_id=cli.id,
        tenant_id=cli.tenant_id,
        agent_name="agent",
        repos=[SkillRepo(url="https://github.com/o/r", branch="main", split=False)],
        sessionmaker=db_session_factory,
        fernet=fernet,
        http_client=http_client,
        anthropic_client=anthropic_client,
    )

    assert report.synced == 0, "no skill should have been created"
    assert report.updated == 0, "no version-create should have fired"
    assert len(report.failed_uploads) == 1, (
        f"truncated-list error must be recorded as a failed upload, got {report.failed_uploads}"
    )
    failed_name, failed_msg = report.failed_uploads[0]
    assert failed_name == "o-r", f"failed skill name must be 'o-r', got {failed_name!r}"
    assert "truncated" in failed_msg.lower() or "full page" in failed_msg.lower(), (
        f"failure reason must reference the truncation, got {failed_msg!r}"
    )


async def test_non_duplicate_api_status_error_re_raises_to_failed_uploads(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A non-409, non-duplicate-message error skips recovery and lands in failed_uploads."""
    cli = await make_cli_principal(db_session, os_user="alice")
    await db_session.commit()
    fernet = _make_fernet()
    await _seed_pat(sessionmaker=db_session_factory, fernet=fernet, principal_id=cli.id)

    tarball = _make_tarball({"r-main/SKILL.md": b"---\nname: r\ndescription: d\n---\nbody"})
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, content=tarball))
    )

    list_calls: list[httpx.Request] = []

    def on_create(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        # 500 with a message that does not look like a duplicate-title rejection.
        return httpx.Response(
            500,
            json={
                "type": "error",
                "error": {
                    "type": "api_error",
                    "message": "internal server error",
                },
            },
        )

    def on_list(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        list_calls.append(req)
        return _list_envelope([])

    router = MARouter()
    router.add("POST", r"/v1/skills", on_create)
    router.add("GET", r"/v1/skills", on_list)
    router.add("GET", r"/v1/agents", lambda req, _m: list_response([]))
    anthropic_client = _build_anthropic(router)

    report = await sync_agent_skills(
        principal_id=cli.id,
        tenant_id=cli.tenant_id,
        agent_name="agent",
        repos=[SkillRepo(url="https://github.com/o/r", branch="main", split=False)],
        sessionmaker=db_session_factory,
        fernet=fernet,
        http_client=http_client,
        anthropic_client=anthropic_client,
    )

    assert report.synced == 0
    assert report.updated == 0
    assert len(report.failed_uploads) == 1, (
        f"non-duplicate error must be recorded as failed upload, got {report.failed_uploads}"
    )
    assert list_calls == [], (
        "recovery MUST NOT trigger find_skill_by_display_title for non-duplicate errors"
    )


async def test_400_with_display_title_substring_does_not_trigger_recovery(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A 400 mentioning 'display_title' as a parameter (not duplicate) must NOT recover.

    Regression — live spike (sync_agent_skills_live.py, 2026-05-09) showed
    `display_title must be at most 64 characters long` was misclassified as
    duplicate-title because the heuristic loosely matched any 400 mentioning
    display_title. The tightened heuristic requires the exact phrase
    'cannot reuse an existing display_title'.
    """
    cli = await make_cli_principal(db_session, os_user="alice")
    await db_session.commit()
    fernet = _make_fernet()
    await _seed_pat(sessionmaker=db_session_factory, fernet=fernet, principal_id=cli.id)

    tarball = _make_tarball({"r-main/SKILL.md": b"---\nname: r\ndescription: d\n---\nbody"})
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, content=tarball))
    )

    list_calls: list[httpx.Request] = []

    def on_create(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "type": "error",
                "error": {
                    "type": "invalid_request_error",
                    "message": "display_title must be at most 64 characters long",
                },
            },
        )

    def on_list(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        list_calls.append(req)
        return _list_envelope([])

    router = MARouter()
    router.add("POST", r"/v1/skills", on_create)
    router.add("GET", r"/v1/skills", on_list)
    router.add("GET", r"/v1/agents", lambda req, _m: list_response([]))
    anthropic_client = _build_anthropic(router)

    report = await sync_agent_skills(
        principal_id=cli.id,
        tenant_id=cli.tenant_id,
        agent_name="agent",
        repos=[SkillRepo(url="https://github.com/o/r", branch="main", split=False)],
        sessionmaker=db_session_factory,
        fernet=fernet,
        http_client=http_client,
        anthropic_client=anthropic_client,
    )

    assert report.synced == 0
    assert report.updated == 0
    assert len(report.failed_uploads) == 1, (
        "length-rejection must be recorded as failed_upload, not silently recovered"
    )
    assert list_calls == [], "recovery MUST NOT trigger for 400s that aren't the dup-title phrase"


async def test_orphan_delete_only_fires_for_successfully_fetched_repos(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """r1 fetched + empty (no skills) → r1's row orphan-deleted. r2 not in repos → untouched."""
    cli = await make_cli_principal(db_session, os_user="alice")
    await db_session.commit()
    fernet = _make_fernet()
    await _seed_pat(sessionmaker=db_session_factory, fernet=fernet, principal_id=cli.id)

    # Seed two existing rows: one for r1 (will be orphaned), one for r2 (untouched).
    async with db_session_factory() as s, s.begin():
        await upsert_user_skill(
            s,
            tenant_id=cli.tenant_id,
            principal_id=cli.id,
            agent_name="agent",
            name="orphan_from_r1",
            source_repo_url="https://github.com/o/r1",
            source_repo_branch="main",
            source_path="",
            content_hash="hash_r1",
            anthropic_id="sk_r1",
            anthropic_latest_version="1",
        )
        await upsert_user_skill(
            s,
            tenant_id=cli.tenant_id,
            principal_id=cli.id,
            agent_name="agent",
            name="kept_from_r2",
            source_repo_url="https://github.com/o/r2",
            source_repo_branch="main",
            source_path="",
            content_hash="hash_r2",
            anthropic_id="sk_r2",
            anthropic_latest_version="1",
        )

    # r1 fetches an empty tarball — no SKILL.md, split=True → bundler returns [].
    empty_tarball = _make_tarball({"r1-main/README.md": b"no skills here"})
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, content=empty_tarball))
    )

    delete_calls: list[httpx.Request] = []

    def on_delete(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        delete_calls.append(req)
        return httpx.Response(204)

    router = MARouter()
    router.add("DELETE", r"/v1/skills/sk_r1", on_delete)
    router.add("DELETE", r"/v1/skills/sk_r2", on_delete)
    # Attach step (41-02): kept_from_r2 user_skills row remains → lookup fires.
    router.add("GET", r"/v1/agents", lambda req, _m: list_response([]))
    anthropic_client = _build_anthropic(router)

    report = await sync_agent_skills(
        principal_id=cli.id,
        tenant_id=cli.tenant_id,
        agent_name="agent",
        repos=[SkillRepo(url="https://github.com/o/r1", branch="main", split=True)],
        sessionmaker=db_session_factory,
        fernet=fernet,
        http_client=http_client,
        anthropic_client=anthropic_client,
    )

    assert report.deleted == 1, f"exactly one orphan-delete expected, got {report}"
    assert len(delete_calls) == 1, "only sk_r1 should be deleted on MA"
    assert delete_calls[0].url.path.endswith("/sk_r1"), (
        f"the delete must target sk_r1, got {delete_calls[0].url.path}"
    )

    async with db_session_factory() as s, s.begin():
        row_r1 = await load_user_skill(
            s,
            tenant_id=cli.tenant_id,
            principal_id=cli.id,
            agent_name="agent",
            name="orphan_from_r1",
        )
        row_r2 = await load_user_skill(
            s,
            tenant_id=cli.tenant_id,
            principal_id=cli.id,
            agent_name="agent",
            name="kept_from_r2",
        )
    assert row_r1 is None, "orphan row from r1 must be removed locally"
    assert row_r2 is not None, "row from un-synced r2 must be left alone"


async def test_orphan_delete_skipped_when_repo_fetch_failed(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """GitHub returns 503 → repo not in successfully_fetched → row preserved, no MA delete."""
    cli = await make_cli_principal(db_session, os_user="alice")
    await db_session.commit()
    fernet = _make_fernet()
    await _seed_pat(sessionmaker=db_session_factory, fernet=fernet, principal_id=cli.id)

    async with db_session_factory() as s, s.begin():
        await upsert_user_skill(
            s,
            tenant_id=cli.tenant_id,
            principal_id=cli.id,
            agent_name="agent",
            name="transient_orphan",
            source_repo_url="https://github.com/o/r1",
            source_repo_branch="main",
            source_path="",
            content_hash="hash_r1",
            anthropic_id="sk_transient",
            anthropic_latest_version="1",
        )

    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(503, json={"message": "service unavailable"})
        )
    )

    ma_requests: list[httpx.Request] = []

    def record_any(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        ma_requests.append(req)
        return httpx.Response(500, json={"error": "must not be called"})

    router = MARouter()
    router.add("DELETE", r"/v1/skills/.*", record_any)
    # Attach step (41-02): transient_orphan row survives the fetch failure →
    # lookup fires. Return no match so the attach step skips without update.
    router.add("GET", r"/v1/agents", lambda req, _m: list_response([]))
    anthropic_client = _build_anthropic(router)

    report = await sync_agent_skills(
        principal_id=cli.id,
        tenant_id=cli.tenant_id,
        agent_name="agent",
        repos=[SkillRepo(url="https://github.com/o/r1", branch="main", split=False)],
        sessionmaker=db_session_factory,
        fernet=fernet,
        http_client=http_client,
        anthropic_client=anthropic_client,
    )

    assert report.deleted == 0, "transient outages must not trigger orphan-delete"
    assert len(report.skipped_repos) == 1, (
        f"r1 must be recorded as skipped, got {report.skipped_repos}"
    )
    assert ma_requests == [], "no MA delete may fire when repo fetch fails"

    async with db_session_factory() as s, s.begin():
        row = await load_user_skill(
            s,
            tenant_id=cli.tenant_id,
            principal_id=cli.id,
            agent_name="agent",
            name="transient_orphan",
        )
    assert row is not None, "row tied to a transient-outage repo must be preserved"


async def test_orphan_delete_local_row_removed_even_when_ma_delete_fails(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """MA delete returns 500 → warning logged, failed_uploads recorded, local row still removed."""
    cli = await make_cli_principal(db_session, os_user="alice")
    await db_session.commit()
    fernet = _make_fernet()
    await _seed_pat(sessionmaker=db_session_factory, fernet=fernet, principal_id=cli.id)

    async with db_session_factory() as s, s.begin():
        await upsert_user_skill(
            s,
            tenant_id=cli.tenant_id,
            principal_id=cli.id,
            agent_name="agent",
            name="doomed_orphan",
            source_repo_url="https://github.com/o/r1",
            source_repo_branch="main",
            source_path="",
            content_hash="hash_r1",
            anthropic_id="sk_doomed",
            anthropic_latest_version="1",
        )

    empty_tarball = _make_tarball({"r1-main/README.md": b"no skills"})
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, content=empty_tarball))
    )

    delete_calls: list[httpx.Request] = []

    def on_delete(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        delete_calls.append(req)
        return httpx.Response(
            500,
            json={
                "type": "error",
                "error": {"type": "api_error", "message": "boom"},
            },
        )

    router = MARouter()
    router.add("DELETE", r"/v1/skills/sk_doomed", on_delete)
    router.add("GET", r"/v1/agents", lambda req, _m: list_response([]))
    anthropic_client = _build_anthropic(router)

    report = await sync_agent_skills(
        principal_id=cli.id,
        tenant_id=cli.tenant_id,
        agent_name="agent",
        repos=[SkillRepo(url="https://github.com/o/r1", branch="main", split=True)],
        sessionmaker=db_session_factory,
        fernet=fernet,
        http_client=http_client,
        anthropic_client=anthropic_client,
    )

    assert report.deleted == 1, "deleted counter increments even when MA delete fails"
    assert len(delete_calls) >= 1, "MA delete must have been attempted (SDK may retry on 5xx)"
    assert any(name == "doomed_orphan" for name, _ in report.failed_uploads), (
        f"MA-delete failure must be recorded, got {report.failed_uploads}"
    )

    async with db_session_factory() as s, s.begin():
        row = await load_user_skill(
            s,
            tenant_id=cli.tenant_id,
            principal_id=cli.id,
            agent_name="agent",
            name="doomed_orphan",
        )
    assert row is None, "local row must be removed even when MA delete fails"

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "skill_sync.orphan_delete_ma_failed" in combined, (
        f"MA-delete failure warning must be emitted; captured={combined!r}"
    )


# ---------------------------------------------------------------------------
# 41-02: attach uploaded skills onto MA agent (#40)
# ---------------------------------------------------------------------------


async def test_sync_agent_skills_attaches_uploaded_skills_to_ma_agent(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """After upload, agents.update is called with the newly-created skill_id."""
    cli = await make_cli_principal(db_session, os_user="alice")
    await db_session.commit()
    fernet = _make_fernet()
    await _seed_pat(sessionmaker=db_session_factory, fernet=fernet, principal_id=cli.id)

    tarball = _make_tarball({"r-main/SKILL.md": b"---\nname: r\ndescription: d\n---\nbody"})
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, content=tarball))
    )

    def on_create(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        return httpx.Response(
            200,
            json=SkillListResponse(
                id="sk_new",
                type="custom",
                display_title="agent/r",
                latest_version="1",
                created_at="2026-04-21T00:00:00Z",
                updated_at="2026-04-21T00:00:00Z",
                source="custom",
            ).model_dump(mode="json"),
        )

    def on_list_agents(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        return list_response(
            [
                BetaManagedAgentsAgent(
                    id="ag_target",
                    type="agent",
                    name="agent",
                    model={"id": "claude-opus-4-7"},
                    metadata={
                        "daimon_tenant": str(cli.tenant_id),
                        "daimon_name": "agent",
                    },
                    description=None,
                    created_at="2026-04-21T00:00:00Z",
                    updated_at="2026-04-21T00:00:00Z",
                    version=7,
                    mcp_servers=[],
                    skills=[],
                    tools=[],
                    system=None,
                ).model_dump(mode="json")
            ]
        )

    update_calls: list[dict[str, object]] = []

    def on_update_agent(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        update_calls.append(json.loads(req.content))
        return httpx.Response(
            200,
            json=BetaManagedAgentsAgent(
                id="ag_target",
                type="agent",
                name="agent",
                model={"id": "claude-opus-4-7"},
                metadata={
                    "daimon_tenant": str(cli.tenant_id),
                    "daimon_name": "agent",
                },
                description=None,
                created_at="2026-04-21T00:00:00Z",
                updated_at="2026-04-21T00:00:00Z",
                version=8,
                mcp_servers=[],
                skills=[
                    BetaManagedAgentsCustomSkill(skill_id="sk_new", type="custom", version="1")
                ],
                tools=[],
                system=None,
            ).model_dump(mode="json"),
        )

    # update_agent_with_version_retry calls agents.retrieve before the update.
    agent_payload = BetaManagedAgentsAgent(
        id="ag_target",
        type="agent",
        name="agent",
        model={"id": "claude-opus-4-7"},
        metadata={
            "daimon_tenant": str(cli.tenant_id),
            "daimon_name": "agent",
        },
        description=None,
        created_at="2026-04-21T00:00:00Z",
        updated_at="2026-04-21T00:00:00Z",
        version=7,
        mcp_servers=[],
        skills=[],
        tools=[],
        system=None,
    ).model_dump(mode="json")

    def on_retrieve_agent(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        return httpx.Response(200, json=agent_payload)

    router = MARouter()
    router.add("POST", r"/v1/skills", on_create)
    router.add("GET", r"/v1/agents", on_list_agents)
    router.add("GET", r"/v1/agents/ag_target", on_retrieve_agent)
    router.add("POST", r"/v1/agents/ag_target", on_update_agent)
    anthropic_client = _build_anthropic(router)

    report = await sync_agent_skills(
        principal_id=cli.id,
        tenant_id=cli.tenant_id,
        agent_name="agent",
        repos=[SkillRepo(url="https://github.com/o/r", branch="main", split=False)],
        sessionmaker=db_session_factory,
        fernet=fernet,
        http_client=http_client,
        anthropic_client=anthropic_client,
    )

    assert report.synced == 1, f"expected one created skill, got report={report}"
    assert len(update_calls) == 1, (
        f"agents.update must be called exactly once after upload, got {len(update_calls)}"
    )
    body = update_calls[0]
    assert body.get("version") == 7, (
        "agents.update must echo the agent's current version for concurrency control"
    )
    assert body.get("skills") == [{"type": "custom", "skill_id": "sk_new"}], (
        f"agents.update body must contain only the newly-created skill, got {body.get('skills')}"
    )


async def test_sync_agent_skills_attach_is_noop_when_skill_already_attached(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Dedup path + existing on-MA skill matches user_skill row → no agents.update call."""
    cli = await make_cli_principal(db_session, os_user="alice")
    await db_session.commit()
    fernet = _make_fernet()
    await _seed_pat(sessionmaker=db_session_factory, fernet=fernet, principal_id=cli.id)

    # CR-01: the agent ("ag_target") is present on MA, so the user_skills ledger is
    # keyed on the agent's stable derived identity (NOT the caller principal). Seed the
    # dedup row under that key so the orchestrator's load_user_skill finds it.
    ledger_key = derive_agent_uuid(tenant_id=cli.tenant_id, ma_agent_id="ag_target")

    # Seed user_skills with anthropic_id=sk_existing matching content hash so
    # upload short-circuits via dedup.
    from pathlib import Path

    from daimon.core.skill_sync.bundler import extract_and_bundle

    tarball = _make_tarball({"r-main/SKILL.md": b"---\nname: r\ndescription: d\n---\nbody"})
    extract_root = Path("/tmp") / f"daimon-test-attach-noop-{uuid.uuid4().hex}"
    extract_root.mkdir(parents=True, exist_ok=True)
    # Use owner-qualified repo_name to match the orchestrator's derivation for
    # github.com/o/r (Phase 45-01 fix): owner="o", repo="r" → repo_name="o-r".
    entries = await extract_and_bundle(
        tarball_bytes=tarball, extract_root=extract_root, repo_name="o-r", split=False
    )
    entry = entries[0]
    if entry.prebuilt_zip is not None:
        zip_bytes = entry.prebuilt_zip
    else:
        zip_bytes = canonical_zip_bytes(entry.skill_dir, arcname_prefix=entry.name)
    seed_hash = hashlib.sha256(zip_bytes).hexdigest()

    async with db_session_factory() as s, s.begin():
        await upsert_user_skill(
            s,
            tenant_id=cli.tenant_id,
            principal_id=ledger_key,
            agent_name="agent",
            name=entry.name,
            source_repo_url="https://github.com/o/r",
            source_repo_branch="main",
            source_path="",
            content_hash=seed_hash,
            anthropic_id="sk_existing",
            anthropic_latest_version="1",
        )

    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, content=tarball))
    )

    update_calls: list[httpx.Request] = []

    def on_update_agent(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        update_calls.append(req)
        return httpx.Response(500, json={"error": "must not be called"})

    def on_list_agents(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        return list_response(
            [
                BetaManagedAgentsAgent(
                    id="ag_target",
                    type="agent",
                    name="agent",
                    model={"id": "claude-opus-4-7"},
                    metadata={
                        "daimon_tenant": str(cli.tenant_id),
                        "daimon_name": "agent",
                    },
                    description=None,
                    created_at="2026-04-21T00:00:00Z",
                    updated_at="2026-04-21T00:00:00Z",
                    version=7,
                    mcp_servers=[],
                    skills=[
                        BetaManagedAgentsCustomSkill(
                            skill_id="sk_existing", type="custom", version="1"
                        )
                    ],
                    tools=[],
                    system=None,
                ).model_dump(mode="json")
            ]
        )

    router = MARouter()
    router.add("GET", r"/v1/agents", on_list_agents)
    router.add("POST", r"/v1/agents/ag_target", on_update_agent)
    anthropic_client = _build_anthropic(router)

    report = await sync_agent_skills(
        principal_id=cli.id,
        tenant_id=cli.tenant_id,
        agent_name="agent",
        repos=[SkillRepo(url="https://github.com/o/r", branch="main", split=False)],
        sessionmaker=db_session_factory,
        fernet=fernet,
        http_client=http_client,
        anthropic_client=anthropic_client,
    )

    assert report.synced == 0
    assert report.updated == 0
    assert report.failed_uploads == [], (
        "dedup must short-circuit cleanly (no skills.create attempt); "
        f"got failed_uploads={report.failed_uploads}"
    )
    assert update_calls == [], (
        "agents.update MUST be skipped when union equals existing on-MA skills "
        f"(noop idempotency); got {len(update_calls)} calls"
    )


async def test_sync_agent_skills_skips_attach_when_agent_not_found(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """find_agent_by_daimon_tag returns None → warning logged, no agents.update."""
    cli = await make_cli_principal(db_session, os_user="alice")
    await db_session.commit()
    fernet = _make_fernet()
    await _seed_pat(sessionmaker=db_session_factory, fernet=fernet, principal_id=cli.id)

    tarball = _make_tarball({"r-main/SKILL.md": b"---\nname: r\ndescription: d\n---\nbody"})
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, content=tarball))
    )

    def on_create(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        return httpx.Response(
            200,
            json=SkillListResponse(
                id="sk_new",
                type="custom",
                display_title="agent/r",
                latest_version="1",
                created_at="2026-04-21T00:00:00Z",
                updated_at="2026-04-21T00:00:00Z",
                source="custom",
            ).model_dump(mode="json"),
        )

    # MA list_agents returns no match for this tenant/name.
    def on_list_agents(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        return list_response([])

    update_calls: list[httpx.Request] = []

    def on_update_agent(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        update_calls.append(req)
        return httpx.Response(500, json={"error": "must not be called"})

    router = MARouter()
    router.add("POST", r"/v1/skills", on_create)
    router.add("GET", r"/v1/agents", on_list_agents)
    router.add("POST", r"/v1/agents/.*", on_update_agent)
    anthropic_client = _build_anthropic(router)

    report = await sync_agent_skills(
        principal_id=cli.id,
        tenant_id=cli.tenant_id,
        agent_name="agent",
        repos=[SkillRepo(url="https://github.com/o/r", branch="main", split=False)],
        sessionmaker=db_session_factory,
        fernet=fernet,
        http_client=http_client,
        anthropic_client=anthropic_client,
    )

    assert report.synced == 1, (
        f"upload must succeed even when the agent isn't on MA yet; got {report}"
    )
    assert update_calls == [], (
        "agents.update MUST NOT fire when the agent doesn't resolve "
        f"on MA; got {len(update_calls)} calls"
    )

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "skill_sync.attach_skipped_no_agent" in combined, (
        f"warning must be emitted on missing agent; captured={combined!r}"
    )


# ---------------------------------------------------------------------------
# Phase 45-01: bundled name-collision regression (PHASE-45-ORPHAN-01)
# ---------------------------------------------------------------------------


async def test_bundled_sync_two_repos_same_trailing_segment_keeps_both_skills(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two bundled repos with colliding trailing URL segments must NOT share a user_skills row.

    Mechanism B bug (45-RESEARCH.md D-16): in bundled mode (split=False) the skill name
    derives from the last URL segment only. Two repos 'orgA/skills' and 'orgB/skills' both
    produce name='skills'; the second upsert overwrites the first row (same PK).  A
    subsequent single-repo sync then orphan-deletes the survivor, losing orgA's skill.

    Fix criterion: after syncing both repos the DB must contain TWO distinct rows (one per
    source_repo_url).  After a single-repo sync of orgB only, orgA's row must still exist
    in DB and orgA's MA skill must NOT have been delete-called.
    """
    cli = await make_cli_principal(db_session, os_user="alice")
    await db_session.commit()
    fernet = _make_fernet()
    await _seed_pat(sessionmaker=db_session_factory, fernet=fernet, principal_id=cli.id)

    url_a = "https://github.com/orgA/skills"
    url_b = "https://github.com/orgB/skills"

    # Each repo gets a distinct tarball routed by request URL so the mock doesn't
    # return the wrong content.  The tarball path prefix matches how the fetcher
    # requests tarballs: /repos/{owner}/{repo}/tarball/{branch}.
    tarball_a = _make_tarball(
        {"skills-main/SKILL.md": b"---\nname: a-skill\ndescription: from orgA\n---\nbody"}
    )
    tarball_b = _make_tarball(
        {"skills-main/SKILL.md": b"---\nname: b-skill\ndescription: from orgB\n---\nbody"}
    )

    def tarball_router(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "orgA" in path:
            return httpx.Response(200, content=tarball_a)
        if "orgB" in path:
            return httpx.Response(200, content=tarball_b)
        return httpx.Response(404)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(tarball_router))

    sk_a = "sk_orga_skills"
    sk_b = "sk_orgb_skills"

    # skills.create sends multipart form data (not JSON); parse display_title from
    # the multipart body to assign distinct skill IDs to orgA vs orgB.
    _create_counter = [0]

    def on_create(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        _create_counter[0] += 1
        # The display_title field is in the multipart body as a plain text part.
        # Decode as latin-1 to safely inspect bytes, then find the display_title value.
        raw = req.content.decode("latin-1")
        # Multipart form field: 'name="display_title"\r\n\r\n<value>\r\n'
        if "orga" in raw.lower():
            sk_id = sk_a
            display_title = "agent/orga-skills"
        else:
            sk_id = sk_b
            display_title = "agent/orgb-skills"
        return httpx.Response(
            200,
            json=SkillListResponse(
                id=sk_id,
                type="custom",
                display_title=display_title,
                latest_version="1",
                created_at="2026-04-21T00:00:00Z",
                updated_at="2026-04-21T00:00:00Z",
                source="custom",
            ).model_dump(mode="json"),
        )

    delete_calls: list[httpx.Request] = []

    def on_delete(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        delete_calls.append(req)
        return httpx.Response(204)

    router = MARouter()
    router.add("POST", r"/v1/skills", on_create)
    router.add("DELETE", r"/v1/skills/.*", on_delete)
    router.add("GET", r"/v1/agents", lambda req, _m: list_response([]))
    anthropic_client = _build_anthropic(router)

    # The test fixture's db_session_factory shares a single asyncpg connection
    # (per daimon.testing.db). Concurrent _process_one calls in _upload_all would
    # race on that connection ("another operation is in progress"). Force serial
    # execution via concurrency=1; this does not affect the naming fix under test.
    monkeypatch.setattr(orch_mod, "_UPLOAD_CONCURRENCY", 1)

    # Phase 1: sync both repos in one call.
    repo_a = SkillRepo(url=url_a, branch="main", split=False)
    repo_b = SkillRepo(url=url_b, branch="main", split=False)

    report1 = await sync_agent_skills(
        principal_id=cli.id,
        tenant_id=cli.tenant_id,
        agent_name="agent",
        repos=[repo_a, repo_b],
        sessionmaker=db_session_factory,
        fernet=fernet,
        http_client=http_client,
        anthropic_client=anthropic_client,
    )

    assert not report1.skipped_repos, f"no repos should be skipped; got {report1.skipped_repos}"

    async with db_session_factory() as s, s.begin():
        all_rows = await list_user_skills_for_agent(
            s,
            tenant_id=cli.tenant_id,
            principal_id=cli.id,
            agent_name="agent",
        )

    assert len(all_rows) == 2, (
        "two repos with colliding trailing segment must not share a user_skills row; "
        f"got {len(all_rows)} rows: {[(r.name, r.source_repo_url) for r in all_rows]}"
    )
    source_urls = {r.source_repo_url for r in all_rows}
    assert url_a in source_urls, f"orgA's row must exist; got source_urls={source_urls}"
    assert url_b in source_urls, f"orgB's row must exist; got source_urls={source_urls}"

    # Find orgA's row name so we can check it survives the single-repo sync.
    row_a = next(r for r in all_rows if r.source_repo_url == url_a)

    # Phase 2: single-repo sync of orgB only — orgA's row must survive, orgA's MA skill
    # must NOT be delete-called.
    delete_calls.clear()

    report2 = await sync_agent_skills(
        principal_id=cli.id,
        tenant_id=cli.tenant_id,
        agent_name="agent",
        repos=[repo_b],
        sessionmaker=db_session_factory,
        fernet=fernet,
        http_client=http_client,
        anthropic_client=anthropic_client,
    )

    assert not report2.skipped_repos, (
        f"second sync: no repos should be skipped; got {report2.skipped_repos}"
    )

    # orgA's skill must NOT have been delete-called on MA.
    orga_deleted = any(req.url.path.endswith(f"/{sk_a}") for req in delete_calls)
    assert not orga_deleted, (
        f"orgA's MA skill ({sk_a}) must NOT be delete-called during a single-repo sync of orgB; "
        f"delete_calls={[r.url.path for r in delete_calls]}"
    )

    async with db_session_factory() as s, s.begin():
        surviving_row_a = await load_user_skill(
            s,
            tenant_id=cli.tenant_id,
            principal_id=cli.id,
            agent_name="agent",
            name=row_a.name,
        )

    assert surviving_row_a is not None, (
        f"orgA's user_skills row (name={row_a.name!r}) must survive a single-repo sync of orgB"
    )


# ---------------------------------------------------------------------------
# SC-4: two-tenant title distinctness + namespace-checked recovery (#138)
# ---------------------------------------------------------------------------


async def test_sync_creates_two_distinct_skills_when_two_tenants_sync_same_named_skill(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SC-4 proof: two guilds syncing the same agent/skill name get two distinct MA skills.

    Pre-fix: every guild's agent is `daimon`, so `daimon/x` titles collide across
    tenants. Post-fix: titles are tenant-prefixed `{t8}-daimon/x`, so guild-A and
    guild-B produce `{t8_A}-daimon/x` and `{t8_B}-daimon/x` respectively.

    This test drives the orchestrator path twice with the same `agent_name` and
    skill name but two different `tenant_id`s against one stateful fake MA, then
    asserts:
    - Two `skills.create` requests with two DIFFERENT display_titles (each
      carrying its tenant's prefix).
    - The second run performs NO `versions.create` on the first run's skill id.
    """
    # Two separate tenant principals
    cli_a = await make_cli_principal(db_session, os_user="guild_a")
    cli_b = await make_cli_principal(db_session, os_user="guild_b")
    await db_session.commit()

    fernet = _make_fernet()
    await _seed_pat(sessionmaker=db_session_factory, fernet=fernet, principal_id=cli_a.id)
    await _seed_pat(sessionmaker=db_session_factory, fernet=fernet, principal_id=cli_b.id)

    # Both tenants have the same agent name and same skill in their repo.
    tarball = _make_tarball({"r-main/SKILL.md": b"---\nname: r\ndescription: d\n---\nbody"})
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, content=tarball))
    )

    create_requests: list[httpx.Request] = []
    version_requests: list[httpx.Request] = []

    _create_id_counter = [0]

    def on_create(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        create_requests.append(req)
        _create_id_counter[0] += 1
        sk_id = f"sk_{_create_id_counter[0]:03d}"
        return httpx.Response(
            200,
            json=SkillListResponse(
                id=sk_id,
                type="custom",
                display_title="placeholder",
                latest_version="1",
                created_at="2026-04-21T00:00:00Z",
                updated_at="2026-04-21T00:00:00Z",
                source="custom",
            ).model_dump(mode="json"),
        )

    def on_version(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        version_requests.append(req)
        return httpx.Response(500, json={"error": "must not be called in SC-4 test"})

    router = MARouter()
    router.add("POST", r"/v1/skills", on_create)
    router.add("POST", r"/v1/skills/.*/versions", on_version)
    router.add("GET", r"/v1/agents", lambda req, _m: list_response([]))
    anthropic_client = _build_anthropic(router)

    # Force serial execution to avoid asyncpg "another operation in progress" on
    # the shared test connection.
    monkeypatch.setattr(orch_mod, "_UPLOAD_CONCURRENCY", 1)

    repos = [SkillRepo(url="https://github.com/o/r", branch="main", split=False)]

    # Tenant A syncs first.
    report_a = await sync_agent_skills(
        principal_id=cli_a.id,
        tenant_id=cli_a.tenant_id,
        agent_name="daimon",
        repos=repos,
        sessionmaker=db_session_factory,
        fernet=fernet,
        http_client=http_client,
        anthropic_client=anthropic_client,
    )
    assert report_a.synced == 1, f"tenant A: expected one created skill, got {report_a}"
    assert report_a.failed_uploads == [], f"tenant A: unexpected failures {report_a.failed_uploads}"

    # Tenant B syncs second with identical agent_name + skill name.
    report_b = await sync_agent_skills(
        principal_id=cli_b.id,
        tenant_id=cli_b.tenant_id,
        agent_name="daimon",
        repos=repos,
        sessionmaker=db_session_factory,
        fernet=fernet,
        http_client=http_client,
        anthropic_client=anthropic_client,
    )
    assert report_b.synced == 1, f"tenant B: expected one created skill, got {report_b}"
    assert report_b.failed_uploads == [], f"tenant B: unexpected failures {report_b.failed_uploads}"

    # Extract display_titles from the two create requests.
    assert len(create_requests) == 2, (
        f"SC-4: expected exactly 2 skills.create calls, got {len(create_requests)}"
    )

    def _extract_display_title(req: httpx.Request) -> str:
        raw = req.content.decode("latin-1")
        # Multipart form field 'name="display_title"\r\n\r\n<value>\r\n--'
        marker = 'name="display_title"\r\n\r\n'
        start = raw.find(marker)
        assert start != -1, "display_title field not found in multipart body"
        value_start = start + len(marker)
        value_end = raw.find("\r\n", value_start)
        return raw[value_start:value_end]

    title_a = _extract_display_title(create_requests[0])
    title_b = _extract_display_title(create_requests[1])

    assert title_a != title_b, (
        f"SC-4: two tenants syncing the same skill must produce DISTINCT display_titles; "
        f"got title_a={title_a!r} == title_b={title_b!r}"
    )

    t8_a = str(cli_a.tenant_id)[:8]
    t8_b = str(cli_b.tenant_id)[:8]

    assert title_a.startswith(t8_a + "-"), (
        f"SC-4: tenant A's title must start with its t8 prefix {t8_a!r}, got {title_a!r}"
    )
    assert title_b.startswith(t8_b + "-"), (
        f"SC-4: tenant B's title must start with its t8 prefix {t8_b!r}, got {title_b!r}"
    )

    # The second run must NOT push a version onto the first run's skill id.
    assert version_requests == [], (
        f"SC-4: second tenant's sync must not touch first tenant's skill id; "
        f"got version_requests={[r.url.path for r in version_requests]}"
    )


async def test_recovery_refuses_to_push_version_onto_foreign_tenant_skill(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-07 / #138: recovery refuses to push a version when recovered skill is foreign-prefixed.

    Scenario: skills.create returns a duplicate-title 400 and find_skill_by_display_title
    returns a skill whose display_title carries a DIFFERENT tenant's prefix. The orchestrator
    must refuse to push versions.create onto that foreign skill and record the refusal in
    report.failed_uploads. Zero versions.create calls against the foreign skill id.

    This tests defense-in-depth: post-D-01, cross-tenant title collisions cannot happen
    in production (distinct prefixes), but we guard against future bugs with a namespace check.
    We fake the defense-in-depth scenario directly by monkeypatching find_skill_by_display_title
    to return a foreign-prefixed skill so the namespace check fires.
    """
    cli = await make_cli_principal(db_session, os_user="alice")
    await db_session.commit()
    fernet = _make_fernet()
    await _seed_pat(sessionmaker=db_session_factory, fernet=fernet, principal_id=cli.id)

    tarball = _make_tarball({"r-main/SKILL.md": b"---\nname: r\ndescription: d\n---\nbody"})
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, content=tarball))
    )

    # Build a foreign-prefixed display_title that does NOT match the caller's tenant.
    foreign_tenant_id = uuid.uuid4()
    foreign_title = tenant_scoped_display_title(
        tenant_id=foreign_tenant_id, name="o-r", agent_name="agent"
    )
    # Confirm the foreign prefix differs from the caller's.
    assert not foreign_title.startswith(str(cli.tenant_id)[:8] + "-"), (
        "test setup: foreign title must not share caller tenant prefix"
    )

    foreign_skill = SkillListResponse(
        id="sk_foreign",
        type="custom",
        display_title=foreign_title,
        latest_version="1",
        created_at="2026-04-21T00:00:00Z",
        updated_at="2026-04-21T00:00:00Z",
        source="custom",
    )

    version_calls: list[httpx.Request] = []

    # Monkeypatch find_skill_by_display_title to return the foreign skill regardless
    # of the requested display_title — this directly fakes the defense-in-depth scenario.
    from daimon.core.skill_sync import orchestrator as _orch_mod

    async def _fake_find(
        _client: object,
        _display_title: str,
        *,
        on_truncation: str = "degrade",
    ) -> SkillListResponse:
        return foreign_skill

    monkeypatch.setattr(_orch_mod, "find_skill_by_display_title", _fake_find)

    def on_create(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        return _conflict_response(req, _m)

    def on_version(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        version_calls.append(req)
        return httpx.Response(500, json={"error": "must not be called"})

    router = MARouter()
    router.add("POST", r"/v1/skills", on_create)
    router.add("POST", r"/v1/skills/sk_foreign/versions", on_version)
    router.add("GET", r"/v1/agents", lambda req, _m: list_response([]))
    anthropic_client = _build_anthropic(router)

    report = await sync_agent_skills(
        principal_id=cli.id,
        tenant_id=cli.tenant_id,
        agent_name="agent",
        repos=[SkillRepo(url="https://github.com/o/r", branch="main", split=False)],
        sessionmaker=db_session_factory,
        fernet=fernet,
        http_client=http_client,
        anthropic_client=anthropic_client,
    )

    assert version_calls == [], (
        f"namespace-refused recovery must make ZERO versions.create calls on the foreign skill; "
        f"got {[r.url.path for r in version_calls]}"
    )
    assert len(report.failed_uploads) == 1, (
        f"namespace refusal must be recorded in failed_uploads; got {report.failed_uploads}"
    )
    failed_name, failed_msg = report.failed_uploads[0]
    assert failed_name == "o-r", f"failed skill must be 'o-r', got {failed_name!r}"
    assert "namespace" in failed_msg.lower() or "#138" in failed_msg, (
        f"failure message must mention namespace or #138; got {failed_msg!r}"
    )


# ---------------------------------------------------------------------------
# #141 + #144-2: attach — base-toolset guard + version-conflict retry
# ---------------------------------------------------------------------------


async def test_attach_adds_base_toolset_when_agent_lacks_it(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """#141: attach sends tools=merge_default_agent_toolset(...) when agent has no agent_toolset.

    An agent without agent_toolset_20260401 on MA would fail session creation once
    skills are attached ("skills require the read tool to be usable"). The attach
    step must include the base toolset in the same update so the agent stays usable.
    """
    cli = await make_cli_principal(db_session, os_user="alice")
    await db_session.commit()
    fernet = _make_fernet()
    await _seed_pat(sessionmaker=db_session_factory, fernet=fernet, principal_id=cli.id)

    # CR-01: the orchestrator keys the ledger on the agent's derived UUID when the agent
    # is found on MA. Seed the row under that key so list_user_skills_for_agent finds it.
    ledger_key = derive_agent_uuid(tenant_id=cli.tenant_id, ma_agent_id="ag_toolless")

    # Seed a user_skills row so the attach step fires.
    async with db_session_factory() as s, s.begin():
        await upsert_user_skill(
            s,
            tenant_id=cli.tenant_id,
            principal_id=ledger_key,
            agent_name="agent",
            name="my-skill",
            source_repo_url="https://github.com/o/r",
            source_repo_branch="main",
            source_path="",
            content_hash="hash_1",
            anthropic_id="sk_new",
            anthropic_latest_version="1",
        )

    # Toolless agent — no tools at all.
    agent_payload = BetaManagedAgentsAgent(
        id="ag_toolless",
        type="agent",
        name="agent",
        model={"id": "claude-opus-4-7"},
        metadata={
            "daimon_tenant": str(cli.tenant_id),
            "daimon_name": "agent",
        },
        description=None,
        created_at="2026-04-21T00:00:00Z",
        updated_at="2026-04-21T00:00:00Z",
        version=3,
        mcp_servers=[],
        skills=[],
        tools=[],  # no agent_toolset_20260401
        system=None,
    ).model_dump(mode="json")

    update_calls: list[dict[str, object]] = []

    def on_update(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        update_calls.append(json.loads(req.content))
        return httpx.Response(
            200,
            json=BetaManagedAgentsAgent(
                id="ag_toolless",
                type="agent",
                name="agent",
                model={"id": "claude-opus-4-7"},
                metadata={"daimon_tenant": str(cli.tenant_id), "daimon_name": "agent"},
                description=None,
                created_at="2026-04-21T00:00:00Z",
                updated_at="2026-04-21T00:00:00Z",
                version=4,
                mcp_servers=[],
                skills=[
                    BetaManagedAgentsCustomSkill(skill_id="sk_new", type="custom", version="1")
                ],
                tools=[],
                system=None,
            ).model_dump(mode="json"),
        )

    router = MARouter()
    router.add("GET", r"/v1/agents", lambda req, _m: list_response([agent_payload]))
    router.add(
        "GET", r"/v1/agents/ag_toolless", lambda req, _m: httpx.Response(200, json=agent_payload)
    )
    router.add("POST", r"/v1/agents/ag_toolless", on_update)
    anthropic_client = _build_anthropic(router)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(_unreachable_handler))

    await sync_agent_skills(
        principal_id=cli.id,
        tenant_id=cli.tenant_id,
        agent_name="agent",
        repos=[],  # no repos — skip straight to attach (user_skills row already seeded)
        sessionmaker=db_session_factory,
        fernet=fernet,
        http_client=http_client,
        anthropic_client=anthropic_client,
    )

    assert len(update_calls) == 1, (
        f"#141: agents.update must fire once for the attach; got {len(update_calls)}"
    )
    body = update_calls[0]
    tools = body.get("tools", [])
    tool_types = [t.get("type") for t in tools]  # type: ignore[union-attr]
    assert "agent_toolset_20260401" in tool_types, (
        f"#141: attach must include agent_toolset_20260401 when agent lacks it; "
        f"got tool types: {tool_types}"
    )


async def test_attach_sends_skills_only_when_agent_has_base_toolset(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """#141: when the agent already has agent_toolset_20260401, attach sends skills only.

    The update payload must not carry a tools key when the agent already has the base
    toolset — no unnecessary churn.
    """
    cli = await make_cli_principal(db_session, os_user="alice")
    await db_session.commit()
    fernet = _make_fernet()
    await _seed_pat(sessionmaker=db_session_factory, fernet=fernet, principal_id=cli.id)

    ledger_key = derive_agent_uuid(tenant_id=cli.tenant_id, ma_agent_id="ag_with_toolset")

    async with db_session_factory() as s, s.begin():
        await upsert_user_skill(
            s,
            tenant_id=cli.tenant_id,
            principal_id=ledger_key,
            agent_name="agent",
            name="my-skill",
            source_repo_url="https://github.com/o/r",
            source_repo_branch="main",
            source_path="",
            content_hash="hash_1",
            anthropic_id="sk_new",
            anthropic_latest_version="1",
        )

    from anthropic.types.beta.beta_managed_agents_agent_toolset20260401 import (
        BetaManagedAgentsAgentToolset20260401,
    )
    from anthropic.types.beta.beta_managed_agents_agent_toolset_default_config import (
        BetaManagedAgentsAgentToolsetDefaultConfig,
    )
    from anthropic.types.beta.beta_managed_agents_always_allow_policy import (
        BetaManagedAgentsAlwaysAllowPolicy,
    )

    _base_toolset = BetaManagedAgentsAgentToolset20260401(
        type="agent_toolset_20260401",
        configs=[],
        default_config=BetaManagedAgentsAgentToolsetDefaultConfig(
            enabled=True,
            permission_policy=BetaManagedAgentsAlwaysAllowPolicy(type="always_allow"),
        ),
    )

    # Agent already has the base toolset.
    agent_payload = BetaManagedAgentsAgent(
        id="ag_with_toolset",
        type="agent",
        name="agent",
        model={"id": "claude-opus-4-7"},
        metadata={
            "daimon_tenant": str(cli.tenant_id),
            "daimon_name": "agent",
        },
        description=None,
        created_at="2026-04-21T00:00:00Z",
        updated_at="2026-04-21T00:00:00Z",
        version=5,
        mcp_servers=[],
        skills=[],
        tools=[_base_toolset],
        system=None,
    ).model_dump(mode="json")

    update_calls: list[dict[str, object]] = []

    def on_update(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        update_calls.append(json.loads(req.content))
        return httpx.Response(
            200,
            json=BetaManagedAgentsAgent(
                id="ag_with_toolset",
                type="agent",
                name="agent",
                model={"id": "claude-opus-4-7"},
                metadata={"daimon_tenant": str(cli.tenant_id), "daimon_name": "agent"},
                description=None,
                created_at="2026-04-21T00:00:00Z",
                updated_at="2026-04-21T00:00:00Z",
                version=6,
                mcp_servers=[],
                skills=[
                    BetaManagedAgentsCustomSkill(skill_id="sk_new", type="custom", version="1")
                ],
                tools=[],
                system=None,
            ).model_dump(mode="json"),
        )

    router = MARouter()
    router.add("GET", r"/v1/agents", lambda req, _m: list_response([agent_payload]))
    router.add(
        "GET",
        r"/v1/agents/ag_with_toolset",
        lambda req, _m: httpx.Response(200, json=agent_payload),
    )
    router.add("POST", r"/v1/agents/ag_with_toolset", on_update)
    anthropic_client = _build_anthropic(router)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(_unreachable_handler))

    await sync_agent_skills(
        principal_id=cli.id,
        tenant_id=cli.tenant_id,
        agent_name="agent",
        repos=[],
        sessionmaker=db_session_factory,
        fernet=fernet,
        http_client=http_client,
        anthropic_client=anthropic_client,
    )

    assert len(update_calls) == 1, (
        f"#141: agents.update must fire once for the attach; got {len(update_calls)}"
    )
    body = update_calls[0]
    assert "tools" not in body, (
        f"#141: attach must NOT include tools when agent already has agent_toolset_20260401; "
        f"got body keys: {list(body.keys())}"
    )
    assert "skills" in body, "attach must include skills in the update payload"


async def test_attach_retries_once_on_version_conflict(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """#144-2: attach retries once on 409; second attempt unions against the fresh agent's skills.

    The fresh retrieve returns a different skill set than the first read — the final
    update payload must union against the FRESH set, not the stale initial read.
    """
    cli = await make_cli_principal(db_session, os_user="alice")
    await db_session.commit()
    fernet = _make_fernet()
    await _seed_pat(sessionmaker=db_session_factory, fernet=fernet, principal_id=cli.id)

    ledger_key = derive_agent_uuid(tenant_id=cli.tenant_id, ma_agent_id="ag_conflict")

    # Row to attach.
    async with db_session_factory() as s, s.begin():
        await upsert_user_skill(
            s,
            tenant_id=cli.tenant_id,
            principal_id=ledger_key,
            agent_name="agent",
            name="my-skill",
            source_repo_url="https://github.com/o/r",
            source_repo_branch="main",
            source_path="",
            content_hash="hash_1",
            anthropic_id="sk_target",
            anthropic_latest_version="1",
        )

    # Initial list response: agent has no skills yet.
    initial_agent = BetaManagedAgentsAgent(
        id="ag_conflict",
        type="agent",
        name="agent",
        model={"id": "claude-opus-4-7"},
        metadata={"daimon_tenant": str(cli.tenant_id), "daimon_name": "agent"},
        description=None,
        created_at="2026-04-21T00:00:00Z",
        updated_at="2026-04-21T00:00:00Z",
        version=10,
        mcp_servers=[],
        skills=[],
        tools=[],
        system=None,
    ).model_dump(mode="json")

    # Fresh retrieve (after conflict): agent now has sk_concurrent added concurrently.
    fresh_agent = BetaManagedAgentsAgent(
        id="ag_conflict",
        type="agent",
        name="agent",
        model={"id": "claude-opus-4-7"},
        metadata={"daimon_tenant": str(cli.tenant_id), "daimon_name": "agent"},
        description=None,
        created_at="2026-04-21T00:00:00Z",
        updated_at="2026-04-21T00:00:00Z",
        version=11,
        mcp_servers=[],
        skills=[BetaManagedAgentsCustomSkill(skill_id="sk_concurrent", type="custom", version="1")],
        tools=[],
        system=None,
    ).model_dump(mode="json")

    retrieve_count = 0
    update_calls: list[dict[str, object]] = []

    def on_retrieve(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        nonlocal retrieve_count
        retrieve_count += 1
        if retrieve_count == 1:
            return httpx.Response(200, json=initial_agent)
        return httpx.Response(200, json=fresh_agent)

    def on_update(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        body = json.loads(req.content)
        update_calls.append(body)
        if len(update_calls) == 1:
            # First attempt: conflict
            return httpx.Response(
                409,
                json={
                    "type": "error",
                    "error": {
                        "type": "invalid_request_error",
                        "message": "Concurrent modification detected. Please fetch the latest version and retry.",
                    },
                },
            )
        # Second attempt: success
        return httpx.Response(
            200,
            json=BetaManagedAgentsAgent(
                id="ag_conflict",
                type="agent",
                name="agent",
                model={"id": "claude-opus-4-7"},
                metadata={"daimon_tenant": str(cli.tenant_id), "daimon_name": "agent"},
                description=None,
                created_at="2026-04-21T00:00:00Z",
                updated_at="2026-04-21T00:00:00Z",
                version=12,
                mcp_servers=[],
                skills=[
                    BetaManagedAgentsCustomSkill(
                        skill_id="sk_concurrent", type="custom", version="1"
                    ),
                    BetaManagedAgentsCustomSkill(skill_id="sk_target", type="custom", version="1"),
                ],
                tools=[],
                system=None,
            ).model_dump(mode="json"),
        )

    # max_retries=0: disable SDK auto-retry so helper retry logic fires in isolation.
    router = MARouter()
    router.add("GET", r"/v1/agents", lambda req, _m: list_response([initial_agent]))
    router.add("GET", r"/v1/agents/ag_conflict", on_retrieve)
    router.add("POST", r"/v1/agents/ag_conflict", on_update)

    anthropic_client = AsyncAnthropic(
        api_key="test",
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(router.dispatch),
            base_url="https://api.anthropic.com",
        ),
        max_retries=0,
    )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(_unreachable_handler))

    await sync_agent_skills(
        principal_id=cli.id,
        tenant_id=cli.tenant_id,
        agent_name="agent",
        repos=[],
        sessionmaker=db_session_factory,
        fernet=fernet,
        http_client=http_client,
        anthropic_client=anthropic_client,
    )

    assert len(update_calls) == 2, (
        f"#144-2: two update attempts expected (conflict + retry); got {len(update_calls)}"
    )
    assert retrieve_count == 2, (
        f"#144-2: two retrieve calls expected (initial + after conflict); got {retrieve_count}"
    )

    # Second attempt's skills must union against the FRESH agent (sk_concurrent + sk_target).
    second_skills = {s["skill_id"] for s in update_calls[1].get("skills", [])}  # type: ignore[index]
    assert "sk_concurrent" in second_skills, (
        "#144-2: second attempt must include sk_concurrent from the fresh agent"
    )
    assert "sk_target" in second_skills, "#144-2: second attempt must include the row's sk_target"


async def test_attach_over_cap_records_failure_instead_of_raising(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """MA's per-agent skill-cap 400 at attach is captured in attach_failures, not raised.

    Uploads already landed; only the agents.update binding is refused. The sync
    must return a SyncReport with a clear attach failure so the caller can tell
    the user to remove a repo — instead of dying with a raw BadRequestError.
    """
    cli = await make_cli_principal(db_session, os_user="alice")
    await db_session.commit()
    fernet = _make_fernet()

    ledger_key = derive_agent_uuid(tenant_id=cli.tenant_id, ma_agent_id="ag_cap")
    async with db_session_factory() as s, s.begin():
        await upsert_user_skill(
            s,
            tenant_id=cli.tenant_id,
            principal_id=ledger_key,
            agent_name="agent",
            name="my-skill",
            source_repo_url="https://github.com/o/r",
            source_repo_branch="main",
            source_path="",
            content_hash="hash_1",
            anthropic_id="sk_target",
            anthropic_latest_version="1",
        )

    agent_payload = BetaManagedAgentsAgent(
        id="ag_cap",
        type="agent",
        name="agent",
        model={"id": "claude-opus-4-7"},
        metadata={"daimon_tenant": str(cli.tenant_id), "daimon_name": "agent"},
        description=None,
        created_at="2026-04-21T00:00:00Z",
        updated_at="2026-04-21T00:00:00Z",
        version=10,
        mcp_servers=[],
        skills=[],
        tools=[],
        system=None,
    ).model_dump(mode="json")

    update_calls: list[dict[str, object]] = []

    def on_update(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        update_calls.append(json.loads(req.content))
        return httpx.Response(
            400,
            json={
                "type": "error",
                "error": {
                    "type": "invalid_request_error",
                    "message": (
                        "Agent has invalid configuration: skills: 21 exceeds "
                        "maximum of 20 for this organization"
                    ),
                },
            },
        )

    router = MARouter()
    router.add("GET", r"/v1/agents", lambda req, _m: list_response([agent_payload]))
    router.add("GET", r"/v1/agents/ag_cap", lambda req, _m: httpx.Response(200, json=agent_payload))
    router.add("POST", r"/v1/agents/ag_cap", on_update)
    anthropic_client = AsyncAnthropic(
        api_key="test",
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(router.dispatch),
            base_url="https://api.anthropic.com",
        ),
        max_retries=0,
    )
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(_unreachable_handler))

    report = await sync_agent_skills(
        principal_id=cli.id,
        tenant_id=cli.tenant_id,
        agent_name="agent",
        repos=[],
        sessionmaker=db_session_factory,
        fernet=fernet,
        http_client=http_client,
        anthropic_client=anthropic_client,
    )

    assert len(update_calls) == 1, "attach must be attempted exactly once (400 is not retried)"
    assert len(report.attach_failures) == 1, (
        f"the cap rejection must be recorded as one attach failure, got {report.attach_failures}"
    )
    failed_agent, reason = report.attach_failures[0]
    assert failed_agent == "agent", "attach_failures must carry the agent name"
    assert "exceeds maximum of 20" in reason, "the MA cap message must be preserved for the user"
