"""GitHub: an organization's issues, pull requests, discussions and repository files, each
readable by the people GitHub lets read its repository, never more.

A repository's readers are its collaborators with read access (owners, team members, direct and
outside collaborators; pending invitations not included), plus every organization member while
the repository is public or internal. Each run publishes that list as one group per repository
(`github/<org>:repo:<id>`); every record of the repository is readable by exactly that group,
so a repository made private, or a person removed, takes effect on the next run. A repository
whose list cannot be read is not published and its records go dark when its last list lapses
(three hours). Issues turned off hide the repository's issues. A repository the source no longer
lists publishes nobody, so its records are hidden at once; listed again within `LOST` (30 days)
they show again, later they go.

Content, by `config.content` (default `issues, pulls`): issues and pull requests with their
conversation comments, discussions with their comments and replies, and the text files of
`config.file_paths`. Each run reads what changed since the last one; every `relist_hours`
(168) it reads everything again, which is how a deleted or transferred record is removed.

Credential `{"token": "..."}`: a fine-grained token (or an App installation token) with read
access to the organization's repositories: contents, issues, pull requests, discussions and
metadata, plus administration read to list collaborators, and organization members read.
Configuration (`sources.config`): `org`, `repos` (names to read; empty: all), `content`,
`file_paths`, `branch` (default: each repository's own), `relist_hours`.
"""

from __future__ import annotations

import re
import time
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import ClassVar, Final
from urllib.parse import quote

import httpx
from daimon.data.access import subject
from daimon.data.identity import SourceAccount
from daimon.data.sync.connector import (
    Acl,
    AclRef,
    Checkpoint,
    Connector,
    Descriptor,
    Field,
    ItemRecord,
    Json,
    PermissionUnit,
    Record,
    Roster,
    Source,
    SyncState,
)
from daimon.data.sync.connectors.chat import lost_for_good, names, obj, rows
from daimon.data.sync.connectors.github_api import (
    Api,
    Refused,
    blob,
    client_of,
    head,
    repositories,
    tree,
    wants,
    when,
)
from daimon.data.sync.http import Upstream
from daimon.data.sync.request_cache import once

PROVIDER: Final = "github"
KINDS: Final = ("issues", "pulls", "discussions", "files")
UNIT_MINUTES: Final = 180
"""How long a repository's reader list stays good without a new pass."""
OVERLAP: Final = timedelta(minutes=5)
"""A repository is read this far before the last read began."""
TEXT_MAX: Final = 100_000
OPEN_TO_ORG: Final = ("public", "internal")
CHECK_EVERY: Final = 25
HELD: Final = (("issue", "#"), ("pull_request", "#"), ("discussion", "#"), ("file", ":"))
"""The id prefixes of one repository's records: `<kind>:<repo id><separator>`."""
ID: Final = re.compile(r"[a-z_]+:(\d+)[#:]")

DISCUSSIONS: Final = (
    "query($owner: String!, $name: String!, $after: String) {"
    " repository(owner: $owner, name: $name) {"
    " discussions(first: 25, after: $after, orderBy: {field: UPDATED_AT, direction: DESC}) {"
    " pageInfo { hasNextPage endCursor } nodes { number title body url createdAt updatedAt"
    " author { login } category { name } comments(first: 50) { nodes { body author { login }"
    " replies(first: 20) { nodes { body author { login } } } } } } } } }"
)


@dataclass(frozen=True)
class Config:
    org: str
    repos: frozenset[str]
    content: tuple[str, ...]
    file_paths: tuple[str, ...]
    branch: str
    relist: timedelta

    @classmethod
    def of(cls, config: Json) -> Config:
        org = str(config.get("org") or "").strip()
        if not org:
            raise ValueError("config.org must name the GitHub organization")
        content = tuple(names(config, "content")) or ("issues", "pulls")
        if bad := [c for c in content if c not in KINDS]:
            raise ValueError(f"config.content: {', '.join(bad)} is not one of {', '.join(KINDS)}")
        paths = tuple(names(config, "file_paths"))
        if "files" in content and not paths:
            raise ValueError("config.file_paths lists the files or folders to read")
        raw = str(config.get("relist_hours") or "").strip()
        if raw and not (raw.isascii() and raw.isdigit() and int(raw) >= 1):
            raise ValueError("config.relist_hours must be a whole number, 1 or more")
        return cls(
            org,
            frozenset(r.lower() for r in names(config, "repos")),
            content,
            paths,
            str(config.get("branch") or "").strip(),
            timedelta(hours=int(raw or 168)),
        )

    @property
    def signature(self) -> str:
        return "|".join(
            (self.org, *sorted(self.repos), *self.content, *self.file_paths, self.branch)
        )


def repo_group(org: str, repo_id: object) -> str:
    return subject(PROVIDER, org, "repo", str(repo_id))


def repo_acl(org: str, repo_id: object) -> Acl:
    return Acl((AclRef(repo_group(org, repo_id), group=True),))


def repo_of(external_id: str) -> str | None:
    """The repository id in `<kind>:<repo id>#<number>` or `file:<repo id>:<path>`."""
    m = ID.match(external_id)
    return m[1] if m else None


def comment_text(c: Json, depth: int = 0) -> str:
    who = str(obj(c.get("user") or c.get("author")).get("login") or "someone")
    return "  " * depth + f"{who}: {str(c.get('body') or '').strip()}"


class GitHubConnector(Connector):
    kind: ClassVar[str] = "github"
    name: ClassVar[str] = "GitHub"
    category: ClassVar[str] = "code"
    descriptor: ClassVar[Descriptor | None] = Descriptor(PROVIDER)
    recheck_reads: ClassVar[bool] = True
    fields: ClassVar[tuple[Field, ...]] = (
        Field("token", "Token", "secret"),
        Field("org", "Organization", placeholder="your-org"),
        Field("repos", "Only these repositories", "list", required=False, placeholder="names"),
        Field(
            "content",
            "Content",
            "list",
            required=False,
            placeholder="issues, pulls, discussions, files",
        ),
        Field(
            "file_paths", "Files or folders", "list", required=False, placeholder="docs, README.md"
        ),
        Field("branch", "Branch", required=False, placeholder="each repository's default"),
        Field("relist_hours", "Hours between full reads", required=False, placeholder="168"),
    )

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        """`transport` replaces the guarded one (tests)."""
        self.transport = transport

    def workspace_of(self, source: Source) -> str:
        return Config.of(source.config).org

    async def _repos(self, api: Api, cfg: Config) -> list[Json]:
        return [
            r
            for r in await repositories(api, cfg.org)
            if not cfg.repos or str(r["name"]).lower() in cfg.repos
        ]

    async def test(self, source: Source) -> tuple[bool, str]:
        try:
            cfg = Config.of(source.config)
            async with client_of(source, self.transport) as client:
                api = Api(client, source)
                repos = await self._repos(api, cfg)
                if repos and await self._readers(api, repos[0]) is None:
                    return False, "the token cannot list collaborators (administration read)"
        except (ValueError, Upstream, httpx.HTTPError) as e:
            return False, str(e)
        return True, f"{len(repos)} repositories"

    # ---- who reads ----

    async def _readers(self, api: Api, repo: Json) -> set[str] | None:
        """The ids of the people with read access to the repository; None: not listable now.
        Listed once per request, on a client of its own (the first caller may be gone)."""
        key = ("github-readers", api.source.id, repo["id"])
        found = await once(key, lambda: self._list_readers(api.source, repo))
        return None if found is None else set(found)

    async def _list_readers(self, source: Source, repo: Json) -> frozenset[str] | None:
        async with client_of(source, self.transport) as client:
            try:
                return frozenset(
                    [
                        str(u["id"])
                        async for u in Api(client, source).pages(
                            f"/repositories/{repo['id']}/collaborators", {"affiliation": "all"}
                        )
                        if obj(u.get("permissions")).get("pull") is True
                    ]
                )
            except Refused as e:
                if e.status in (403, 404):
                    return None
                raise

    async def permissions(self, source: Source) -> AsyncGenerator[PermissionUnit]:
        """One unit per repository, every run: its readers, and the organization's members
        while it is public or internal."""
        cfg = Config.of(source.config)
        members: set[str] | None = None
        async with client_of(source, self.transport) as client:
            api = Api(client, source)
            repos = await self._repos(api, cfg)
            for repo in repos:
                ids = await self._readers(api, repo)
                if ids is None:
                    continue
                if repo.get("visibility") in OPEN_TO_ORG:
                    if members is None:
                        members = {
                            str(u["id"]) async for u in api.pages(f"/orgs/{quote(cfg.org)}/members")
                        }
                    ids |= members
                yield PermissionUnit(
                    "repo",
                    str(repo["id"]),
                    UNIT_MINUTES,
                    {
                        repo_group(cfg.org, repo["id"]): tuple(
                            subject(PROVIDER, cfg.org, "user", i) for i in sorted(ids)
                        )
                    },
                    time.time_ns() // 1000,
                )
        for rid in set(obj(source.state.get("since"))) - {str(r["id"]) for r in repos}:
            # No longer listed: nobody reads it from now, not from when its last list lapses.
            yield PermissionUnit(
                "repo", rid, UNIT_MINUTES, {repo_group(cfg.org, rid): ()}, time.time_ns() // 1000
            )

    async def accounts(self, source: Source) -> Roster | None:
        """Organization members and outside collaborators. GitHub verifies no address, so
        each person is linked by their GitHub sign-in or by an admin."""
        cfg = Config.of(source.config)
        out: dict[str, SourceAccount] = {}
        async with client_of(source, self.transport) as client:
            api = Api(client, source)
            for path in (
                f"/orgs/{quote(cfg.org)}/members",
                f"/orgs/{quote(cfg.org)}/outside_collaborators",
            ):
                async for u in api.pages(path):
                    out[str(u["id"])] = SourceAccount(str(u["id"]), str(u.get("login") or ""))
        return Roster(PROVIDER, cfg.org, tuple(out.values()))

    async def can_open(
        self, source: Source, external_id: str, subjects: Sequence[str]
    ) -> bool | None:
        """Whether GitHub lists one of these people (numeric ids) as able to read the
        record's repository now."""
        cfg = Config.of(source.config)
        rid = repo_of(external_id)
        if rid is None:
            return None
        async with client_of(source, self.transport) as client:
            api = Api(client, source)
            r = await api.get(f"/repositories/{rid}", ok=(200, 404))
            if r.status_code != 200:
                return False
            repo = obj(r.json())
            if external_id.startswith("issue:") and not repo.get("has_issues", True):
                return False
            ids = await self._readers(api, repo)
            if ids is None:
                return None
            if ids & set(subjects):
                return True
            if repo.get("visibility") not in OPEN_TO_ORG:
                return False
            for i in subjects:
                login = obj((await api.get(f"/user/{quote(i)}", ok=(200, 404))).json()).get("login")
                if (
                    login
                    and (
                        await api.get(
                            f"/orgs/{quote(cfg.org)}/members/{quote(str(login))}",
                            ok=(204, 302, 404),
                        )
                    ).status_code
                    == 204
                ):
                    return True
            return False

    # ---- content ----

    async def fetch(self, source: Source) -> AsyncGenerator[Record]:
        cfg = Config.of(source.config)
        started = datetime.now(UTC)
        old = source.state if source.state.get("sig") == cfg.signature else {}
        full_at = when(old["full_at"]) if old.get("full_at") else None
        full = full_at is None or started - full_at >= cfg.relist
        since = {str(k): str(v) for k, v in obj(old.get("since")).items()}
        blobs = obj(old.get("blobs"))
        state: Json = {
            "v": 1,
            "sig": cfg.signature,
            "since": dict(since),
            "blobs": dict(blobs),
            "full_at": old.get("full_at"),
            "lost": dict(obj(old.get("lost"))),
        }
        live: set[str] = set()
        n = 0
        async with client_of(source, self.transport) as client:
            api = Api(client, source)
            repos = await self._repos(api, cfg)
            missing = set(since) - {str(r["id"]) for r in repos}
            for rid in lost_for_good(state["lost"], missing, started):
                del state["since"][rid]
                state["blobs"].pop(rid, None)
            for repo in repos:
                rid = str(repo["id"])
                mark = None if full else since.get(rid)
                async for rec in self._repo(api, cfg, repo, mark, state, live):
                    yield rec
                    n += 1
                    if n % CHECK_EVERY == 0:
                        yield Checkpoint(state)
                state["since"][rid] = (started - OVERLAP).isoformat()
                yield Checkpoint(state)
        if full:
            state["full_at"] = started.isoformat()
        keep = tuple(f"{k}:{r}{s}" for r in state["lost"] for k, s in HELD)
        yield SyncState(state, live if full else None, keep=keep)

    async def _repo(
        self,
        api: Api,
        cfg: Config,
        repo: Json,
        since: str | None,
        state: Json,
        live: set[str],
    ) -> AsyncGenerator[ItemRecord]:
        rid, full_name = str(repo["id"]), str(repo["full_name"])
        acl = repo_acl(cfg.org, rid)
        if "issues" in cfg.content or "pulls" in cfg.content:
            params: dict[str, str | int] = {"state": "all", "sort": "updated", "direction": "asc"}
            if since:
                params["since"] = since
            if repo.get("has_issues", True):
                async for j in api.pages(f"/repos/{full_name}/issues", params, ok=(200, 410)):
                    pull = "pull_request" in j
                    if ("pulls" if pull else "issues") not in cfg.content:
                        continue
                    rec = await self._issue(api, repo, j, pull, acl)
                    live.add(rec.external_id)
                    yield rec
            elif "pulls" in cfg.content:
                async for j in api.pages(f"/repos/{full_name}/pulls", params):
                    rec = await self._issue(api, repo, j, True, acl)
                    live.add(rec.external_id)
                    yield rec
        if "discussions" in cfg.content:
            async for rec in self._discussions(api, repo, since, acl):
                live.add(rec.external_id)
                yield rec
        if "files" in cfg.content:
            async for rec in self._files(api, cfg, repo, state, live, acl):
                yield rec

    async def _issue(self, api: Api, repo: Json, j: Json, pull: bool, acl: Acl) -> ItemRecord:
        full_name, number = str(repo["full_name"]), int(j["number"])
        lines = [str(j.get("body") or "").strip()]
        if int(j.get("comments") or 0):
            lines += [
                comment_text(c)
                async for c in api.pages(f"/repos/{full_name}/issues/{number}/comments")
            ]
        kind = "pull_request" if pull else "issue"
        return ItemRecord(
            f"{kind}:{repo['id']}#{number}",
            kind,
            f"{j.get('title') or ''} ({repo['name']}#{number})",
            "\n\n".join(x for x in lines if x)[:TEXT_MAX],
            when(j.get("updated_at")),
            container=full_name,
            container_kind="repo",
            author=str(obj(j.get("user")).get("login") or "") or None,
            url=str(j["html_url"]) if j.get("html_url") else None,
            payload={
                "repo_id": repo["id"],
                "number": number,
                "state": j.get("state"),
                "labels": [str(obj(x).get("name")) for x in rows(j.get("labels"))],
            },
            acl=acl,
        )

    async def _discussions(
        self, api: Api, repo: Json, since: str | None, acl: Acl
    ) -> AsyncGenerator[ItemRecord]:
        owner, name = str(repo["full_name"]).split("/", 1)
        after: str | None = None
        while True:
            data = obj(
                obj(
                    (await api.graphql(DISCUSSIONS, owner=owner, name=name, after=after)).get(
                        "repository"
                    )
                ).get("discussions")
            )
            for d in rows(data.get("nodes")):
                if since and when(d.get("updatedAt")) < when(since):
                    return
                lines = [str(d.get("body") or "").strip()]
                for c in rows(obj(d.get("comments")).get("nodes")):
                    lines.append(comment_text(c))
                    lines += [comment_text(r, 1) for r in rows(obj(c.get("replies")).get("nodes"))]
                yield ItemRecord(
                    f"discussion:{repo['id']}#{d['number']}",
                    "discussion",
                    f"{d.get('title') or ''} ({repo['name']}#{d['number']})",
                    "\n\n".join(x for x in lines if x)[:TEXT_MAX],
                    when(d.get("updatedAt")),
                    container=str(repo["full_name"]),
                    container_kind="repo",
                    author=str(obj(d.get("author")).get("login") or "") or None,
                    url=str(d["url"]) if d.get("url") else None,
                    payload={
                        "repo_id": repo["id"],
                        "number": d["number"],
                        "category": obj(d.get("category")).get("name"),
                    },
                    acl=acl,
                )
            page = obj(data.get("pageInfo"))
            if not page.get("hasNextPage"):
                return
            after = str(page["endCursor"])

    async def _files(
        self, api: Api, cfg: Config, repo: Json, state: Json, live: set[str], acl: Acl
    ) -> AsyncGenerator[ItemRecord]:
        rid, full_name = str(repo["id"]), str(repo["full_name"])
        got = await head(api, full_name, cfg.branch or str(repo.get("default_branch") or ""))
        if got is None:
            return
        sha, date = got
        files = await tree(api, full_name, sha, wants(list(cfg.file_paths)))
        known = {str(k): str(v) for k, v in obj(state["blobs"].get(rid)).items()}
        for path in sorted(files):
            live.add(f"file:{rid}:{path}")
            if known.get(path) == files[path]:
                continue
            text = await blob(api, full_name, files[path])
            yield ItemRecord(
                f"file:{rid}:{path}",
                "file",
                f"{path} ({repo['name']})",
                text[:TEXT_MAX],
                date,
                container=full_name,
                container_kind="repo",
                url=f"https://github.com/{full_name}/blob/{sha}/{path}",
                payload={"repo_id": repo["id"], "path": path, "sha": sha},
                acl=acl,
            )
        state["blobs"][rid] = files
