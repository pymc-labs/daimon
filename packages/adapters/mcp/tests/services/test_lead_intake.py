"""Real intake HTTP transports replay synthetic Gmail, HubSpot and Discord APIs."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
from daimon.adapters.mcp.services.lead_intake import HttpIntakePorts
from daimon.core.github_credentials import build_multifernet
from daimon.core.lead_intake import (
    IntakeConfig,
    IntakeResolution,
    LeadClassification,
    LeadDraft,
    LeadSource,
    LeadSourceKind,
)
from daimon.core.lead_intake_service import (
    EffectRequest,
    IntakeAccess,
    IntakeService,
    ReviewRequired,
)
from daimon.core.stores.lead_intake import IntakeStore
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

MAIL = "From: Ada <ada@synthetic.example>\nTo: intake@example.test\n\nPlease scope a demand forecasting project.\nAda, Synthetic synthetic.example"


def config() -> IntakeConfig:
    return IntakeConfig(
        agent_id=uuid4(),
        mode="active",
        guild_id="1",
        channel_id="2",
        gmail_mailbox="owner@example.test",
        source_connection_id="gmail-1",
        crm_connection_id="crm-1",
        portal_id="3",
        recipients=("intake@example.test",),
        notifier_user_ids=("7",),
        reconciliation_start=datetime.now(UTC) - timedelta(days=1),
    )


class ProviderReplay:
    def __init__(self) -> None:
        self.messages: dict[str, list[dict[str, Any]]] = {"2": []}
        self.objects: dict[str, list[dict[str, Any]]] = {
            "companies": [],
            "contacts": [],
            "deals": [],
        }
        self.associations: dict[tuple[str, str], set[str]] = {}
        self.thread = False
        self.archived = False
        self.events: list[str] = []
        self.lose_response = ""
        self.next_id = 100
        self.omit_nonce = False
        self.company_candidates: list[dict[str, Any]] = []
        self.portal = "3"
        self.unique = True
        self.attribution_options: list[dict[str, Any]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        payload: dict[str, Any] = json.loads(request.content) if request.content else {}
        if request.url.host == "gmail.googleapis.com":
            if path.endswith("profile"):
                return httpx.Response(200, json={"emailAddress": "owner@example.test"})
            if path.endswith("modify"):
                assert self.messages.get("100"), "Archive must follow complete dossier"
                self.archived = True
                self.events.append("archive")
                return httpx.Response(200, json={"id": "immutable-1"})
            return httpx.Response(
                200,
                json={
                    "id": "immutable-1",
                    "raw": base64.urlsafe_b64encode(MAIL.encode()).decode(),
                    "internalDate": str(int(datetime.now(UTC).timestamp() * 1000)),
                    "labelIds": [] if self.archived else ["INBOX"],
                },
            )
        if request.url.host == "discord.com":
            if path.endswith("users/@me"):
                return httpx.Response(200, json={"id": "99"})
            if path.endswith("/threads"):
                self.thread = True
                self.events.append("thread")
                self.messages["100"] = []
                return httpx.Response(200, json={"id": "100", "parent_id": "2"})
            if path == "/api/v10/channels/100":
                return httpx.Response(
                    200 if self.thread else 404, json={"id": "100", "parent_id": "2"}
                )
            channel = path.split("/")[-2]
            if method == "GET":
                return httpx.Response(
                    200,
                    json=[
                        {k: v for k, v in m.items() if k != "nonce" or not self.omit_nonce}
                        for m in self.messages.get(channel, [])
                    ],
                )
            assert payload["allowed_mentions"]["parse"] == []
            assert payload["allowed_mentions"]["users"] == ([] if channel == "2" else ["7"])
            if channel == "2":
                assert payload["content"] == "Synthetic", "Starter contains the bare title only"
            self.next_id += 1
            row = {
                "id": "100" if channel == "2" else str(self.next_id),
                "author": {"id": "99"},
                "timestamp": datetime.now(UTC).isoformat(),
                **payload,
            }
            effect = "starter" if channel == "2" else "dossier"
            if self.lose_response == f"before_{effect}":
                self.lose_response = ""
                raise httpx.ReadTimeout("synthetic pre-post failure", request=request)
            self.messages.setdefault(channel, []).append(row)
            self.events.append(effect)
            if self.lose_response == effect:
                self.lose_response = ""
                raise httpx.ReadTimeout("synthetic response loss", request=request)
            return httpx.Response(200, json=row)
        if path.endswith("account-info/v3/details"):
            return httpx.Response(200, json={"portalId": self.portal})
        if path.endswith("how_they_heard_about_pymc_labs"):
            return httpx.Response(200, json={"options": self.attribution_options})
        if "properties" in path:
            return httpx.Response(200, json={"hasUniqueValue": self.unique})
        parts = path.split("/")
        if "associations" in parts:
            if method == "PUT":
                self.associations.setdefault((parts[5], parts[-2]), set()).add(parts[-1])
                self.events.append("association")
                return httpx.Response(200, json={})
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"toObjectId": x}
                        for x in self.associations.get((parts[5], parts[-1]), set())
                    ]
                },
            )
        kind = parts[4]
        if method == "POST":
            assert self.archived, "CRM follows delivery and archive"
            if self.lose_response == f"before_{kind}":
                self.lose_response = ""
                raise httpx.ReadTimeout("synthetic pre-create failure", request=request)
            self.next_id += 1
            record = {
                "id": str(self.next_id),
                "properties": payload["properties"],
                "archived": False,
            }
            self.objects[kind].append(record)
            self.events.append(kind)
            if self.lose_response == kind:
                self.lose_response = ""
                raise httpx.ReadTimeout("synthetic response loss", request=request)
            return httpx.Response(200, json=record)
        if len(parts) == 5:
            return httpx.Response(
                200,
                json={
                    "results": self.objects[kind]
                    + (self.company_candidates if kind == "companies" else [])
                },
            )
        value, prop = parts[5], request.url.params.get("idProperty")
        for row in self.objects[kind]:
            if (row["properties"].get(prop) if prop else row["id"]) == value:
                return httpx.Response(200, json=row)
        return httpx.Response(404, json={})


async def readable(target: object) -> None:
    pass


async def noop(access: IntakeAccess) -> None:
    pass


@pytest.mark.parametrize(
    "lost",
    [
        "",
        "starter",
        "dossier",
        "companies",
        "contacts",
        "deals",
        "before_companies",
        "starter_no_nonce",
        "dossier_no_nonce",
        "before_dossier",
    ],
)
async def test_full_active_http_replay_without_duplicate_effects(
    db_nullpool_engine: AsyncEngine, db_clean: None, lost: str
) -> None:
    sessions = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        tenant = await make_tenant(session)
    store = IntakeStore(sessions, build_multifernet((Fernet.generate_key().decode(),)), tenant.id)
    settings = config()
    await store.configure(settings, {"hubspot_access_token": "synthetic"})
    replay = ProviderReplay()
    replay.omit_nonce = lost.endswith("_no_nonce")
    replay.lose_response = lost.removesuffix("_no_nonce")
    async with httpx.AsyncClient(transport=httpx.MockTransport(replay.handle)) as client:
        ports = HttpIntakePorts(
            client, gmail_token="synthetic", hubspot_token="synthetic", discord_token="synthetic"
        )
        intake = IntakeService(store, ports, noop, authorize_read=readable)
        draft = LeadDraft(
            source=LeadSource(
                kind=LeadSourceKind.GMAIL, mailbox="owner@example.test", message_id="immutable-1"
            ),
            classification=LeadClassification.PROJECT,
            opportunity_description="Demand forecasting",
            thread_title="Synthetic",
            thread_content="Research summary\nDRAFT: Thank you. <@7> <@&42> @everyone",
            enquirer_email="ada@synthetic.example",
            company_name="Synthetic",
            company_domain="synthetic.example",
            enquirer_evidence="From: Ada <ada@synthetic.example>",
            company_evidence="Ada, Synthetic synthetic.example",
        )
        first = await intake.submit(draft, "7")
        result = await intake.resume(first.id)
        assert result.status == "completed", result.error_code
        again = await intake.submit(draft, "7")
        assert again.id == result.id
        assert len(replay.messages["2"]) == 1
        assert len(replay.messages["100"]) == 1
        assert {kind: len(rows) for kind, rows in replay.objects.items()} == {
            "companies": 1,
            "contacts": 1,
            "deals": 1,
        }
        assert (
            replay.events.index("dossier")
            < replay.events.index("archive")
            < replay.events.index("companies")
        )
        assert "send_email" not in replay.events


@pytest.mark.parametrize(
    "portal,unique,reason",
    [("wrong", True, "portal_mismatch"), ("3", False, "unique_schema_required")],
)
async def test_crm_portal_and_schema_fail_closed(portal: str, unique: bool, reason: str) -> None:
    replay = ProviderReplay()
    replay.portal, replay.unique = portal, unique
    async with httpx.AsyncClient(transport=httpx.MockTransport(replay.handle)) as client:
        ports = HttpIntakePorts(
            client, gmail_token="synthetic", hubspot_token="synthetic", discord_token="synthetic"
        )
        with pytest.raises(ReviewRequired, match=reason):
            await ports.recover(
                config(),
                EffectRequest(
                    step="company",
                    key="stable",
                    payload={"domain": "synthetic.example", "name": "Synthetic"},
                ),
            )
    assert replay.events == []


@pytest.mark.parametrize(
    "author,bot,content,guild,accepted",
    [
        ("7", False, "Please register this lead: Synthetic", "1", True),
        ("8", False, "Please register this lead: Synthetic", "1", False),
        ("7", True, "Please register this lead: Synthetic", "1", False),
        ("7", False, "Ordinary project chatter", "1", False),
        ("7", False, "Please register this lead: Synthetic", "other", False),
    ],
)
async def test_manual_source_is_verified_from_provider(
    author: str, bot: bool, content: str, guild: str, accepted: bool
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET", "Source verification has no provider effects"
        if "/messages/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "id": "600",
                    "channel_id": "500",
                    "author": {"id": author, "bot": bot},
                    "content": content,
                },
            )
        return httpx.Response(200, json={"id": "500", "guild_id": guild})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        ports = HttpIntakePorts(
            client, gmail_token="synthetic", hubspot_token="synthetic", discord_token="synthetic"
        )
        source = LeadSource(kind=LeadSourceKind.MANUAL, channel_id="500", message_id="600")
        if accepted:
            assert (await ports.source(config(), source, "7")).text == content
        else:
            with pytest.raises(ValueError):
                await ports.source(config(), source, "7")


async def test_manual_thread_source_verifies_parent_guild() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if "/messages/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "id": "600",
                    "channel_id": "502",
                    "author": {"id": "7"},
                    "content": "Register this lead: Synthetic",
                },
            )
        if request.url.path.endswith("502"):
            return httpx.Response(200, json={"id": "502", "parent_id": "500"})
        return httpx.Response(200, json={"id": "500", "guild_id": "1"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        ports = HttpIntakePorts(
            client, gmail_token="synthetic", hubspot_token="synthetic", discord_token="synthetic"
        )
        result = await ports.source(
            config(),
            LeadSource(kind=LeadSourceKind.MANUAL, channel_id="502", message_id="600"),
            "7",
        )
    assert result.text == "Register this lead: Synthetic"


@pytest.mark.parametrize(
    "peer,claimed,count", [(True, False, 1), (False, True, 1), (False, False, 2)]
)
async def test_nonce_less_starter_ambiguity_never_reposts(
    peer: bool, claimed: bool, count: int
) -> None:
    replay = ProviderReplay()
    replay.messages["2"] = [
        {
            "id": str(100 + i),
            "author": {"id": "99"},
            "content": "Synthetic",
            "timestamp": datetime.now(UTC).isoformat(),
        }
        for i in range(count)
    ]
    payload = {
        "content": "Synthetic",
        "_uncertain": "True",
        "_uncertain_peer": str(peer),
        "_accepted_at": datetime.now(UTC).isoformat(),
        "_claimed": json.dumps(["100"] if claimed else []),
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(replay.handle)) as client:
        ports = HttpIntakePorts(
            client, gmail_token="synthetic", hubspot_token="synthetic", discord_token="synthetic"
        )
        with pytest.raises(ReviewRequired):
            await ports.recover(
                config(), EffectRequest(step="starter", key="stable", payload=payload)
            )
    assert replay.events == []


@pytest.mark.parametrize(
    "step,kind,prop",
    [
        ("company", "companies", "daimon_company_identity"),
        ("contact", "contacts", "email"),
        ("deal", "deals", "daimon_intake_identity"),
    ],
)
@pytest.mark.parametrize("conflict", [False, True])
async def test_approved_crm_selection_cannot_be_replaced_by_recovery(
    step: str, kind: str, prop: str, conflict: bool
) -> None:
    replay = ProviderReplay()
    replay.objects[kind] = [{"id": "201", "properties": {}, "archived": False}]
    if conflict:
        replay.objects[kind].append(
            {"id": "999", "properties": {prop: "stable"}, "archived": False}
        )
    async with httpx.AsyncClient(transport=httpx.MockTransport(replay.handle)) as client:
        ports = HttpIntakePorts(
            client, gmail_token="synthetic", hubspot_token="synthetic", discord_token="synthetic"
        )
        effect = EffectRequest(
            step=step, key="stable", payload={"selected": "201", "email": "stable"}
        )
        if conflict:
            with pytest.raises(ReviewRequired, match="approved_selection_conflicts"):
                await ports.recover(config(), effect)
        else:
            assert await ports.recover(config(), effect) == "201"
    assert replay.events == [], "Conflicting recovery cannot create or associate any record"


@pytest.mark.parametrize(
    "case",
    [
        "valid",
        "invalid",
        "hidden",
        "missing_quote",
        "unsupported",
        "non_verbatim",
        "disabled",
        "identity_review",
    ],
)
async def test_deal_preserves_only_source_supported_allowed_attribution(
    db_nullpool_engine: AsyncEngine, db_clean: None, case: str
) -> None:
    sessions = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        tenant = await make_tenant(session)
    store = IntakeStore(sessions, build_multifernet((Fernet.generate_key().decode(),)), tenant.id)
    settings = config().model_copy(
        update={
            "deal_fields": {"dealtype": "newbusiness", "channel": "Cold in-mail"},
            "attribution_property": "" if case == "disabled" else "how_they_heard_about_pymc_labs",
        }
    )
    await store.configure(settings, {"hubspot_access_token": "synthetic"})
    replay = ProviderReplay()
    replay.attribution_options = [{"value": "forecasting", "hidden": case == "hidden"}]
    async with httpx.AsyncClient(transport=httpx.MockTransport(replay.handle)) as client:
        ports = HttpIntakePorts(
            client, gmail_token="synthetic", hubspot_token="synthetic", discord_token="synthetic"
        )
        intake = IntakeService(store, ports, noop, authorize_read=readable)
        proposal = LeadDraft(
            source=LeadSource(
                kind=LeadSourceKind.GMAIL, mailbox="owner@example.test", message_id="immutable-1"
            ),
            classification=LeadClassification.PROJECT,
            opportunity_description="Demand forecasting",
            thread_title="Synthetic",
            thread_content="DRAFT: Thank you",
            enquirer_email="ada@synthetic.example",
            company_name="Synthetic",
            company_domain="synthetic.example",
            enquirer_evidence="From: Ada <ada@synthetic.example>",
            company_evidence=(
                "invented company quote"
                if case == "identity_review"
                else "Ada, Synthetic synthetic.example"
            ),
            attribution_value="demand" if case == "invalid" else "forecasting",
            attribution_evidence=(
                None
                if case in {"missing_quote", "disabled"}
                else "Ada, Synthetic synthetic.example"
                if case in {"unsupported", "identity_review"}
                else "I heard forecasting elsewhere"
                if case == "non_verbatim"
                else "Please scope a demand forecasting project."
            ),
        )
        result = await intake.submit(proposal, "7")
        assert result.submission is not None
        if case == "identity_review":
            assert result.status == "review"
            assert not result.submission.evidence.identity_verified
            assert result.submission.evidence.verification_notes == (
                "Company identity needs a verbatim source quote"
            )
            replay.objects["companies"].append({"id": "901", "properties": {}})
            replay.objects["contacts"].append({"id": "902", "properties": {}})
            result = await intake.resolve(
                result.id,
                IntakeResolution(
                    company_id="901",
                    contact_id="902",
                    evidence="Verified company 901 and contact 902 by hand.",
                ),
                "7",
            )
        else:
            assert result.submission.evidence.identity_verified
        if case not in {"invalid", "hidden"}:
            assert result.status == "completed", result.error_code
            properties = replay.objects["deals"][0]["properties"]
            if case == "valid":
                assert properties["how_they_heard_about_pymc_labs"] == "forecasting"
            else:
                assert result.submission is not None
                assert result.submission.evidence.attribution_value is None
                assert result.submission.evidence.attribution_evidence is None
                assert "how_they_heard_about_pymc_labs" not in properties
            assert properties["dealtype"] == "newbusiness"
            assert properties["channel"] == "Cold in-mail"
        else:
            assert result.status == "review", result.error_code
            assert await store.effect(result.id, "deal") is None
            assert replay.objects["deals"] == []
            assert result.progress.review_reason == (
                "Attribution is not a valid HubSpot option; review the explicit evidence"
            )
            replay.objects["deals"].append({"id": "900", "properties": {}})
            resolved = await intake.resolve(
                result.id,
                IntakeResolution(deal_id="900", evidence="Verified existing deal 900 by hand."),
                "7",
            )
            assert resolved.status == "completed", resolved.error_code
            assert resolved.progress.deal_id == "900"
            assert len(replay.objects["deals"]) == 1


@pytest.mark.parametrize("case", ["company_domain", "company_name", "orphan", "opportunity"])
async def test_crm_review_identifies_candidates(case: str) -> None:
    replay = ProviderReplay()
    payload = {
        "domain": "synthetic.example",
        "name": "Synthetic",
        "company": "201",
        "contact": "202",
        "company_name": "Synthetic",
        "description": "Forecast project",
        "separate": "False",
    }
    if case.startswith("company"):
        replay.company_candidates = [
            {
                "id": str(n),
                "properties": {
                    "name": "Synthetic",
                    "domain": "synthetic.example" if case == "company_domain" else "other.example",
                },
            }
            for n in (301, 302)
        ]
        step, reason = "company", "company_ambiguous"
        expected = ["Synthetic (301)", "Synthetic (302)"]
    else:
        replay.objects["deals"] = [
            {
                "id": str(n),
                "properties": {
                    "dealname": f"Synthetic project {n}",
                    "description": "Different project",
                    "hs_is_closed": "false",
                },
            }
            for n in (301, 302)
        ]
        if case == "opportunity":
            replay.associations[("201", "deals")] = {"301", "302"}
        step = "deal"
        reason = "orphan_opportunity_review" if case == "orphan" else "opportunity_ambiguous"
        expected = ["Synthetic project 301 (301)", "Synthetic project 302 (302)"]
    async with httpx.AsyncClient(transport=httpx.MockTransport(replay.handle)) as client:
        ports = HttpIntakePorts(
            client, gmail_token="synthetic", hubspot_token="synthetic", discord_token="synthetic"
        )
        with pytest.raises(ReviewRequired, match=reason) as exc:
            await ports.recover(config(), EffectRequest(step=step, key="stable", payload=payload))
    assert all(candidate in str(exc.value) for candidate in expected)
    assert replay.events == []
