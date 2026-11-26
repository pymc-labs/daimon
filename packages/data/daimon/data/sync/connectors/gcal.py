"""Google Calendar, read live as the person asking: nothing is stored.

Each search reads the asker's own primary calendar, acting as them through the domain's
delegation (the same service account as Drive's), so Google shows an event only to the people it
lists on it. When a search names several readers, an event shows only if every one of their own
calendars has it. A reader the source cannot act as (not linked, suspended) gets nothing, and
nobody's calendar is read while a reader turned their `calendar` part off.

The delegation needs the scope `https://www.googleapis.com/auth/calendar.readonly` besides the
directory's. See `docs/data-connectors.md`.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import ClassVar, Final

import httpx
from daimon.data.personal import Part
from daimon.data.sync.connector import Connector, Field, Json, LiveHit, Source
from daimon.data.sync.connectors.chat import obj, rows
from daimon.data.sync.connectors.google import ApiError, DelegationRefused, connect, person_email
from daimon.data.sync.http import Upstream

PROVIDER: Final = "google"
EVENTS: Final = "/calendar/v3/calendars/primary/events"
WINDOW: Final = timedelta(days=90)
"""How far back and ahead of now a search looks; the events nearest to now are kept."""
FETCH: Final = 250
"""Events one person's search reads from the window, to rank by distance from now."""
TEXT_MAX: Final = 4_000
REF: Final = re.compile(r"[\w.~#@-]+")


def when(t: object) -> datetime | None:
    raw = obj(t).get("dateTime") or obj(t).get("date")
    if not raw:
        return None
    try:
        d = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def hit(e: Json) -> LiveHit | None:
    ref = str(e.get("id", ""))
    if e.get("status") == "cancelled" or not REF.fullmatch(ref):
        return None
    with_ = ", ".join(
        str(a.get("displayName") or a.get("email", "")) for a in rows(e.get("attendees"))
    )
    text = "\n".join(
        x
        for x in (e.get("description"), e.get("location"), f"With {with_}" if with_ else None)
        if x
    )
    return LiveHit(
        ref,
        str(e.get("summary") or "Busy"),
        text[:TEXT_MAX],
        "event",
        str(e["htmlLink"]) if e.get("htmlLink") else None,
        None,
        when(e.get("start")),
    )


class GCalConnector(Connector):
    kind: ClassVar[str] = "gcal"
    name: ClassVar[str] = "Google Calendar"
    category: ClassVar[str] = "meetings"
    live: ClassVar[bool] = True
    live_as_reader: ClassVar[bool] = True
    personal_part: ClassVar[Part | None] = "calendar"
    fields: ClassVar[tuple[Field, ...]] = (
        Field("service_account_key", "Service account key (JSON)", "secret", required=False),
        Field("domain", "Domain", placeholder="example.com"),
        Field("admin", "Admin to read the directory as", placeholder="admin@example.com"),
        Field(
            "service_account",
            "Service account (without a key)",
            required=False,
            placeholder="reader@project.iam.gserviceaccount.com",
        ),
    )

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        """`transport` replaces the guarded one (tests)."""
        self.transport = transport

    def workspace_of(self, source: Source) -> str:
        return self._domain(source)[0]

    @staticmethod
    def _domain(source: Source) -> tuple[str, str]:
        domain = str(source.config.get("domain") or "").strip().lower()
        admin = str(source.config.get("admin") or "").strip().lower()
        if not domain or not admin.endswith("@" + domain):
            raise ValueError(
                "config.domain and config.admin (an admin of that domain) are required"
            )
        return domain, admin

    async def test(self, source: Source) -> tuple[bool, str]:
        try:
            _, admin = self._domain(source)
            g = connect(source, self.transport)
        except ValueError as e:
            return False, str(e)
        try:
            await g.get(admin, "calendar", "/calendar/v3/calendars/primary")
        except ApiError as e:
            if e.status != 404:  # no calendar: Google still let the source act as the admin
                return False, str(e)
        except Upstream as e:
            return False, str(e)
        finally:
            await g.aclose()
        return True, "ok"

    async def live_search_as(
        self, source: Source, query: str, limit: int, subjects: Sequence[str]
    ) -> list[LiveHit]:
        _, admin = self._domain(source)
        g = connect(source, self.transport)
        try:
            now = datetime.now(UTC)
            found: dict[str, LiveHit] = {}
            for i in subjects:
                email = await person_email(g, admin, i)
                if email is None:
                    continue
                try:
                    body = await g.get(
                        email,
                        "calendar",
                        EVENTS,
                        {
                            "q": query,
                            "maxResults": FETCH,
                            "singleEvents": "true",
                            "orderBy": "startTime",
                            "timeMin": (now - WINDOW).isoformat(),
                            "timeMax": (now + WINDOW).isoformat(),
                        },
                    )
                except DelegationRefused:
                    continue
                except ApiError as e:
                    if e.status in (403, 404):
                        continue
                    raise
                for e in rows(body.get("items")):
                    if (h := hit(e)) and h.ref not in found:
                        found[h.ref] = h
            nearest = sorted(found.values(), key=lambda h: abs((h.occurred_at or now) - now))
            return nearest[:limit]
        finally:
            await g.aclose()
