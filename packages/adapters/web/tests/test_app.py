"""Health and single-page app serving."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import WebClientFactory, web_settings


async def test_healthz_reports_ok_when_the_database_answers(
    web_client: WebClientFactory, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with web_client(sessionmaker=db_session_factory) as client:
        res = await client.get("/healthz")
    assert (res.status_code, res.json()) == (200, {"status": "ok"})


async def test_healthz_reports_unavailable_when_the_database_does_not(
    web_client: WebClientFactory,
) -> None:
    async with web_client() as client:  # an unbound sessionmaker cannot connect
        res = await client.get("/healthz")
    assert (res.status_code, res.json()) == (503, {"status": "unavailable"})


async def test_pages_fall_back_to_index_and_files_are_served(
    web_client: WebClientFactory, built_app: Path
) -> None:
    async with web_client(web_settings(static_dir=built_app)) as client:
        root = await client.get("/")
        deep = await client.get("/chat/some-thread")
        icon = await client.get("/favicon.svg")
        asset = await client.get("/assets/main-abc123.js")
    assert root.text == deep.text == "<html>app</html>", "any other path is a client-side page"
    assert root.headers["cache-control"] == "no-cache"
    assert icon.text == "<svg/>" and icon.headers["cache-control"] == "no-cache"
    assert "immutable" in asset.headers["cache-control"], "hashed assets never change"


async def test_unknown_api_and_auth_paths_are_404_not_the_app(
    web_client: WebClientFactory, built_app: Path
) -> None:
    async with web_client(web_settings(static_dir=built_app)) as client:
        for path in ("/api/nope", "/auth/nope", "/api"):
            res = await client.get(path)
            assert res.status_code == 404 and "html" not in res.text, path


async def test_paths_cannot_escape_the_static_folder(
    web_client: WebClientFactory, built_app: Path
) -> None:
    (built_app.parent / "secret.txt").write_text("secret")
    async with web_client(web_settings(static_dir=built_app)) as client:
        res = await client.get("/..%2Fsecret.txt")
        raw = await client.get("/assets/../../secret.txt")
    assert "secret" not in res.text + raw.text


async def test_a_missing_build_is_a_404_not_a_crash(
    web_client: WebClientFactory, tmp_path: Path
) -> None:
    async with web_client(web_settings(static_dir=tmp_path / "absent")) as client:
        res = await client.get("/")
    assert res.status_code == 404
