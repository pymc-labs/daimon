"""What the Toggl connector's reads share: the client, the calls inside the source's budget
(GET, and the one POST that searches time entries; never `/me`), and the people and roles.

Who reads a time entry is what Toggl shows, or less: its author while their account is
active, every holder of an "all" role (owner, admin and the roles that see everything), and
the managers of its project. Project lead, group roles, public projects and departed authors
grant nothing: stricter than Toggl, by design.
"""

from __future__ import annotations

import time
from datetime import UTC, date, datetime
from typing import Final, cast

import httpx
from daimon.data.access import subject
from daimon.data.sync.connector import Acl, AclRef, Json, Source
from daimon.data.sync.connectors.chat import rows
from daimon.data.sync.http import GuardedTransport, Upstream, send

PROVIDER: Final = "toggl"
API: Final = "https://api.track.toggl.com"
PAGE_ROWS: Final = 5000
PAGE_SIZE: Final = 200
PAGES: Final = 500
"""Pages read of one list at most: a list that never ends is a fault."""
SEES_ALL: Final = frozenset({"1", "2", "6", "7"})
"""Role ids that read every entry of the workspace; any other role id grants nothing."""
ORIGIN: Final = date(2010, 1, 1)
"""Where a source reads history from when its config names no day."""
UNIT_MINUTES: Final = 60
"""How long a permission pass stays good: four 15-minute runs."""


def ident(value: object) -> str | None:
    return None if value is None else str(value)


def stamp(value: object) -> datetime | None:
    """An ISO time as sent (its own offset kept: a report's day is its author's own)."""
    return datetime.fromisoformat(str(value)) if value else None


def is_active(user: Json) -> bool:
    return bool(user.get("active")) and not user.get("inactive")


def sees_all(user: Json) -> bool:
    return is_active(user) and str(user.get("role_id")) in SEES_ALL


def user_ref(workspace: str, user_id: object) -> str:
    return subject(PROVIDER, workspace, "user", str(user_id))


def roles_group(workspace: str) -> str:
    return subject(PROVIDER, workspace, "role", "all")


def managers_group(workspace: str, project_id: object) -> str:
    return subject(PROVIDER, workspace, "project", f"{project_id}:managers")


def metadata_acl(workspace: str) -> Acl:
    return Acl((AclRef(roles_group(workspace), group=True),))


def entry_acl(workspace: str, user_id: object, project_id: object) -> Acl:
    """The author (only while their person unit is live: an inactive or departed account
    renews none), every "all" role, and the managers of the entry's project."""
    refs: list[AclRef] = []
    if user_id is not None:
        mine = user_ref(workspace, user_id)
        refs.append(AclRef(mine, unit=("person", mine)))
    refs.append(AclRef(roles_group(workspace), group=True))
    if project_id is not None:
        refs.append(AclRef(managers_group(workspace, project_id), group=True))
    return Acl(tuple(refs))


def local_day(start: str) -> date:
    """The author's own calendar day of an entry."""
    return datetime.fromisoformat(start).date()


def now() -> datetime:
    return datetime.now(UTC)


def seq() -> int:
    """Orders the passes of one unit: a later pass has a larger number."""
    return time.time_ns() // 1000


class Config:
    def __init__(self, config: Json) -> None:
        self.workspace = str(config.get("workspace") or "").strip()
        if not self.workspace.isascii() or not self.workspace.isdigit():
            raise ValueError("config.workspace must be the workspace's numeric id")
        raw = str(config.get("history_from") or "").strip()
        try:
            self.history_from = date.fromisoformat(raw) if raw else ORIGIN
        except ValueError:
            raise ValueError("config.history_from must be a day, YYYY-MM-DD") from None

    @property
    def signature(self) -> str:
        return f"{self.workspace}|{self.history_from}"


def client_of(source: Source, transport: httpx.AsyncBaseTransport | None) -> httpx.AsyncClient:
    token = source.credentials.get("token", "")
    if not token:
        raise ValueError("the credential needs the token")
    return httpx.AsyncClient(
        base_url=API,
        auth=(token, "api_token"),
        timeout=60,
        transport=transport or GuardedTransport(source.private_hosts),
    )


class Refused(Upstream):
    """Toggl answered a call with a status the caller did not accept."""

    def __init__(self, status: int, path: str) -> None:
        super().__init__(f"Toggl answered HTTP {status} for {path.rsplit('/', 1)[-1] or path}")
        self.status = status


class Api:
    """One workspace's calls. A 402 (hourly quota spent) or 429 waits what Toggl asks; with
    `live` set a read never waits: it fails and the search leaves the source out."""

    def __init__(
        self, client: httpx.AsyncClient, source: Source, workspace: str, *, live: bool = False
    ) -> None:
        self.client, self.source, self.live = client, source, live
        self.base = f"/api/v9/workspaces/{workspace}"
        self.report_path = f"/reports/api/v3/workspace/{workspace}/search/time_entries"

    async def _send(
        self, path: str, params: Json | None = None, body: Json | None = None
    ) -> httpx.Response:
        return await send(
            self.client,
            self.source.budget,
            path,
            params,
            deadline=self.source.deadline,
            json=body,
            throttled=(402, 429),
            cap=0.0 if self.live else 600.0,
            waits=() if self.live else None,
        )

    async def json(self, path: str, **params: str | int) -> object:
        r = await self._send(path, params)
        if r.status_code != 200:
            raise Refused(r.status_code, path)
        return r.json()

    async def pages(self, path: str, **params: str | int) -> list[Json]:
        """A v9 list read page by page (a plain list or `{data: [...]}`) until a page is short."""
        out: list[Json] = []
        for page in range(1, PAGES + 1):
            body = await self.json(path, per_page=PAGE_SIZE, page=page, **params)
            batch = rows(cast("Json", body).get("data") if isinstance(body, dict) else body)
            out += batch
            if len(batch) < PAGE_SIZE:
                return out
        raise Upstream(f"Toggl returned more than {PAGES} pages of {path}")

    async def report(self, body: Json, row: int | None) -> tuple[list[Json], int | None]:
        """One page of the time entry search (`grouped` false: a row per entry) and the row
        number the next page starts at; None on the last."""
        r = await self._send(
            self.report_path, None, {**body, **({"first_row_number": row} if row else {})}
        )
        if r.status_code != 200:
            raise Refused(r.status_code, self.report_path)
        nxt = r.headers.get("x-next-row-number")
        return rows(r.json()), int(nxt) if nxt else None

    async def users(self) -> list[Json]:
        return rows(await self.json(f"{self.base}/workspace_users"))

    async def managers(self) -> list[Json]:
        """The workspace's project memberships (`manager` marks a project's managers)."""
        return rows(await self.json(f"{self.base}/project_users"))
