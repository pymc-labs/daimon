"""Genuine N7 submit/readback in the frozen board, with no provider credit."""

from __future__ import annotations

import copy
import json
import os
import shutil
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import httpx
import pytest
from daimon.adapters.discord import credential_modals
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.credential_requests import mint_request_token
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.credential_requests import create_credential_request
from daimon.core.stores.domain import CredentialRequestRow
from daimon.testing.db import require_owned_sessionmaker
from daimon.testing.discord_credential_conformance import LoopbackRejectingProbe
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.isolated_sdk import build_async_anthropic
from daimon.testing.ma_models import ma_agent
from daimon.testing.provider_fixtures import FixtureGap
from daimon.testing.turn_fakes import deps_for
from mux.contracts.ids import Provider
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker

from . import headless_executor as executor
from .catalog_runner import ScenarioPlan, load_catalog, plan_scenario
from .rejected_token_catalog import (
    MAPPING_NOTE_SHA256,
    SCENARIO,
    RejectedTokenBinding,
    validate_rejected_token_source,
)

CATALOG = Path(__file__).resolve().parents[2] / "packages/testing/fixtures/target53/catalog"
pytest_plugins = ["daimon.testing.db"]


@dataclass(frozen=True)
class OwnedSubmit:
    runtime: DiscordRuntime
    row: CredentialRequestRow
    probe: LoopbackRejectingProbe
    interaction: discord.Interaction[Any]
    requests: list[httpx.Request]


@pytest.fixture
async def owned_submit(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[OwnedSubmit]:
    try:
        require_owned_sessionmaker(db_session_factory)
    except ValueError:
        raw = db_session_factory.kw.get("bind")
        engine = raw.engine if isinstance(raw, AsyncConnection) else raw
        if (
            isinstance(engine, AsyncEngine)
            and engine.url.get_backend_name() == "postgresql"
            and engine.url.host in ("localhost", "127.0.0.1")
            and engine.url.database == "daimon_test"
            and not engine.url.query
            and not db_session_factory.kw.get("binds")
            and not (os.environ.get("GITHUB_ACTIONS") == "true" and os.environ.get("CI") == "true")
        ):
            pytest.skip("administrative proof requires an owned database or isolated CI schema")
        raise
    origin, agent_id = "https://reject.qa.example/mcp", "agent_01CatalogRejected"
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, platform="discord", workspace_id="111")
        account = await make_account(session, tenant=tenant)
        row = await create_credential_request(
            session,
            token=mint_request_token(),
            kind="mcp",
            tenant_id=tenant.id,
            agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=agent_id),
            account_id=account.id,
            target="linear",
            mcp_server_url=origin,
            requester_platform_user_id="100000000000000001",
            channel_id="333",
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
            idempotency_key=uuid.uuid4(),
            target_ma_agent_id=agent_id,
            target_name="catalog agent",
            requested_work=None,
            platform="discord",
            parent_channel_id="222",
            origin_thread_id="333",
            posted_message_id="444",
        )
    agent = ma_agent(id=agent_id, name="catalog agent", metadata={"daimon_tenant": str(tenant.id)})
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path == "/v1/agents":
            return httpx.Response(
                200, json={"data": [agent.model_dump(mode="json")], "has_more": False}
            )
        if request.method == "GET" and request.url.path == f"/v1/agents/{agent_id}":
            return httpx.Response(200, json=agent.model_dump(mode="json"))
        raise AssertionError("rejected-token submit attempted an unbound SDK operation")

    client = build_async_anthropic(transport=httpx.MockTransport(handler))
    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild_id = 111
    interaction.user = MagicMock(spec=discord.Member)
    interaction.user.id = 100000000000000001
    interaction.user.guild_permissions.administrator = True
    interaction.user.guild_permissions.manage_guild = False
    interaction.guild = MagicMock()
    interaction.guild.owner_id = 999
    interaction.type = discord.InteractionType.modal_submit
    interaction.message = None
    interaction.channel_id = 333
    interaction.channel = MagicMock(spec=discord.Thread)
    interaction.channel.parent_id = 222
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.response.is_done.return_value = True
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    interaction.client = MagicMock()
    interaction.client.get_partial_messageable.return_value.get_partial_message.return_value.edit = AsyncMock()
    settings = MagicMock()
    settings.mcp.public_url = "https://mcp.example.com/mcp"
    settings.mcp.jwt_secret.get_secret_value.return_value = "offline-fixture-" * 3
    try:
        async with LoopbackRejectingProbe(origin) as probe:
            runtime = DiscordRuntime(
                settings=settings,
                anthropic=client,
                sessionmaker=db_session_factory,
                notebook_rate_limiter=RateLimiter(max_requests=999),
                billing_config=None,
                deployment_default=DeploymentDefault(),
                resolver_cache=new_resolver_cache(),
                turn_deps=replace(deps_for(client, db_session_factory), turn_path=None),
                mcp_token_probe=probe,
            )
            yield OwnedSubmit(runtime, row, probe, interaction, requests)
    finally:
        await client.close()


def require(condition: object, reason: str) -> None:
    if not condition:
        raise AssertionError(reason)


def plan(backend: Provider = "anthropic") -> ScenarioPlan:
    entry = next(e for e in load_catalog(CATALOG) if e.id == SCENARIO)
    return plan_scenario(entry, CATALOG, backend, run_id="rejected-token-catalog")


def binding(owned: OwnedSubmit) -> RejectedTokenBinding:
    return RejectedTokenBinding(
        owned.runtime,
        owned.row,
        owned.interaction,
        owned.probe,
    )


async def test_genuine_admin_release_stays_pending_after_json_roundtrip_without_a_turn(
    owned_submit: OwnedSubmit, record_property: Callable[[str, object], None]
) -> None:
    result = await executor.execute_cell(plan(), None, rejected_token=binding(owned_submit))
    encoded = result.model_dump_json(exclude_computed_fields=True)
    restored = executor.CellResult.model_validate_json(encoded)
    require(restored.reported_status == "PENDING", "administration acquired full-cell credit")
    require(restored.catalog_outcome.status == "PASS", "real released-row diagnostic missing")
    require(
        any(check.code == "SOURCE_SQL_INVALID" for check in restored.host_outcome.checks),
        "invalid SQL lost its typed refusal",
    )
    require(restored.mode == "offline_admin_credential", "administrative boundary missing")
    require(not restored.evidence.turns and restored.request_count == 0, "invented provider turn")
    require(restored.host_outcome.status == "PENDING", "administration certified provider turns")
    require(len(restored.catalog_outcome.checks) == 1, "frozen db predicate dropped")
    require(restored.catalog_outcome.checks[0].turn is None, "admin predicate acquired a turn")
    admin = restored.admin_credential
    require(admin is not None, "actual envelope missing")
    if admin is None:
        raise AssertionError("actual envelope missing")
    require(admin.mapping_note_sha256 == MAPPING_NOTE_SHA256, "request-token mapping not pinned")
    require(admin.receipt.before.unused and admin.receipt.after.unused, "wrong actual readback")
    require(not admin.receipt.at_probe.unused, "probe did not observe the actual consumed attempt")
    require(admin.receipt.after.outcome == "token_rejected", "submit refusal not observed")
    require(admin.receipt.probe_status == 403, "actual MCP rejection missing")
    require(
        owned_submit.row.token not in encoded and "wrong-token-123" not in encoded, "token leak"
    )
    require(
        all(r.method == "GET" for r in owned_submit.requests), "rejected token wrote credentials"
    )
    record_property("new27_cell", encoded)


async def test_forged_admin_envelopes_refuse_after_json_roundtrip(
    owned_submit: OwnedSubmit,
) -> None:
    result = await executor.execute_cell(plan(), None, rejected_token=binding(owned_submit))
    raw = result.model_dump(mode="json", exclude_computed_fields=True)
    for fault in (
        "empty_report",
        "claimed_pass",
        "foreign_provider",
        "foreign_source",
        "foreign_evidence",
        "no_envelope",
        "turn_mode",
        "request_count",
        "host_pass",
        "missing_readback",
        "foreign_token",
        "wrong_predicate",
        "pre_submit_probe",
        "post_finish_probe",
        "foreign_mapping",
        "foreign_hook",
        "foreign_submit_token",
        "not_rejected",
        "extra_check",
        "readback_changed",
        "readback_changed_consistent",
        "non_finite_window",
        "extra_field",
    ):
        forged: dict[str, Any] = copy.deepcopy(raw)
        admin = forged["admin_credential"]
        if fault == "empty_report":
            forged["catalog_outcome"]["checks"] = []
        elif fault == "claimed_pass":
            forged["reported_status"] = "PASS"
        elif fault == "foreign_provider":
            forged["backend"] = "openai"
        elif fault == "foreign_source":
            forged["source_sha256"] = "0" * 64
        elif fault == "foreign_evidence":
            forged["evidence"]["scenario_id"] = "foreign"
        elif fault == "no_envelope":
            forged["admin_credential"] = None
        elif fault == "turn_mode":
            forged["mode"] = "offline_scripted"
        elif fault == "request_count":
            forged["request_count"] = 1
        elif fault == "host_pass":
            forged["host_outcome"]["checks"][0]["status"] = "PASS"
        elif fault == "missing_readback":
            del admin["receipt"]["after"]
        elif fault == "foreign_token":
            admin["receipt"]["request_token_sha256"] = "0" * 64
        elif fault == "wrong_predicate":
            admin["receipt"]["unused_assertion"] = False
        elif fault == "pre_submit_probe":
            admin["probe_s"] = admin["started_s"] - 1
        elif fault == "post_finish_probe":
            admin["probe_s"] = admin["finished_s"] + 1
        elif fault == "foreign_mapping":
            admin["mapping_note_sha256"] = "0" * 64
        elif fault == "foreign_hook":
            admin["hook_head"] = "0" * 40
        elif fault == "foreign_submit_token":
            admin["submitted_token_sha256"] = "0" * 64
        elif fault == "not_rejected":
            admin["receipt"]["after"]["outcome"] = None
        elif fault == "extra_check":
            forged["catalog_outcome"]["checks"].append(forged["catalog_outcome"]["checks"][0])
        elif fault in ("readback_changed", "readback_changed_consistent"):
            used = datetime.fromisoformat(admin["receipt"]["at_probe"]["used_at"])
            admin["receipt"]["after"]["used_at"] = (used + timedelta(seconds=1)).isoformat()
            admin["receipt"]["after"]["unused"] = False
            admin["receipt"]["unused_assertion"] = False
            if fault == "readback_changed_consistent":
                check = forged["catalog_outcome"]["checks"][0]
                check["status"] = "FAIL"
                check["code"] = "CREDENTIAL_REQUEST_CONSUMED"
        elif fault == "non_finite_window":
            admin["finished_s"] = float("inf")
        else:
            admin["invented"] = True
        match = (
            "changed across the rejecting probe" if fault == "readback_changed_consistent" else None
        )
        with pytest.raises(ValueError, match=match):
            executor.CellResult.model_validate_json(json.dumps(forged))


@pytest.mark.parametrize("backend", ("openai", "gemini"))
async def test_a3_providers_stay_pending_without_using_the_anthropic_hook(
    owned_submit: OwnedSubmit,
    backend: Provider,
) -> None:
    result = await executor.execute_cell(plan(backend), None)
    require(result.reported_status == "PENDING", "A3 provider acquired execution credit")
    require("admin_credential" not in result.model_dump(), "default serialization changed")
    with pytest.raises(ValueError, match="Anthropic-only"):
        await executor.execute_cell(plan(backend), None, rejected_token=binding(owned_submit))
    require(not owned_submit.probe.posts, "A3 refusal contacted MCP")


@pytest.mark.parametrize(
    "fault",
    (
        "source_pin",
        "predicate",
        "token",
        "url",
        "extra_step",
        "missing_step",
        "host_turn",
        "extra_assertion",
        "global_assertion",
        "invocation",
        "notes",
    ),
)
async def test_source_mutation_refuses_before_submit_or_probe(
    owned_submit: OwnedSubmit,
    fault: str,
) -> None:
    original = plan()
    raw: dict[str, Any] = original.model_dump()
    if fault == "source_pin":
        raw["scenario"]["sha256"] = "0" * 64
    elif fault == "predicate":
        raw["scenario"]["scenario"]["assert"][0]["expect"] = False
    elif fault in {"token", "url"}:
        raw["scenario"]["scenario"]["steps"][0]["args"][fault] = "foreign"
    elif fault == "extra_step":
        raw["scenario"]["scenario"]["steps"].append({"do": "wait", "s": 0})
    elif fault == "missing_step":
        raw["scenario"]["scenario"]["steps"] = []
    elif fault == "host_turn":
        raw["scenario"]["scenario"]["est_turns"] = 1
    elif fault == "extra_assertion":
        raw["assertions"] = [*raw["assertions"], {"kind": "db_check", "expect": True}]
    elif fault == "global_assertion":
        raw["global_assertions"] = [{"kind": "turn_completed", "turn": 1}]
    elif fault == "invocation":
        raw["invocations"][0]["root"] = "invented"
    else:
        raw["scenario"]["scenario"]["notes"] = "invented permission"
    with pytest.raises(ValueError):
        changed = ScenarioPlan.model_validate(raw)
        await executor.execute_cell(changed, None, rejected_token=binding(owned_submit))
    require(not owned_submit.probe.posts, "mutated source reached MCP")


async def test_real_consume_removal_changes_probe_readback_without_scored_credit(
    owned_submit: OwnedSubmit,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def leave_unused(*_args: object, **_kwargs: object) -> CredentialRequestRow:
        return owned_submit.row

    monkeypatch.setattr(credential_modals, "consume_credential_submit", leave_unused)
    result = await executor.execute_cell(plan(), None, rejected_token=binding(owned_submit))
    require(result.reported_status == "PENDING", "invalid SQL acquired full-cell credit")
    require(result.catalog_outcome.status == "PASS", "unused-row diagnostic missing")
    if result.admin_credential is None:
        raise AssertionError("consume-removal receipt missing")
    require(result.admin_credential.receipt.at_probe.unused, "consume removal was not observed")
    require(result.host_outcome.status == "PENDING", "admin result became provider certification")


async def test_real_release_removal_changes_diagnostic_but_invalid_sql_stays_pending(
    owned_submit: OwnedSubmit,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Main #611's actual release is the difference between unused and consumed.
    # Keep admission/consume/probe real; remove only the store's release operation.
    monkeypatch.setattr(
        credential_modals.credential_requests,
        "release_failed_credential_request",
        AsyncMock(return_value=None),
    )
    result = await executor.execute_cell(plan(), None, rejected_token=binding(owned_submit))
    require(result.status == "FAIL", "release removal did not change the observed predicate")
    require(result.reported_status == "PENDING", "invalid SQL acquired full-cell credit")
    require(result.catalog_outcome.status == "FAIL", "surviving consumption diagnostic lost")
    require(result.host_outcome.status == "PENDING", "admin result became provider certification")
    restored = executor.CellResult.model_validate_json(
        result.model_dump_json(exclude_computed_fields=True)
    )
    require(restored.reported_status == "PENDING", "JSON roundtrip acquired full-cell credit")


async def test_reassigned_used_at_cannot_issue_an_admin_evidence_envelope(
    owned_submit: OwnedSubmit,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy import text

    async def reassign_attempt(
        session: AsyncSession, *, row: CredentialRequestRow, outcome: str
    ) -> CredentialRequestRow | None:
        require(outcome == "token_rejected", "mutation touched an unrelated release")
        if row.used_at is None:
            raise AssertionError("mutation lacks the real consumed attempt")
        await session.execute(
            text("UPDATE credential_requests SET used_at = :used WHERE token = :token"),
            {"used": row.used_at + timedelta(seconds=1), "token": row.token},
        )
        return None

    monkeypatch.setattr(
        credential_modals.credential_requests, "release_failed_credential_request", reassign_attempt
    )
    with pytest.raises(ValueError, match="readbacks changed"):
        await executor.execute_cell(plan(), None, rejected_token=binding(owned_submit))
    require(
        len(owned_submit.probe.posts) == 1, "timestamp refusal bypassed the real rejecting probe"
    )


async def test_removed_submit_cannot_issue_an_admin_evidence_envelope(
    owned_submit: OwnedSubmit,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(credential_modals.McpCredentialModal, "on_submit", AsyncMock())
    with pytest.raises(ValueError, match="did not reach"):
        await executor.execute_cell(plan(), None, rejected_token=binding(owned_submit))
    require(not owned_submit.probe.posts, "no-submit mutation called MCP")


@pytest.mark.parametrize("selected", ("admin_values", "native_gaps", "replay"))
async def test_admin_binding_cannot_discard_other_selected_inputs(
    owned_submit: OwnedSubmit,
    selected: str,
) -> None:
    selected_plan = plan()
    with pytest.raises(ValueError, match="cannot replace selected turn inputs"):
        await executor.execute_cell(
            selected_plan,
            executor.ScenarioReplay(
                scenario_id=SCENARIO,
                scenario_sha256=selected_plan.scenario.sha256,
                turns=(),
            )
            if selected == "replay"
            else None,
            admin_values={"request": "foreign"} if selected == "admin_values" else None,
            native_gaps=(FixtureGap(code="RUNNER_HOOK", location="steps[0]", reason="unbound"),)
            if selected == "native_gaps"
            else (),
            rejected_token=binding(owned_submit),
        )
    require(not owned_submit.probe.posts, "conflicting inputs reached MCP")


async def test_full_frozen_board_changes_exactly_one_cell(
    tmp_path: Path,
    owned_submit: OwnedSubmit,
    record_property: Callable[[str, object], None],
) -> None:
    shutil.copytree(CATALOG, tmp_path, dirs_exist_ok=True)
    (tmp_path / "SCHEMA.md").write_text("offline frozen administrative binding proof")
    (tmp_path / "PROPOSED-KINDS.md").write_text("offline frozen administrative binding proof")
    before = await executor.execute_catalog(
        tmp_path,
        integration_sha="a" * 40,
        run_id="new27-board",
        replays=(),
    )
    after = await executor.execute_catalog(
        tmp_path,
        integration_sha="a" * 40,
        run_id="new27-board",
        replays=(),
        rejected_token_runs={(SCENARIO, "anthropic"): binding(owned_submit)},
    )
    require(
        before.scored_counts == {"PASS": 0, "FAIL": 0, "EXPECTED-FAIL": 0, "PENDING": 159},
        "unbound baseline changed",
    )
    require(
        after.scored_counts == {"PASS": 0, "FAIL": 0, "EXPECTED-FAIL": 0, "PENDING": 159},
        "administrative binding moved unrelated cells",
    )
    changed: list[tuple[str, Provider]] = []
    for previous, current in zip(before.cells, after.cells, strict=True):
        if previous.model_dump_json() != current.model_dump_json():
            changed.append((current.scenario_id, current.backend))
    require(changed == [(SCENARIO, "anthropic")], "binding changed unrelated frozen cells")
    record_property("new27_board_counts", json.dumps(after.scored_counts, sort_keys=True))
    record_property("new27_board", after.model_dump_json(exclude_computed_fields=True))


@pytest.mark.parametrize(
    "key", ((SCENARIO, "openai"), (SCENARIO, "gemini"), ("foreign", "anthropic"))
)
async def test_foreign_opt_in_key_refuses_before_io(
    tmp_path: Path,
    owned_submit: OwnedSubmit,
    key: tuple[str, Provider],
) -> None:
    shutil.copytree(CATALOG, tmp_path, dirs_exist_ok=True)
    (tmp_path / "SCHEMA.md").write_text("offline key proof")
    (tmp_path / "PROPOSED-KINDS.md").write_text("offline key proof")
    with pytest.raises(ValueError, match="Anthropic-only"):
        await executor.execute_catalog(
            tmp_path,
            integration_sha="a" * 40,
            run_id="foreign",
            replays=(),
            rejected_token_runs={key: binding(owned_submit)},
        )
    require(not owned_submit.probe.posts, "foreign key reached MCP")


def test_default_source_pin_is_accepted_without_certifying_an_execution() -> None:
    validate_rejected_token_source(plan())
