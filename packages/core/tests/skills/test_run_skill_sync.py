"""Transport-level tests for run_skill_sync.

Uses httpx.MockTransport for both the httpx.AsyncClient (tarball) and the
AsyncAnthropic (MA API), so the real SDK code path runs in full.
"""

from __future__ import annotations

import io
import shutil
import tarfile
import tempfile
import time
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from anthropic.types.beta import SkillListResponse
from daimon.core.defaults.report import Action
from daimon.core.errors import DaimonError
from daimon.core.skills.fetch import FetchResult
from daimon.core.skills.pipeline import run_skill_sync
from daimon.testing.ma import MARouter, list_response
from daimon.testing.ma import build_fake_anthropic as build_fake_anthropic_http

_TENANT = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")


def _make_tarball(files: dict[str, str]) -> bytes:
    """Create an in-memory gzipped tarball from a dict of path -> content."""

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mtime = int(time.time())
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


_SKILL_MD = "---\nname: test-skill\ndescription: A test skill.\n---\nTest skill content.\n"


async def test_successful_sync() -> None:
    """Happy path: tarball with a SKILL.md -> MA create -> ResourceOutcome(CREATED)."""
    tarball = _make_tarball({"my-skill/SKILL.md": _SKILL_MD})

    def tarball_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=tarball)

    router = MARouter()
    router.add("GET", r"/v1/skills", lambda req, _m: list_response([]))
    router.add(
        "POST",
        r"/v1/skills",
        lambda req, _m: httpx.Response(
            200,
            json=SkillListResponse(
                id="sk_123",
                type="custom",
                display_title="test-skill",
                latest_version="1",
                created_at="2026-04-21T00:00:00Z",
                updated_at="2026-04-21T00:00:00Z",
                source="custom",
            ).model_dump(mode="json"),
        ),
    )

    client = build_fake_anthropic_http(router.dispatch)
    http = httpx.AsyncClient(transport=httpx.MockTransport(tarball_handler))

    async with http:
        outcomes = await run_skill_sync(
            client,
            http,
            url="https://github.com/org/repo",
            branch="main",
            tenant_id=_TENANT,
        )

    assert len(outcomes) == 1, "should return one outcome"
    assert outcomes[0].action is Action.CREATED, "action should be CREATED"
    assert outcomes[0].anthropic_id == "sk_123", "should capture anthropic_id"


async def test_path_escape_raises_daimon_error() -> None:
    """path='../etc' escapes the repo root -> DaimonError, temp dir cleaned up."""
    cleanup_dir = Path(tempfile.mkdtemp())
    try:
        router = MARouter()
        client = build_fake_anthropic_http(router.dispatch)
        http = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200)))

        mock_fetch = AsyncMock(return_value=FetchResult(path=cleanup_dir, cleanup_dir=cleanup_dir))
        with patch("daimon.core.skills.pipeline.fetch_repo", mock_fetch):
            async with http:
                with pytest.raises(DaimonError, match="escapes the repository root"):
                    await run_skill_sync(
                        client,
                        http,
                        url="https://github.com/org/repo",
                        path="../etc",
                        tenant_id=_TENANT,
                    )

        assert not cleanup_dir.exists(), "finally block must clean up temp dir"
    finally:
        if cleanup_dir.exists():
            shutil.rmtree(cleanup_dir)


async def test_missing_path_raises_daimon_error() -> None:
    """path='nonexistent' not in fetched repo -> DaimonError, temp dir cleaned up."""
    cleanup_dir = Path(tempfile.mkdtemp())
    try:
        router = MARouter()
        client = build_fake_anthropic_http(router.dispatch)
        http = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200)))

        mock_fetch = AsyncMock(return_value=FetchResult(path=cleanup_dir, cleanup_dir=cleanup_dir))
        with patch("daimon.core.skills.pipeline.fetch_repo", mock_fetch):
            async with http:
                with pytest.raises(DaimonError, match="not found in fetched repository"):
                    await run_skill_sync(
                        client,
                        http,
                        url="https://github.com/org/repo",
                        path="nonexistent",
                        tenant_id=_TENANT,
                    )

        assert not cleanup_dir.exists(), "finally block must clean up temp dir"
    finally:
        if cleanup_dir.exists():
            shutil.rmtree(cleanup_dir)


async def test_cleanup_runs_on_sync_error() -> None:
    """MA returns 500 on create -> run_skill_sync raises, temp dir still cleaned up."""
    cleanup_dir = Path(tempfile.mkdtemp())
    try:
        # Put a valid SKILL.md so discover finds it; sync will fail with 500
        (cleanup_dir / "SKILL.md").write_text(_SKILL_MD)

        router = MARouter()
        router.add("GET", r"/v1/skills", lambda req, _m: list_response([]))
        router.add(
            "POST", r"/v1/skills", lambda req, _m: httpx.Response(500, json={"error": "fail"})
        )

        client = build_fake_anthropic_http(router.dispatch)
        http = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200)))

        mock_fetch = AsyncMock(return_value=FetchResult(path=cleanup_dir, cleanup_dir=cleanup_dir))
        with patch("daimon.core.skills.pipeline.fetch_repo", mock_fetch):
            async with http:
                # 500 from MA produces a FAILED outcome, not a raised exception.
                # run_skill_sync returns normally; outcome.action == FAILED.
                outcomes = await run_skill_sync(
                    client,
                    http,
                    url="https://github.com/org/repo",
                    branch="main",
                    tenant_id=_TENANT,
                )

        assert len(outcomes) == 1, "should return one (failed) outcome"
        assert outcomes[0].action is Action.FAILED, "500 from MA should produce FAILED outcome"
        assert not cleanup_dir.exists(), "finally block must clean up temp dir even when sync fails"
    finally:
        if cleanup_dir.exists():
            shutil.rmtree(cleanup_dir)
