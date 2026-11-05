"""The library's skill files: saved against the loaded version, authored, created, restored."""

from __future__ import annotations

import asyncio
import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from anthropic.types.beta import SkillListResponse
from anthropic.types.beta.skills import VersionCreateResponse, VersionListResponse
from daimon.core import skill_library, skill_library_files
from daimon.core.agent_editing import AgentActor, AgentEditDeps, AgentEditError
from daimon.core.authz import Subject, Surface
from daimon.core.defaults.metadata import tenant_scoped_display_title
from daimon.core.mcp_skills.install import ServerSkill
from daimon.core.mcp_skills.manifest import parse_entry
from daimon.core.server_skills.mirror import add_server_skill
from daimon.core.stores import server_skills, web_audit
from daimon.testing.factories import make_account, make_tenant, make_web_person
from daimon.testing.mcp_skills import SkillsServer, skill_md
from mux.testing.drivers.anthropic.ma import MARouter, build_fake_anthropic, list_response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_TS = "2026-04-21T00:00:00Z"
_SKILL_MD = "---\nname: notes\ndescription: Take notes.\n---\nWrite them down.\n"
_NOW = datetime(2030, 1, 1, tzinfo=UTC)


def _uploaded_files(request: httpx.Request) -> dict[str, bytes]:
    start = request.content.index(b"PK\x03\x04")
    with zipfile.ZipFile(io.BytesIO(request.content[start:])) as archive:
        return {
            name.split("/", 1)[1]: archive.read(name)
            for name in archive.namelist()
            if not name.endswith("/")
        }


def _zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path, body in files.items():
            archive.writestr(f"notes/{path}", body)
    return buffer.getvalue()


@dataclass
class _Library:
    """One custom library skill whose versions are kept in memory."""

    title: str
    versions: dict[str, dict[str, bytes]] = field(default_factory=dict[str, dict[str, bytes]])
    created: list[str] = field(default_factory=list[str])
    refuse_publish: bool = False

    def router(self) -> MARouter:
        router = MARouter()
        router.add("GET", r"/v1/skills$", lambda _r, _m: list_response(self._listed()))
        router.add("POST", r"/v1/skills$", self._create)
        router.add("GET", r"/v1/skills/sk_1/versions$", self._listed_versions)
        router.add("POST", r"/v1/skills/sk_1/versions$", self._publish)
        router.add("GET", r"/v1/skills/sk_1/versions/(?P<version>[^/]+)/content$", self._content)
        return router

    def _listed(self) -> list[dict[str, Any]]:
        return [
            SkillListResponse(
                id="sk_1",
                type="custom",
                display_title=self.title,
                latest_version=max(self.versions),
                created_at=_TS,
                updated_at=_TS,
                source="custom",
            ).model_dump(mode="json")
        ]

    def _version(self, version: str) -> dict[str, Any]:
        return VersionListResponse(
            id=f"ver_{version}",
            skill_id="sk_1",
            version=version,
            type="skill_version",
            name="SKILL.zip",
            directory="/",
            description="",
            created_at=f"2026-04-2{version}T00:00:00Z",
        ).model_dump(mode="json")

    def _listed_versions(self, _r: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        rows = [self._version(v) for v in self.versions]
        return httpx.Response(200, json={"data": rows, "has_more": False, "next_page": None})

    def _publish(self, request: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        if self.refuse_publish:
            return httpx.Response(
                400,
                json={"type": "error", "error": {"type": "invalid_request_error", "message": "no"}},
            )
        version = str(len(self.versions) + 1)
        self.versions[version] = _uploaded_files(request)
        created = VersionCreateResponse.model_validate(self._version(version))
        return httpx.Response(200, json=created.model_dump(mode="json"))

    def _create(self, request: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        self.created.append(request.content.decode("latin-1"))
        body = SkillListResponse(
            id="sk_new",
            type="custom",
            display_title="x",
            latest_version="1",
            created_at=_TS,
            updated_at=_TS,
            source="custom",
        )
        return httpx.Response(200, json=body.model_dump(mode="json"))

    def _content(self, _r: httpx.Request, match: re.Match[str]) -> httpx.Response:
        version = match["version"].removeprefix("ver_")
        return httpx.Response(200, content=_zip(self.versions[version]))


async def _setup(
    db_session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    *,
    admin: bool = True,
) -> tuple[_Library, AgentEditDeps, AgentActor]:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await make_web_person(db_session, tenant=tenant, account=account, display_name="Ada")
    await db_session.commit()
    library = _Library(
        title=tenant_scoped_display_title(tenant_id=tenant.id, name="notes"),
        versions={
            "1": {"SKILL.md": _SKILL_MD.encode()},
            "2": {"SKILL.md": _SKILL_MD.encode(), "ref.md": b"reference"},
        },
    )
    deps = AgentEditDeps(
        client=build_fake_anthropic(library.router().dispatch),
        session_factory=factory,
        public_url=None,
    )
    actor = AgentActor(
        tenant_id=tenant.id,
        account_id=account.id,
        subject=Subject(is_admin=admin, platform_user_id=str(account.id)),
        surface=Surface.WEB,
    )
    return library, deps, actor


async def test_a_save_against_an_older_version_is_refused_as_stale(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    library, deps, actor = await _setup(db_session, db_session_factory)

    with pytest.raises(AgentEditError) as refused:
        await skill_library_files.save_library_files(
            deps,
            actor=actor,
            skill_id="sk_1",
            files={"SKILL.md": _SKILL_MD, "ref.md": None},
            expected_version="1",
        )

    assert refused.value.kind == "stale", "a later version is not overwritten"
    assert sorted(library.versions) == ["1", "2"], "nothing was published"


async def test_a_provider_failure_is_unavailable_not_a_conflict(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    library, deps, actor = await _setup(db_session, db_session_factory)
    library.refuse_publish = True

    with pytest.raises(AgentEditError) as refused:
        await skill_library_files.save_library_files(
            deps,
            actor=actor,
            skill_id="sk_1",
            files={"SKILL.md": _SKILL_MD + "More.\n", "ref.md": None},
            expected_version="2",
        )

    assert refused.value.kind == "unavailable", "the editor keeps the draft and may retry"


async def test_two_saves_of_one_version_publish_once(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    library, deps, actor = await _setup(db_session, db_session_factory)

    results = await asyncio.gather(
        *(
            skill_library_files.save_library_files(
                deps,
                actor=actor,
                skill_id="sk_1",
                files={"SKILL.md": _SKILL_MD + text, "ref.md": None},
                expected_version="2",
            )
            for text in ("One.\n", "Two.\n")
        ),
        return_exceptions=True,
    )

    refused = [r for r in results if isinstance(r, AgentEditError)]
    assert [r.kind for r in refused] == ["stale"], "the second save is refused, not merged"
    assert sorted(library.versions) == ["1", "2", "3"], "one version was published"


async def test_a_save_keeps_unchanged_files_from_the_loaded_version(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    library, deps, actor = await _setup(db_session, db_session_factory)

    saved = await skill_library_files.save_library_files(
        deps,
        actor=actor,
        skill_id="sk_1",
        files={"SKILL.md": _SKILL_MD + "More.\n", "ref.md": None},
        expected_version="2",
    )

    assert (saved.name, saved.version) == ("notes", "3")
    assert library.versions["3"]["ref.md"] == b"reference", "ref.md is read from v2"


async def test_a_save_may_not_rename_the_skill(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _library, deps, actor = await _setup(db_session, db_session_factory)
    renamed = _SKILL_MD.replace("name: notes", "name: other")

    with pytest.raises(AgentEditError) as refused:
        await skill_library_files.save_library_files(
            deps, actor=actor, skill_id="sk_1", files={"SKILL.md": renamed}, expected_version="2"
        )

    assert refused.value.kind == "invalid"


async def test_only_an_admin_reads_or_changes_the_files(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _library, deps, actor = await _setup(db_session, db_session_factory, admin=False)

    kinds: list[str] = []
    for call in (
        skill_library_files.read_library_files(deps, actor=actor, skill_id="sk_1"),
        skill_library_files.save_library_files(
            deps, actor=actor, skill_id="sk_1", files={}, expected_version="2"
        ),
        skill_library_files.restore_library_version(
            deps, actor=actor, skill_id="sk_1", version="1"
        ),
        skill_library_files.create_library_skill(
            deps, actor=actor, name="Plots", description="Draw."
        ),
    ):
        with pytest.raises(AgentEditError) as refused:
            await call
        kinds.append(refused.value.kind)

    assert kinds == ["forbidden"] * 4


async def test_a_restore_publishes_the_old_files_as_a_new_version(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    library, deps, actor = await _setup(db_session, db_session_factory)

    restored = await skill_library_files.restore_library_version(
        deps, actor=actor, skill_id="sk_1", version="1"
    )

    assert restored.version == "3"
    assert list(library.versions["3"]) == ["SKILL.md"], "v1 had no ref.md"


async def test_a_new_skill_gets_a_slug_and_a_taken_name_is_refused(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    library, deps, actor = await _setup(db_session, db_session_factory)

    made = await skill_library_files.create_library_skill(
        deps, actor=actor, name="Data Plots", description="Draw plots."
    )
    with pytest.raises(AgentEditError) as taken:
        await skill_library_files.create_library_skill(
            deps, actor=actor, name="Notes", description="Again."
        )

    assert (made.id, made.name) == ("sk_new", "data-plots")
    assert len(library.created) == 1, "only the free name is created"
    assert taken.value.kind == "taken"


async def test_a_version_names_who_saved_it(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:

    _library, deps, actor = await _setup(db_session, db_session_factory)
    async with db_session_factory.begin() as session:
        await web_audit.record(
            session,
            tenant_id=actor.tenant_id,
            actor_account_id=actor.account_id,
            action=skill_library.SAVED_ACTION,
            target="notes v2",
            at=datetime(2030, 1, 1, tzinfo=UTC),
        )

    versions = await skill_library.skill_versions(deps, actor=actor, skill_id="sk_1")

    assert [(v.version, v.author) for v in versions] == [("2", "Ada"), ("1", None)]


async def _mirror_sk_1(deps: AgentEditDeps, actor: AgentActor) -> None:
    async with deps.session_factory() as session, session.begin():
        await server_skills.create_mirror(
            session,
            tenant_id=actor.tenant_id,
            server="data",
            uri="skill://example/notes/SKILL.md",
            name="notes",
            skill_id="sk_1",
            version_label=None,
            digest="d",
            manifest={},
            allowed_tools=None,
            added_by_account_id=None,
            now=datetime(2030, 1, 1, tzinfo=UTC),
        )


async def test_a_server_skill_is_read_only_until_forked(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    library, deps, actor = await _setup(db_session, db_session_factory)
    await _mirror_sk_1(deps, actor)

    with pytest.raises(AgentEditError) as refused:
        await skill_library_files.save_library_files(
            deps, actor=actor, skill_id="sk_1", files={"ref.md": "x"}, expected_version="2"
        )
    fork = await skill_library_files.fork_library_skill(
        deps, actor=actor, skill_id="sk_1", name="My Notes"
    )

    assert refused.value.kind == "forbidden"
    assert (fork.id, fork.name) == ("sk_new", "my-notes")
    sent = httpx.Request(
        "POST", "https://example.com", content=library.created[0].encode("latin-1")
    )
    files = _uploaded_files(sent)
    assert b"name: my-notes\n" in files["SKILL.md"] and files["ref.md"] == b"reference"


def test_a_fork_renames_only_the_frontmatter() -> None:
    renamed = skill_library_files._renamed(  # pyright: ignore[reportPrivateUsage]
        "---\nname: notes\ndescription: d\n---\nname: keep\n", "copy"
    )
    assert renamed == "---\nname: copy\ndescription: d\n---\nname: keep\n"
    assert skill_library_files._renamed("no frontmatter", "copy") is None  # pyright: ignore[reportPrivateUsage]


async def test_adding_a_server_skill_mirrors_it_once(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    library, deps, actor = await _setup(db_session, db_session_factory)
    server = SkillsServer(skills={"plots": {"SKILL.md": skill_md("plots")}})
    entry = parse_entry(server.entries()[0])
    assert entry is not None
    fetched = ServerSkill(entry=entry, files=dict(server.skills["plots"]))
    first = await add_server_skill(deps, actor=actor, server="data", fetched=fetched, now=_NOW)
    again = await add_server_skill(deps, actor=actor, server="data", fetched=fetched, now=_NOW)

    assert (first.id, first.name) == ("sk_new", "plots") and again == first
    assert len(library.created) == 1
    async with db_session_factory() as session:
        mirror = await server_skills.mirror_of_skill(
            session, tenant_id=actor.tenant_id, skill_id="sk_new"
        )
        assert mirror is not None and mirror.uri == entry.uri, "the mirror keeps its source"
        assert mirror.added_by_account_id == actor.account_id, "the sync asks as the adder"
        held = await server_skills.list_deployments(session, mirror.id)
    assert {p: d.version for p, d in held.items()} == {"managed_agents": "1"}
