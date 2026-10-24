"""Bounded Gmail/HubSpot/Discord transports for the native intake coordinator."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta
from typing import Any, Literal, cast, overload
from urllib.parse import quote

import httpx
from daimon.core.lead_intake import (
    IntakeConfig,
    LeadDealCandidate,
    LeadProgress,
    LeadSource,
    get_relevant_deals,
    select_matching_opportunity,
)
from daimon.core.lead_intake_service import EffectRequest, ReviewRequired, VerifiedSource
from daimon.core.lead_source import decode_email, has_registration_request


class HttpIntakePorts:
    """Credentials are coordinator-owned, never mounted in an agent vault.

    No automatic HTTP retries: every create has a committed durable intent and
    uncertain responses return to the coordinator's recovery path.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        gmail_token: str,
        hubspot_token: str,
        discord_token: str,
        gmail_refresh_credentials: dict[str, str] | None = None,
    ) -> None:
        self.client = client
        self.refresh_credentials = gmail_refresh_credentials
        self.gmail_refreshed = False
        self.tokens = {
            "gmail": gmail_token,
            "hubspot": hubspot_token,
            "discord": f"Bot {discord_token}",
        }
        self.hosts = {
            "gmail": "https://gmail.googleapis.com/gmail/v1/users/me/",
            "hubspot": "https://api.hubapi.com/",
            "discord": "https://discord.com/api/v10/",
        }

    @overload
    async def request(
        self,
        provider: str,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        missing: Literal[False] = False,
    ) -> dict[str, Any]: ...

    @overload
    async def request(
        self,
        provider: str,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        *,
        missing: Literal[True],
    ) -> dict[str, Any] | None: ...

    async def request(
        self,
        provider: str,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        missing: bool = False,
    ) -> dict[str, Any] | None:
        if provider == "gmail" and self.refresh_credentials and not self.gmail_refreshed:
            response = await self.client.post(
                "https://oauth2.googleapis.com/token",
                data={"grant_type": "refresh_token", **self.refresh_credentials},
            )
            response.raise_for_status()
            self.tokens["gmail"] = str(response.json()["access_token"])
            self.gmail_refreshed = True
        response = await self.client.request(
            method,
            self.hosts[provider] + path,
            headers={
                "Authorization": ("" if provider == "discord" else "Bearer ")
                + self.tokens[provider]
            },
            json=payload,
            params=params,
        )
        if missing and response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json() if response.content else {}

    async def source(
        self, config: IntakeConfig, source: LeadSource, actor_id: str | None
    ) -> VerifiedSource:
        if source.kind == "gmail":
            profile = await self.request("gmail", "GET", "profile")
            if (
                str(profile["emailAddress"]).strip().casefold()
                != (source.mailbox or "").strip().casefold()
            ):
                raise ValueError("mailbox_mismatch")
            message = await self.request(
                "gmail", "GET", f"messages/{source.message_id}", params={"format": "raw"}
            )
            if message["id"] != source.message_id:
                raise ValueError("message_mismatch")
            if int(message["internalDate"]) < int(config.reconciliation_start.timestamp() * 1000):
                raise ValueError("source_before_activation")
            if str(profile["emailAddress"]).strip().casefold() != config.gmail_mailbox.casefold():
                raise ValueError("configured_mailbox_mismatch")
            decoded = decode_email(str(message["raw"]))
            # Retain a reconstructed MIME without any attachment payloads.
            raw = base64.urlsafe_b64encode(decoded.text.encode()).decode()
            return VerifiedSource(
                source=source, text=decoded.text, raw=raw, recipients=tuple(decoded.recipients)
            )
        channel = await self.request("discord", "GET", f"channels/{source.channel_id}")
        if str(channel["id"]) != source.channel_id:
            raise ValueError("source_channel_mismatch")
        if channel.get("guild_id") is None and channel.get("parent_id"):
            channel = await self.request("discord", "GET", f"channels/{channel['parent_id']}")
        if str(channel.get("guild_id")) != config.guild_id:
            raise ValueError("source_guild_mismatch")
        message = await self.request(
            "discord", "GET", f"channels/{source.channel_id}/messages/{source.message_id}"
        )
        if (
            str(message["id"]) != source.message_id
            or str(message["channel_id"]) != source.channel_id
            or str(message["author"]["id"]) != actor_id
            or message["author"].get("bot", False)
            or not has_registration_request(str(message["content"]))
        ):
            raise ValueError("explicit_author_registration_required")
        return VerifiedSource(source=source, text=str(message["content"]))

    async def _require_mailbox(self, config: IntakeConfig, error: type[Exception]) -> None:
        """The Gmail token must still belong to the configured mailbox."""
        profile = await self.request("gmail", "GET", "profile")
        if str(profile["emailAddress"]).casefold() != config.gmail_mailbox.casefold():
            raise error("configured_mailbox_mismatch")

    async def scan(
        self, config: IntakeConfig, start: datetime, end: datetime, cursor: str | None
    ) -> tuple[list[str], str | None]:
        await self._require_mailbox(config, ValueError)
        params = {
            "q": f"in:anywhere after:{int(start.timestamp()) - 1} before:{int(end.timestamp())}",
            "includeSpamTrash": "true",
            "maxResults": "100",
        }
        if cursor:
            params["pageToken"] = cursor
        page = await self.request("gmail", "GET", "messages", params=params)
        return [str(item["id"]) for item in page.get("messages", [])], page.get("nextPageToken")

    async def _crm_check(self, config: IntakeConfig) -> None:
        portal = await self.request("hubspot", "GET", "account-info/v3/details")
        if str(portal["portalId"]) != config.portal_id:
            raise ReviewRequired("portal_mismatch")
        for kind, name in (
            ("companies", "daimon_company_identity"),
            ("deals", "daimon_intake_identity"),
        ):
            prop = await self.request("hubspot", "GET", f"crm/v3/properties/{kind}/{name}")
            if prop.get("hasUniqueValue") is not True:
                raise ReviewRequired("unique_schema_required")

    async def _objects(self, kind: str, properties: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        cursor = ""
        seen: set[str] = set()
        while True:
            page = await self.request(
                "hubspot",
                "GET",
                f"crm/v3/objects/{kind}",
                params={
                    "limit": "100",
                    "properties": properties,
                    **({"after": cursor} if cursor else {}),
                },
            )
            result.extend(cast("list[dict[str, Any]]", page["results"]))
            cursor = str(page.get("paging", {}).get("next", {}).get("after", ""))
            if not cursor:
                return result
            if cursor in seen or len(result) > 100_000:
                raise ValueError("crm_pagination_limit")
            seen.add(cursor)

    async def _associations(self, kind: str, identity: str, target: str) -> set[str]:
        found: set[str] = set()
        after = ""
        seen: set[str] = set()
        while True:
            page = await self.request(
                "hubspot",
                "GET",
                f"crm/v4/objects/{kind}/{identity}/associations/{target}",
                params={"limit": "500", **({"after": after} if after else {})},
            )
            found.update(str(row["toObjectId"]) for row in page.get("results", []))
            after = str(page.get("paging", {}).get("next", {}).get("after", ""))
            if not after:
                return found
            if after in seen:
                raise ValueError("association_cursor_repeated")
            seen.add(after)

    async def _messages(self, channel: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        before = ""
        for _ in range(100):
            page = cast(
                "list[dict[str, Any]]",
                await self.request(
                    "discord",
                    "GET",
                    f"channels/{channel}/messages",
                    params={"limit": "100", **({"before": before} if before else {})},
                ),
            )
            result.extend(page)
            if len(page) < 100:
                return result
            before = str(page[-1]["id"])
        raise ReviewRequired("discord_recovery_scan_incomplete")

    async def recover(self, config: IntakeConfig, effect: EffectRequest) -> str | None:
        step, p = effect.step, effect.payload
        if step == "starter_resolution":
            bot = await self.request("discord", "GET", "users/@me")
            matches = [
                m
                for m in await self._messages(config.channel_id)
                if str(m["author"]["id"]) == str(bot["id"])
                and str(m.get("content", "")) == p["content"]
                and str(m.get("timestamp", "")) >= p["accepted_at"]
            ]
            if p["selected"]:
                return (
                    p["selected"] if any(str(m["id"]) == p["selected"] for m in matches) else None
                )
            return "absent" if not matches else None
        if step == "thread":
            channel = await self.request("discord", "GET", f"channels/{p['starter']}", missing=True)
            if channel is not None:
                if str(channel.get("parent_id")) != config.channel_id:
                    raise ReviewRequired("thread_parent_mismatch")
                return str(channel["id"])
            return None
        if step == "starter" or step.startswith("dossier:"):
            if p.get("_uncertain") != "True":
                return None
            channel_id = config.channel_id if step == "starter" else p["thread"]
            bot = await self.request("discord", "GET", "users/@me")
            posted = [
                m
                for m in await self._messages(channel_id)
                if str(m["author"]["id"]) == str(bot["id"])
            ]
            matches = [m for m in posted if str(m.get("nonce", "")) == effect.key[:24]]
            if len(matches) > 1:
                raise ReviewRequired("duplicate_discord_effect")
            if matches:
                return str(matches[0]["id"])
            if step == "starter":
                if p.get("_uncertain_peer") == "True":
                    raise ReviewRequired("starter_candidates_require_human")
                since = datetime.fromisoformat(p["_accepted_at"]) - timedelta(seconds=60)
                claimed = cast("list[str]", json.loads(p.get("_claimed", "[]")))
                matches = [
                    m
                    for m in posted
                    if str(m.get("content", "")).strip() == p["content"].strip()
                    and m.get("timestamp")
                    and datetime.fromisoformat(str(m["timestamp"])) >= since
                    and str(m["id"]) not in claimed
                ]
                if len(matches) == 1:
                    return str(matches[0]["id"])
                raise ReviewRequired("uncertain_write")
            prefix = cast("list[str]", json.loads(p["_prefix"]))
            ordered = sorted(
                [m for m in posted if m.get("type", 0) == 0], key=lambda m: int(m["id"])
            )
            visible = [str(m.get("content", "")) for m in ordered]
            if visible[: len(prefix)] == prefix:
                return str(ordered[len(prefix) - 1]["id"])
            if visible == prefix[:-1]:
                p["_safe_dossier_absence"] = "True"
                return None
            raise ReviewRequired("dossier_history_review")
        if step == "archive":
            await self._require_mailbox(config, ReviewRequired)
            message = await self.request(
                "gmail", "GET", f"messages/{p['message']}", params={"format": "minimal"}
            )
            return p["message"] if "INBOX" not in message.get("labelIds", []) else None
        await self._crm_check(config)
        if step.startswith("association:"):
            kind = step.split(":")[1]
            return (
                p["target"]
                if p["target"] in await self._associations("deals", p["deal"], kind)
                else None
            )
        kind = {"company": "companies", "contact": "contacts", "deal": "deals"}[step]
        prop = {
            "company": "daimon_company_identity",
            "contact": "email",
            "deal": "daimon_intake_identity",
        }[step]
        value = p["email"] if step == "contact" else effect.key
        selected = p.get("selected")
        if selected:
            approved = await self.request(
                "hubspot",
                "GET",
                f"crm/v3/objects/{kind}/{selected}",
                params={"properties": "hs_is_closed"},
                missing=True,
            )
            if (
                not approved
                or str(approved["id"]) != selected
                or approved.get("archived")
                or str(approved.get("properties", {}).get("hs_is_closed")).lower() == "true"
            ):
                raise ReviewRequired("selected_record_unavailable")
        record = await self.request(
            "hubspot",
            "GET",
            f"crm/v3/objects/{kind}/{quote(value, safe='')}",
            params={"idProperty": prop, "properties": "hs_is_closed"}
            if step == "deal"
            else {"idProperty": prop},
            missing=True,
        )
        if record and selected and str(record["id"]) != selected:
            raise ReviewRequired("approved_selection_conflicts_with_recovery")
        if selected:
            return selected
        if record:
            if record.get("archived") or (
                step == "deal"
                and str(record.get("properties", {}).get("hs_is_closed")).lower() == "true"
            ):
                raise ReviewRequired("closed_or_archived_crm_record")
            return str(record["id"])
        if step == "company":
            objects = await self._objects(kind, "name,domain,hs_additional_domains")
            matches = [
                r
                for r in objects
                if p["domain"]
                in {
                    str(r["properties"].get("domain") or "").casefold().rstrip("."),
                    *str(r["properties"].get("hs_additional_domains") or "").casefold().split(";"),
                }
            ]
            names = [
                r
                for r in objects
                if str(r["properties"].get("name", "")).casefold() == p["name"].casefold()
            ]
            if len(matches) > 1 or (not matches and names):
                raise ReviewRequired(
                    "company_ambiguous. Candidate companies: "
                    + ", ".join(
                        f"{r['properties'].get('name') or ''} ({r['id']})"
                        for r in (matches or names)
                    )
                )
            return str(matches[0]["id"]) if matches else None
        if step == "deal":
            objects = await self._objects(kind, "dealname,description,hs_is_closed")
            company = await self._associations("companies", p["company"], "deals")
            contact = await self._associations("contacts", p["contact"], "deals")
            candidates = get_relevant_deals(
                [
                    LeadDealCandidate(
                        id=str(r["id"]),
                        name=str(r["properties"].get("dealname") or ""),
                        description=str(r["properties"].get("description") or ""),
                        is_closed=str(r["properties"].get("hs_is_closed")).lower() == "true",
                        company_ids=[p["company"]] if str(r["id"]) in company else [],
                        contact_ids=[p["contact"]] if str(r["id"]) in contact else [],
                    )
                    for r in objects
                ],
                company_id=p["company"],
                contact_id=p["contact"],
                company_name=p["company_name"],
                company_domain=p["domain"],
                opportunity_description=p["description"],
            )
            if p["separate"] != "True" and any(
                not c.company_ids and not c.contact_ids for c in candidates
            ):
                raise ReviewRequired(
                    "orphan_opportunity_review. Candidate deals: "
                    + ", ".join(f"{c.name} ({c.id})" for c in candidates)
                )
            match = select_matching_opportunity(
                candidates,
                p["description"],
                LeadProgress(separate_opportunity_confirmed=p["separate"] == "True"),
            )
            if match.requires_review:
                raise ReviewRequired(
                    "opportunity_ambiguous. Candidate deals: "
                    + ", ".join(f"{c.name} ({c.id})" for c in candidates)
                )
            return match.deal_id
        return None

    def replay_safe(self, effect: EffectRequest) -> bool:
        # Called only after recover validated portal/schema and read remote state.
        # CRM unique properties/contact email, one thread per starter, label sets
        # and association PUTs provide provider-enforced duplicate prevention.
        if effect.step.startswith("dossier:"):
            return effect.payload.get("_safe_dossier_absence") == "True"
        return effect.step in {
            "company",
            "contact",
            "deal",
            "thread",
            "archive",
        } or effect.step.startswith("association:")

    async def write(self, config: IntakeConfig, effect: EffectRequest) -> str:
        step, p = effect.step, effect.payload
        if step == "starter" or step.startswith("dossier:"):
            channel = config.channel_id if step == "starter" else p["thread"]
            # Discord's allowlist controls actual pings even for malicious source text.
            payload = dict(
                content=p["content"],
                nonce=effect.key[:24],
                enforce_nonce=True,
                allowed_mentions={
                    "parse": [],
                    "users": [] if step == "starter" else list(config.notifier_user_ids),
                },
            )
            response = await self.request(
                "discord", "POST", f"channels/{channel}/messages", payload
            )
            return str(response["id"])
        if step == "thread":
            response = await self.request(
                "discord",
                "POST",
                f"channels/{config.channel_id}/messages/{p['starter']}/threads",
                {"name": p["title"][:100], "auto_archive_duration": 10080},
            )
            return str(response["id"])
        if step == "archive":
            await self._require_mailbox(config, ReviewRequired)
            await self.request(
                "gmail", "POST", f"messages/{p['message']}/modify", {"removeLabelIds": ["INBOX"]}
            )
            return p["message"]
        await self._crm_check(config)
        if step.startswith("association:"):
            kind = step.split(":")[1]
            await self.request(
                "hubspot",
                "PUT",
                f"crm/v4/objects/deals/{p['deal']}/associations/default/{kind}/{p['target']}",
            )
            if p["target"] not in await self._associations("deals", p["deal"], kind):
                raise ValueError("association_readback_missing")
            return p["target"]
        kind = {"company": "companies", "contact": "contacts", "deal": "deals"}[step]
        if step == "company":
            properties = {
                "name": p["name"],
                "domain": p["domain"],
                "daimon_company_identity": effect.key,
            }
        elif step == "contact":
            properties = {"email": p["email"]}
        else:
            properties = {
                **config.deal_fields,
                "dealname": p["name"],
                "description": p["description"],
                "pipeline": config.pipeline,
                "dealstage": config.stage,
                "daimon_intake_identity": effect.key,
                "discord_lead_thread": p["thread_url"],
            }
        if step == "deal" and (value := p.get("attribution_value")):
            evidence = p.get("attribution_evidence")
            if (
                not evidence
                or evidence not in p["original_text"]
                or value.casefold() not in evidence.casefold()
            ):
                raise ReviewRequired(
                    "Attribution requires an explicit source quote supporting the value"
                )
            if not config.attribution_property:
                raise ReviewRequired("attribution_property_required")
            definition = await self.request(
                "hubspot",
                "GET",
                f"crm/v3/properties/deals/{quote(config.attribution_property, safe='')}",
                missing=True,
            )
            if not definition or value not in {
                option["value"]
                for option in definition.get("options", [])
                if not option.get("hidden")
            }:
                raise ReviewRequired(
                    "Attribution is not a valid HubSpot option; review the explicit evidence"
                )
            properties[config.attribution_property] = value
        response = await self.request(
            "hubspot", "POST", f"crm/v3/objects/{kind}", {"properties": properties}
        )
        # Read back the committed remote identity before declaring success.
        remote_id = str(response["id"])
        await self.request("hubspot", "GET", f"crm/v3/objects/{kind}/{remote_id}")
        return remote_id
