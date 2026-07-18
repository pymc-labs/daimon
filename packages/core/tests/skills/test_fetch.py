"""Transport-level tests for daimon.core.skills.fetch.

All tests use httpx.MockTransport — no real network, no module-level mocks.
"""

from __future__ import annotations

import io
import shutil
import tarfile
from collections.abc import AsyncIterator

import httpx
import pytest
from daimon.core.errors import DaimonError
from daimon.core.skills.fetch import fetch_repo


def _build_tarball(files: dict[str, str]) -> bytes:
    """Create an in-memory gzipped tarball from a dict of path -> content."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


async def test_fetch_repo_extracts_and_strips() -> None:
    """GitHub-style wrapper dir is stripped; skill files are accessible at the returned root."""
    tarball = _build_tarball(
        {
            "owner-repo-abc123/skills/brainstorming/SKILL.md": (
                "---\nname: brainstorming\ndescription: d\n---\nbody"
            ),
            "owner-repo-abc123/skills/brainstorming/rules.md": "rules",
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=tarball)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await fetch_repo(client, "https://github.com/owner/repo", branch="main")
    try:
        assert (result.path / "skills" / "brainstorming" / "SKILL.md").exists(), (
            "smart strip should remove the GitHub wrapper dir so repo root is returned"
        )
        assert result.cleanup_dir != result.path, "stripped result should differ from cleanup_dir"
    finally:
        shutil.rmtree(result.cleanup_dir)


async def test_fetch_repo_no_strip_when_multiple_dirs() -> None:
    """When tarball has multiple root dirs, smart strip returns the extract root."""
    tarball = _build_tarball(
        {
            "dir-a/SKILL.md": "---\nname: a\ndescription: d\n---\nbody",
            "dir-b/SKILL.md": "---\nname: b\ndescription: d\n---\nbody",
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=tarball)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await fetch_repo(client, "https://github.com/owner/repo", branch="main")
    try:
        assert (result.path / "dir-a").exists(), "dir-a must exist at extract root"
        assert (result.path / "dir-b").exists(), "dir-b must exist at extract root"
        assert result.cleanup_dir == result.path, "no-strip result should equal cleanup_dir"
    finally:
        shutil.rmtree(result.cleanup_dir)


async def test_fetch_repo_rejects_non_github_url() -> None:
    """Non-GitHub URL raises DaimonError before any HTTP request is made."""
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, content=b""))
    )
    with pytest.raises(DaimonError, match="not a GitHub URL"):
        await fetch_repo(client, "https://gitlab.com/owner/repo")


async def test_fetch_repo_propagates_http_error() -> None:
    """A 404 response from the tarball URL raises DaimonError wrapping the HTTP error."""
    tarball = _build_tarball({})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=tarball)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(DaimonError, match="HTTP 404"):
        await fetch_repo(client, "https://github.com/owner/repo")


async def test_fetch_repo_handles_git_suffix() -> None:
    """URL with .git suffix is parsed correctly and tarball URL is valid."""
    tarball = _build_tarball(
        {"owner-repo-abc123/SKILL.md": "---\nname: brainstorming\ndescription: d\n---\nbody"}
    )
    received_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        received_urls.append(str(request.url))
        return httpx.Response(200, content=tarball)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await fetch_repo(client, "https://github.com/owner/repo.git", branch="main")
    try:
        assert len(received_urls) == 1, "exactly one request should have been made"
        assert "owner/repo/archive/refs/heads/main.tar.gz" in received_urls[0], (
            "tarball URL must use the repo name without .git suffix"
        )
    finally:
        shutil.rmtree(result.cleanup_dir)


async def test_fetch_repo_constructs_correct_tarball_url() -> None:
    """The tarball URL is constructed from the GitHub archive endpoint."""
    tarball = _build_tarball({"my-skill/SKILL.md": "---\nname: s\ndescription: d\n---\nbody"})
    received_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        received_urls.append(str(request.url))
        return httpx.Response(200, content=tarball)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await fetch_repo(client, "https://github.com/myorg/myrepo", branch="dev")
    try:
        assert received_urls[0] == (
            "https://github.com/myorg/myrepo/archive/refs/heads/dev.tar.gz"
        ), "tarball URL must follow GitHub archive refs/heads pattern"
    finally:
        shutil.rmtree(result.cleanup_dir)


async def test_fetch_repo_with_token_uses_api_tarball_with_auth_header() -> None:
    """Token-authenticated fetch must hit the api.github.com tarball endpoint
    with `Authorization: token …` — the anon github.com/archive URL 404s on
    private repos."""
    tarball = _build_tarball(
        {"owner-repo-abc/skills/s/SKILL.md": "---\nname: s\ndescription: d\n---\nb"}
    )
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization", "")
        return httpx.Response(200, content=tarball)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await fetch_repo(
        client, "https://github.com/owner/repo", branch="main", token="github_pat_x"
    )
    try:
        assert seen["url"] == "https://api.github.com/repos/owner/repo/tarball/main", (
            f"token fetch must use the API tarball endpoint, got {seen['url']}"
        )
        assert seen["auth"] == "token github_pat_x", (
            "token fetch must send the Authorization header"
        )
        assert (result.path / "skills" / "s" / "SKILL.md").exists(), (
            "smart strip should still apply on the authenticated path"
        )
    finally:
        shutil.rmtree(result.cleanup_dir)


# --- streaming size guard + zip-bomb bound ---


async def test_fetch_repo_rejects_oversize_body_streamed() -> None:
    """A response body exceeding max_tarball_bytes raises DaimonError naming the cap."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 1000)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(DaimonError, match="max_tarball_bytes"):
        await fetch_repo(
            client,
            "https://github.com/owner/repo",
            branch="main",
            max_tarball_bytes=100,
        )


async def test_fetch_repo_rejects_content_length_over_cap_without_reading_body() -> None:
    """A Content-Length header exceeding the cap rejects before the body is consumed."""
    consumed: list[bool] = []

    async def body_gen() -> AsyncIterator[bytes]:
        consumed.append(True)
        yield b"x" * 1000

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-length": "1000000"}, content=body_gen())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(DaimonError, match="max_tarball_bytes"):
        await fetch_repo(
            client,
            "https://github.com/owner/repo",
            branch="main",
            max_tarball_bytes=100,
        )
    assert consumed == [], "body must never be read when Content-Length alone exceeds the cap"


async def test_fetch_repo_rejects_zip_bomb_before_extractall() -> None:
    """A tarball whose member sizes total more than the decompressed cap is rejected.

    KiB-scale fixture: highly-compressible repeated bytes keep the
    gzip-compressed tarball tiny while the member's declared/actual size
    (2 KiB) exceeds a KiB-scale injected cap — no real multi-MB bomb needed.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        content = b"\x00" * 2048
        info = tarfile.TarInfo(name="bomb/SKILL.md")
        info.size = len(content)
        tf.addfile(info, io.BytesIO(content))
    tarball = buf.getvalue()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=tarball)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(DaimonError, match="max_tarball_decompressed_bytes"):
        await fetch_repo(
            client,
            "https://github.com/owner/repo",
            branch="main",
            max_tarball_decompressed_bytes=1024,
        )


async def test_fetch_repo_under_cap_extracts_unchanged() -> None:
    """A normal under-cap repo tarball extracts unchanged with both caps set."""
    tarball = _build_tarball(
        {"owner-repo-abc123/SKILL.md": "---\nname: s\ndescription: d\n---\nbody"}
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=tarball)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await fetch_repo(
        client,
        "https://github.com/owner/repo",
        branch="main",
        max_tarball_bytes=1024 * 1024,
        max_tarball_decompressed_bytes=1024 * 1024,
    )
    try:
        assert (result.path / "SKILL.md").exists(), "under-cap tarball must extract unchanged"
    finally:
        shutil.rmtree(result.cleanup_dir)


async def test_fetch_repo_both_caps_zero_disables_guards() -> None:
    """Setting both caps to 0 disables both the raw-size and zip-bomb guards."""
    tarball = _build_tarball(
        {"owner-repo-abc123/SKILL.md": "---\nname: s\ndescription: d\n---\nbody"}
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=tarball)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await fetch_repo(
        client,
        "https://github.com/owner/repo",
        branch="main",
        max_tarball_bytes=0,
        max_tarball_decompressed_bytes=0,
    )
    try:
        assert (result.path / "SKILL.md").exists(), (
            "0-disabled caps must still allow a normal fetch to succeed"
        )
    finally:
        shutil.rmtree(result.cleanup_dir)
