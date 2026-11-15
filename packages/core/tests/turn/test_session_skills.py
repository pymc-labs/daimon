"""Reading a source's custom skills for a preview: sizes survive a timeout, and
latest versions are remembered briefly, so a cold agent with many skills fits."""

from __future__ import annotations

import asyncio
import io
import re
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any

import anthropic
import httpx
import pytest
from daimon.core.channel_backend import transient_failure
from daimon.core.errors import TurnError
from daimon.core.mux_backend import resource_scope
from daimon.core.mux_compat import publish_skill_version
from daimon.core.skills.latest_versions import LATEST_VERSION_TTL_S, LatestVersionCache
from daimon.core.skills.rate_limit import SkillsRateLimitedTransport
from daimon.core.stores.skill_copies import get_skill_sizes
from daimon.core.turn import session_skills
from daimon.core.turn.admission import PREVIEW_CAPTURE_BUDGET_S
from daimon.testing.factories import make_tenant
from mux.contracts.ids import SkillRef
from mux.testing.drivers.anthropic.ma import MARouter, build_fake_anthropic
from mux.testing.isolated_sdk import build_async_anthropic
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_PACKED = io.BytesIO()
with zipfile.ZipFile(_PACKED, "w") as _archive:
    _archive.writestr("SKILL.md", "---\nname: helper\ndescription: Fixture.\n---\nUse it.\n")
ARCHIVE = _PACKED.getvalue()
#: The default agent's custom skills on the deployment where the preview timed out.
DEFAULT_AGENT_SKILLS = 23
#: Production paces 80 Skills requests a minute; this test runs that 20 times faster.
SPEEDUP = 20
PRODUCTION_RPM = 80


def _skill(skill_id: str, version: str) -> dict[str, object]:
    return {
        "id": skill_id,
        "created_at": "2026-01-02T00:00:00Z",
        "display_title": skill_id,
        "latest_version": version,
        "source": "custom",
        "type": "skill",
        "updated_at": "2026-01-02T00:00:00Z",
    }


def _serve_skills(router: MARouter, versions: dict[str, str], hits: list[str]) -> None:
    """Each skill's retrieve, version list and download, counting every request."""

    def retrieve(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        hits.append(request.url.path)
        return httpx.Response(200, json=_skill(match[1], versions[match[1]]))

    def listing(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        hits.append(request.url.path)
        row = {"id": f"v-{match[1]}", "version": versions[match[1]]}
        return httpx.Response(200, json={"data": [row], "has_more": False})

    def content(request: httpx.Request, _match: re.Match[str]) -> httpx.Response:
        hits.append(request.url.path)
        return httpx.Response(200, content=ARCHIVE)

    router.add("GET", r"/v1/skills/([^/]+)", retrieve)
    router.add("GET", r"/v1/skills/([^/]+)/versions", listing)
    router.add("GET", r"/v1/skills/([^/]+)/versions/[^/]+/content", content)


async def test_a_timed_out_measure_keeps_each_size_it_finished(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The caller's timeout stops its wait, not the downloads: finished sizes are
    stored at once, and a retry downloads only the one still unmeasured."""
    tenant = await make_tenant(db_session)
    await db_session.commit()
    slow = asyncio.Event()
    downloads: list[str] = []

    async def download(*_a: object, skill_id: str, **_k: object) -> bytes:
        downloads.append(skill_id)
        if skill_id == "s2":
            await slow.wait()
        return ARCHIVE

    monkeypatch.setattr(session_skills, "download_skill_version", download)
    pins = (("s0", "1"), ("s1", "1"), ("s2", "1"))

    async def measure() -> dict[tuple[str, str], object]:
        return dict(
            await session_skills.measure_skills(
                db_session_factory,
                build_fake_anthropic(MARouter().dispatch),
                scope=resource_scope(tenant_id=str(tenant.id)),
                tenant_id=tenant.id,
                pins=pins,
            )
        )

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(1):
            await measure()
    async with db_session_factory() as db:
        stored = await get_skill_sizes(db, tenant_id=tenant.id, pins=pins)
    assert set(stored) == {("s0", "1"), ("s1", "1")}, f"finished sizes are kept: {stored}"
    slow.set()
    assert set(await measure()) == set(pins), "the retry has every size"
    assert sorted(downloads) == ["s0", "s1", "s2"], f"each version downloads once: {downloads}"


async def test_two_measures_of_one_version_share_its_download(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second caller joins the measurement in flight instead of downloading again."""
    tenant = await make_tenant(db_session)
    await db_session.commit()
    gate = asyncio.Event()
    downloads: list[str] = []

    async def download(*_a: object, skill_id: str, **_k: object) -> bytes:
        downloads.append(skill_id)
        await gate.wait()
        return ARCHIVE

    monkeypatch.setattr(session_skills, "download_skill_version", download)
    anthropic = build_fake_anthropic(MARouter().dispatch)
    calls = [
        asyncio.create_task(
            session_skills.measure_skills(
                db_session_factory,
                anthropic,
                scope=resource_scope(tenant_id=str(tenant.id)),
                tenant_id=tenant.id,
                pins=(("s0", "1"),),
            )
        )
        for _ in range(2)
    ]
    await asyncio.sleep(0.05)
    gate.set()
    first, second = await asyncio.gather(*calls)
    assert first == second and ("s0", "1") in first, "both callers get the size"
    assert downloads == ["s0"], f"one download for both: {downloads}"


def test_a_latest_version_is_remembered_until_its_ttl() -> None:
    now = [0.0]
    cache = LatestVersionCache(clock=lambda: now[0])
    cache.put("t1", "s0", "3", generation=0)
    assert cache.get("t1", "s0") == "3", "a hit inside the TTL"
    assert cache.get("t2", "s0") is None, "another tenant asks for itself"
    now[0] = LATEST_VERSION_TTL_S
    assert cache.get("t1", "s0") is None, "expired at the TTL"
    cache.put("t1", "s0", "3", generation=0)
    cache.put("t2", "s0", "3", generation=0)
    cache.forget("s0")
    assert (cache.get("t1", "s0"), cache.get("t2", "s0")) == (None, None), "forgotten everywhere"


def test_a_lookup_that_overlaps_a_publish_never_remembers_the_old_version() -> None:
    """A read started before a publish here may return the old latest: never keep it."""
    cache = LatestVersionCache()
    started = cache.generation()
    cache.forget("s0")  # published while the read was in flight
    cache.put("t1", "s0", "2", generation=started)
    assert cache.get("t1", "s0") is None, "the stale read is dropped, so the next one asks"
    cache.put("t1", "s0", "3", generation=cache.generation())
    assert cache.get("t1", "s0") == "3", "a read started after the publish is kept"


async def test_a_version_daimon_publishes_is_seen_at_once_and_one_published_elsewhere_after_the_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deliberate difference: a skill version published by another process or the
    console is seen within `LATEST_VERSION_TTL_S`, so that an agent with many skills
    at `latest` doesn't pay one paced request per skill on every read. A version
    this process publishes drops the remembered one at once."""
    from daimon.core import mux_compat

    now = [0.0]
    cache = LatestVersionCache(clock=lambda: now[0])
    monkeypatch.setattr(session_skills, "LATEST_VERSIONS", cache)
    monkeypatch.setattr(mux_compat, "LATEST_VERSIONS", cache)
    versions, hits = {"s0": "1"}, list[str]()
    router = MARouter()
    _serve_skills(router, versions, hits)
    router.add(
        "POST",
        r"/v1/skills/s0/versions",
        lambda _r, _m: httpx.Response(
            200,
            json={
                "id": "v-s0-3",
                "skill_id": "s0",
                "version": "3",
                "type": "skill_version",
                "name": "helper",
                "directory": "helper",
                "description": "d",
                "created_at": "2026-01-02T00:00:00Z",
            },
        ),
    )
    anthropic = build_fake_anthropic(router.dispatch)
    scope = resource_scope(tenant_id=str(uuid.uuid4()))
    attached = SkillRef(id="s0", version="latest", source="custom")

    async def latest() -> str:
        return await session_skills.latest_version(anthropic, scope, attached)

    assert await latest() == "1", "looked up"
    versions["s0"] = "2"  # published elsewhere
    assert await latest() == "1", "remembered inside the TTL"
    now[0] = LATEST_VERSION_TTL_S
    assert await latest() == "2", "seen once the TTL runs out"
    versions["s0"] = "3"
    await publish_skill_version(anthropic, "s0", data=ARCHIVE, scope=scope)
    assert await latest() == "3", "a version published here is seen at once"
    assert len(hits) == 3, f"one lookup per miss: {hits}"


def test_every_skill_version_daimon_publishes_forgets_its_remembered_latest() -> None:
    """Each production call that publishes or deletes a skill version sits in a
    function that drops the remembered latest version."""
    root = Path(__file__).resolve().parents[4]
    publishing = re.compile(r"\.(publish_version|versions\.create|delete_version)\(")
    callers = {
        path.relative_to(root).as_posix()
        for package in ("packages/core/daimon", "packages/adapters")
        for path in (root / package).rglob("*.py")
        if publishing.search(path.read_text())
    }
    assert callers == {
        "packages/core/daimon/core/mux_compat.py",
        "packages/core/daimon/core/neutral_resources.py",
    }, f"a new publish path must forget the remembered latest version: {callers}"
    for caller in callers:
        text = (root / caller).read_text()
        assert text.count("LATEST_VERSIONS.forget(") >= len(publishing.findall(text)), caller


async def test_a_cold_preview_of_the_default_agents_skills_fits_its_budget(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """23 skills at `latest`, none measured, through the production pacer sped up
    20 times: the read outlasts the old 30 s budget and fits the preview's. A
    second preview makes no Skills request at all."""
    tenant = await make_tenant(db_session)
    await db_session.commit()
    ids = [f"skill-{i}" for i in range(DEFAULT_AGENT_SKILLS)]
    hits: list[str] = []
    router = MARouter()
    _serve_skills(router, dict.fromkeys(ids, "4"), hits)
    transport = SkillsRateLimitedTransport(
        PRODUCTION_RPM * SPEEDUP, inner=httpx.MockTransport(router.dispatch)
    )
    anthropic = build_async_anthropic(transport=transport)
    carried = [SkillRef(id=skill_id, version="latest", source="custom") for skill_id in ids]

    async def preview() -> dict[tuple[str, str], object]:
        _, sizes = await session_skills.measure_carried_skills(
            db_session_factory,
            anthropic,
            scope=resource_scope(tenant_id=str(tenant.id)),
            tenant_id=tenant.id,
            carried=carried,
            record=None,
        )
        return dict(sizes)

    started = time.monotonic()
    async with asyncio.timeout(PREVIEW_CAPTURE_BUDGET_S / SPEEDUP):
        sizes = await preview()
    elapsed = time.monotonic() - started
    assert set(sizes) == {(skill_id, "4") for skill_id in ids}, "every skill is measured"
    assert len(hits) == 3 * DEFAULT_AGENT_SKILLS, f"a lookup, a list and a download each: {hits}"
    assert elapsed > 30 / SPEEDUP, f"the old budget would have timed out: {elapsed:.2f}s"
    hits.clear()
    assert set(await preview()) == set(sizes), "the warm preview reads the same sizes"
    assert hits == [], f"a warm preview asks the Skills API nothing: {hits}"


def _api_error(status: int) -> anthropic.APIStatusError:
    request = httpx.Request("GET", "https://api.example.com/v1/skills")
    kind = {404: anthropic.NotFoundError, 429: anthropic.RateLimitError}.get(
        status, anthropic.InternalServerError
    )
    return kind("upstream", response=httpx.Response(status, request=request), body=None)


@pytest.mark.parametrize("version", ["1", "latest"])
@pytest.mark.parametrize("status", [429, 503])
async def test_a_blip_reading_a_skill_propagates_instead_of_dropping_it(
    status: int,
    version: str,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 429 or 5xx on the lookup or the download proves no loss: it reaches the
    capture, which asks for a retry, instead of a bogus `skill:` drop."""
    tenant = await make_tenant(db_session)
    await db_session.commit()

    async def blip(*_a: object, **_k: object) -> str:
        raise _api_error(status)

    monkeypatch.setattr(session_skills, "download_skill_version", blip)
    monkeypatch.setattr(session_skills, "latest_version", blip)
    with pytest.raises(TurnError) as raised:
        await session_skills.measure_carried_skills(
            db_session_factory,
            build_fake_anthropic(MARouter().dispatch),
            scope=resource_scope(tenant_id=str(tenant.id)),
            tenant_id=tenant.id,
            carried=[SkillRef(id="s0", version=version, source="custom")],
            record=None,
        )
    assert isinstance(raised.value.__cause__, anthropic.APIStatusError)
    assert transient_failure(raised.value), "a turn says try again, not not set up"


async def test_a_missing_skill_is_its_own_named_loss(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 404 is permanent: that skill goes unmeasured, a loss of it alone."""
    tenant = await make_tenant(db_session)
    await db_session.commit()

    async def gone(*_a: object, **_k: object) -> str:
        raise _api_error(404)

    monkeypatch.setattr(session_skills, "download_skill_version", gone)
    monkeypatch.setattr(session_skills, "latest_version", gone)
    pins, sizes = await session_skills.measure_carried_skills(
        db_session_factory,
        build_fake_anthropic(MARouter().dispatch),
        scope=resource_scope(tenant_id=str(tenant.id)),
        tenant_id=tenant.id,
        carried=[
            SkillRef(id="s0", version="1", source="custom"),
            SkillRef(id="s1", version="latest", source="custom"),
        ],
        record=None,
    )
    assert sizes == {}, f"nothing measured: {sizes}"
    assert session_skills.skill_losses(pins, ()) == ("skill:s0", "skill:s1"), "named losses"


async def test_measures_read_with_different_scopes_never_share_a_download(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A joiner inherits the first caller's scope and record, so only a caller
    reading the same way joins a measurement in flight."""
    tenant = await make_tenant(db_session)
    await db_session.commit()
    gate = asyncio.Event()
    readers: list[str] = []

    async def download(*_a: object, scope: Any, **_k: object) -> bytes:
        readers.append(scope.account_id)
        await gate.wait()
        return ARCHIVE

    monkeypatch.setattr(session_skills, "download_skill_version", download)
    anthropic_client = build_fake_anthropic(MARouter().dispatch)
    calls = [
        asyncio.create_task(
            session_skills.measure_skills(
                db_session_factory,
                anthropic_client,
                scope=resource_scope(tenant_id=str(tenant.id), account_id=account),
                tenant_id=tenant.id,
                pins=(("s0", "1"),),
            )
        )
        for account in ("a", "b")
    ]
    await asyncio.sleep(0.05)
    gate.set()
    await asyncio.gather(*calls)
    assert sorted(readers) == ["a", "b"], f"each scope reads for itself: {readers}"


async def test_a_remembered_latest_version_still_checks_the_skills_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hit is only for a ref whose source a lookup matched: one attached with
    another source asks again and is refused, as a miss is."""
    cache = LatestVersionCache()
    monkeypatch.setattr(session_skills, "LATEST_VERSIONS", cache)
    hits: list[str] = []
    router = MARouter()
    _serve_skills(router, {"s0": "1"}, hits)
    client = build_fake_anthropic(router.dispatch)
    scope = resource_scope(tenant_id=str(uuid.uuid4()))
    custom = SkillRef(id="s0", version="latest", source="custom")
    assert await session_skills.latest_version(client, scope, custom) == "1", "looked up"
    with pytest.raises(ValueError, match="no concrete current version"):
        await session_skills.latest_version(
            client, scope, custom.model_copy(update={"source": "anthropic"})
        )
    assert len(hits) == 2, f"the other source asked again: {hits}"
