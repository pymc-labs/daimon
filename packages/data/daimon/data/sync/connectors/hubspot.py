"""HubSpot, read live: companies, deals, contacts and notes found at question time with the
source's own private app token, nothing stored. Read-only (searches and lookups), and
scope-gated: a caller whose scopes include the source's default scope reads everything the
token sees; there is no per-record access list. See `docs/data-connectors-apps.md`.

Credential `{"token": "..."}`: a private app token. Configuration: `objects` (the types to
search: companies, deals, contacts, notes; default all), `portal_id` (to link each hit).
"""

from __future__ import annotations

import asyncio
import html
import re
from datetime import UTC, datetime
from typing import ClassVar, Final

import httpx
from daimon.data.sync.connector import Connector, Field, Json, LiveHit, Source
from daimon.data.sync.connectors.chat import names, obj, rows
from daimon.data.sync.connectors.live_http import call, client_of
from daimon.data.sync.http import Upstream

API: Final = "https://api.hubapi.com"
HS: Final = {  # object type: (kind, label, properties, modified property, record type id)
    "companies": (
        "company",
        "Companies",
        ["name", "domain", "industry", "description"],
        "hs_lastmodifieddate",
        "0-2",
    ),
    "deals": (
        "deal",
        "Deals",
        ["dealname", "dealstage", "amount", "pipeline", "closedate"],
        "hs_lastmodifieddate",
        "0-3",
    ),
    "contacts": (
        "contact",
        "Contacts",
        ["firstname", "lastname", "email", "company", "jobtitle"],
        "lastmodifieddate",
        "0-1",
    ),
    "notes": ("note", "Notes", ["hs_note_body", "hs_timestamp"], "hs_lastmodifieddate", None),
}


def strip_html(s: object) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", str(s or ""))).strip()


def when(v: object) -> datetime | None:
    if not v:
        return None
    if re.fullmatch(r"\d+", str(v)):  # epoch milliseconds
        return datetime.fromtimestamp(int(str(v)) / 1000, UTC)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def hit(
    t: str, o: Json, company: tuple[str, str] | None, labels: dict[str, str], portal: str | None
) -> LiveHit:
    kind, label, _, mod, type_id = HS[t]
    p = obj(o.get("properties"))
    cid, cname = company or (None, None)
    if t == "companies":
        cid, cname = str(o["id"]), p.get("name")
    first = " ".join(str(x) for x in (p.get("firstname"), p.get("lastname")) if x)
    title = {
        "companies": p.get("name"),
        "deals": p.get("dealname"),
        "contacts": first or p.get("email"),
        "notes": strip_html(p.get("hs_note_body"))[:80],
    }[t] or f"{label[:-1]} {o['id']}"
    facts = {
        "company": cname,
        "stage": labels.get(f"stage:{p.get('dealstage')}", p.get("dealstage")),
        "amount": p.get("amount"),
        "email": p.get("email"),
        "job title": p.get("jobtitle"),
        "domain": p.get("domain"),
        "industry": p.get("industry"),
    }
    text = (
        strip_html(p.get("hs_note_body"))
        if t == "notes"
        else ". ".join(f"{k.capitalize()}: {v}" for k, v in facts.items() if v)
        + (f"\n\n{p['description']}" if p.get("description") else "")
    )
    rec = (type_id, o["id"]) if type_id else ("0-2", cid) if cid else None
    url = (
        f"https://app.hubspot.com/contacts/{portal}/record/{rec[0]}/{rec[1]}"
        if portal and rec
        else None
    )
    return LiveHit(
        f"{t}~{o['id']}", str(title), text, kind, url, None, when(p.get(mod) or o.get("updatedAt"))
    )


class HubSpotConnector(Connector):
    kind: ClassVar[str] = "hubspot"
    name: ClassVar[str] = "HubSpot"
    category: ClassVar[str] = "crm"
    live: ClassVar[bool] = True
    fields: ClassVar[tuple[Field, ...]] = (
        Field("token", "Private app token", "secret"),
        Field("portal_id", "Portal ID", required=False),
        Field("objects", "Objects", "list", required=False, placeholder="companies, deals"),
    )

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        """`transport` replaces the guarded one (tests)."""
        self.transport = transport

    @staticmethod
    def _types(source: Source) -> list[str]:
        want = [t.lower() for t in names(source.config, "objects")]
        if bad := [t for t in want if t not in HS]:
            raise ValueError(f"config.objects: {', '.join(bad)} is not one of {', '.join(HS)}")
        return [t for t in HS if not want or t in want]

    async def test(self, source: Source) -> tuple[bool, str]:
        try:
            self._types(source)
            async with client_of(source, API, self.transport) as client:
                j = await call(client, source, "HubSpot", "/account-info/v3/details")
        except (ValueError, Upstream, httpx.HTTPError) as e:
            return False, str(e)
        return True, f"portal {j.get('portalId')}"

    @staticmethod
    async def _companies(
        client: httpx.AsyncClient, source: Source, t: str, ids: list[str]
    ) -> dict[str, tuple[str, str]]:
        """Object id to (company id, company name), through its first associated company."""
        if not ids or t == "companies":
            return {}
        j = await call(
            client,
            source,
            "HubSpot",
            f"/crm/v4/associations/{t}/companies/batch/read",
            {"inputs": [{"id": i} for i in ids]},
        )
        first = {
            str(obj(r.get("from"))["id"]): str(rows(r.get("to"))[0]["toObjectId"])
            for r in rows(j.get("results"))
            if rows(r.get("to"))
        }
        if not first:
            return {}
        j = await call(
            client,
            source,
            "HubSpot",
            "/crm/v3/objects/companies/batch/read",
            {"properties": ["name"], "inputs": [{"id": c} for c in set(first.values())]},
        )
        found = {
            str(r["id"]): str(obj(r.get("properties")).get("name") or "")
            for r in rows(j.get("results"))
        }
        return {o: (c, found.get(c, "")) for o, c in first.items()}

    @staticmethod
    async def _search(
        client: httpx.AsyncClient, source: Source, t: str, query: str, limit: int
    ) -> list[Json]:
        _, _, props, mod, _ = HS[t]
        body: Json = {"limit": limit, "properties": [*props, mod]}
        if t == "notes":  # notes have no default search properties: match a token in the body
            word = max(re.findall(r"\w+", query) or [query], key=len)
            body["filterGroups"] = [
                {
                    "filters": [
                        {
                            "propertyName": "hs_note_body",
                            "operator": "CONTAINS_TOKEN",
                            "value": f"*{word}*",
                        }
                    ]
                }
            ]
        else:
            body["query"] = query
        return rows(
            (await call(client, source, "HubSpot", f"/crm/v3/objects/{t}/search", body)).get(
                "results"
            )
        )

    async def live_search(self, source: Source, query: str, limit: int) -> list[LiveHit]:
        types = self._types(source)
        async with client_of(source, API, self.transport) as client:
            found = await asyncio.gather(
                *(self._search(client, source, t, query, limit) for t in types)
            )
            labels: dict[str, str] = {}
            if "deals" in types:
                for p in rows(
                    (await call(client, source, "HubSpot", "/crm/v3/pipelines/deals")).get(
                        "results"
                    )
                ):
                    labels[str(p["id"])] = str(p["label"])
                    labels |= {f"stage:{s['id']}": str(s["label"]) for s in rows(p.get("stages"))}
            assoc = await asyncio.gather(
                *(
                    self._companies(client, source, t, [str(o["id"]) for o in objs])
                    for t, objs in zip(types, found, strict=True)
                )
            )
        portal = str(source.config.get("portal_id") or "") or None
        per = [
            [hit(t, o, a.get(str(o["id"])), labels, portal) for o in objs]
            for t, objs, a in zip(types, found, assoc, strict=True)
        ]
        out = [x for rank in range(limit) for lst in per if rank < len(lst) for x in [lst[rank]]]
        return out[:limit]
