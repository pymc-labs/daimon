"""Cleanup evidence through the actual isolated SDK, with no cleanup runner."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

import httpx
import pytest
from daimon.core.mux_backend import managed_agents
from daimon.core.mux_compat import resource_ref
from daimon.testing.admin_resources import (
    ResourceManifestEntry,
    freeze_resource_manifest,
    verify_archive_readback,
    verify_cancel_readback,
)
from daimon.testing.ma_models import ma_agent
from mux.contracts.ids import Revision, Scope
from mux.contracts.receipts import CancelReceipt, Operation, SendReceipt
from mux.contracts.resources import SessionSpec
from mux.errors import ScopeViolation, UnsupportedCapability
from mux.testing.drivers.anthropic.ma_models import ma_session
from mux.testing.drivers.anthropic.ma_transport import ScriptedReply, ScriptedTransport

NOW = datetime.now(UTC)
SCOPE = Scope(
    tenant_id="tenant", account_id="account", principal_id="caller", authorization_id="root"
)


@asynccontextmanager
async def case():
    raw = ma_session(
        id="session",
        agent=ma_agent(id="agent"),
        environment_id="environment",
        metadata={"daimon_tenant": "tenant", "daimon_account": "account"},
    ).model_dump(mode="json")
    transport = ScriptedTransport()
    transport.queue(ScriptedReply("POST", "/v1/sessions", httpx.Response(200, json=raw)))
    async with transport.client() as client:
        backend = managed_agents(
            client,
            scope=SCOPE,
            resources=frozenset(
                {("session", "session"), ("agent", "agent"), ("environment", "environment")}
            ),
        )
        created = await backend.sessions.create(
            SCOPE,
            SessionSpec(
                agent=resource_ref(backend, "agent", "agent", scope=SCOPE),
                agent_revision=Revision(local=1),
                config_revision=1,
                environment=resource_ref(backend, "environment", "environment", scope=SCOPE),
                metadata={"daimon_tenant": "tenant", "daimon_account": "account"},
            ),
            key="create",
        )
        creation = Operation(
            id="create",
            key="create",
            request_digest=sha256(transport.requests[0].body).hexdigest(),
            status="processed",
            resource=created.ref,
            created_at=NOW - timedelta(minutes=1),
            updated_at=NOW,
        )
        entry = ResourceManifestEntry(
            scope=SCOPE,
            run_id=uuid4(),
            binding_id="binding",
            resource=created.ref,
            creation_key="create",
            creation=creation,
            creation_ack=created.ref,
            actions=("cancel", "archive", "delete"),
            input=SendReceipt(
                operation_id="input", status="processed", input_ids=("message",), turn_id="root"
            ),
            agent_id="agent",
            environment_id="environment",
        )
        yield backend, entry, raw, transport
    transport.assert_consumed()


def cancel(entry, status="requested"):
    return CancelReceipt(
        operation_id="cancel",
        session=entry.resource,
        turn_id="root",
        status=status,
        requested_at=NOW,
    )


def archive(entry):
    return Operation(
        id="archive",
        key="archive",
        request_digest="a" * 64,
        resource=entry.resource,
        status="processed",
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.mark.parametrize("status", ("requested", "already_stopped", "outcome_unknown"))
async def test_cancel_ack_is_not_a_native_stop(status):
    async with case() as (backend, entry, raw, transport):
        transport.queue(ScriptedReply.stream("/v1/sessions/session/events/stream", []))
        result = await verify_cancel_readback(
            backend,
            entry,
            cancel(entry, status),
            operation_id="cancel",
            deadline=datetime.now(UTC) + timedelta(seconds=2),
        )
        assert result.status == "pending" and result.stopped is False
        assert result.stop is not None and not result.stop.stopped
        assert [r.method for r in transport.requests] == ["POST", "GET"]


async def test_cancel_uses_actual_independent_sdk_root_stop():
    async with case() as (backend, entry, raw, transport):
        transport.queue(
            ScriptedReply.stream(
                "/v1/sessions/session/events/stream",
                [
                    {
                        "type": "session.status_idle",
                        "id": "stop",
                        "processed_at": NOW.isoformat(),
                        "stop_reason": {"type": "end_turn"},
                    }
                ],
            )
        )
        result = await verify_cancel_readback(
            backend,
            entry,
            cancel(entry),
            operation_id="cancel",
            deadline=datetime.now(UTC) + timedelta(seconds=2),
        )
        assert result.status == "verified" and result.stopped is True
        assert result.stop.receipt_operation_id == "cancel" and result.archived is None
        assert [r.path for r in transport.requests] == [
            "/v1/sessions",
            "/v1/sessions/session/events/stream",
        ]


@pytest.mark.parametrize("fault", ("operation", "turn", "session", "input_unknown", "no_turn"))
async def test_foreign_cancel_receipt_refuses_before_read(fault):
    async with case() as (backend, entry, raw, transport):
        receipt = cancel(entry)
        if fault == "operation":
            receipt = receipt.model_copy(update={"operation_id": "other"})
        elif fault == "turn":
            receipt = receipt.model_copy(update={"turn_id": "other"})
        elif fault == "session":
            receipt = receipt.model_copy(
                update={"session": entry.resource.model_copy(update={"id": "other"})}
            )
        else:
            entry = entry.model_copy(
                update={
                    "input": entry.input.model_copy(
                        update={"status": "outcome_unknown"}
                        if fault == "input_unknown"
                        else {"turn_id": None}
                    )
                }
            )
        with pytest.raises(ScopeViolation):
            await verify_cancel_readback(
                backend,
                entry,
                receipt,
                operation_id="cancel",
                deadline=datetime.now(UTC) + timedelta(seconds=2),
            )
        assert len(transport.requests) == 1


@pytest.mark.parametrize(
    "archived", (None, False, "invalid", "2026-01-01T00:00:00", "2020-01-01T00:00:00Z")
)
async def test_archive_ack_without_valid_independent_after_state_is_pending(archived):
    async with case() as (backend, entry, raw, transport):
        raw["archived_at"] = archived
        transport.queue(ScriptedReply("GET", "/v1/sessions/session", httpx.Response(200, json=raw)))
        result = await verify_archive_readback(
            backend, entry, archive(entry), operation_id="archive"
        )
        assert result.status == "pending" and result.stopped is None


async def test_actual_archive_ack_and_get_never_claim_stop_or_billing_finality():
    async with case() as (backend, entry, raw, transport):
        raw["archived_at"] = datetime.now(UTC).isoformat()
        transport.queue(
            ScriptedReply("POST", "/v1/sessions/session/archive", httpx.Response(200, json=raw)),
            ScriptedReply("GET", "/v1/sessions/session", httpx.Response(200, json=raw)),
        )
        receipt = await backend.sessions.archive(SCOPE, entry.resource, key="archive")
        result = await verify_archive_readback(backend, entry, receipt, operation_id="archive")
        assert result.status == "verified" and result.archived
        assert result.archived_at and result.observed_at
        assert result.stopped is None and result.stop is None
        assert [r.method for r in transport.requests] == ["POST", "POST", "GET"]


@pytest.mark.parametrize("fault", ("id", "agent", "environment", "tenant", "account"))
async def test_foreign_archive_get_cannot_verify_cleanup(fault):
    async with case() as (backend, entry, raw, transport):
        raw["archived_at"] = datetime.now(UTC).isoformat()
        if fault == "id":
            raw["id"] = "other"
        elif fault == "agent":
            raw["agent"]["id"] = "other"
        elif fault == "environment":
            raw["environment_id"] = "other"
        else:
            raw["metadata"]["daimon_" + fault] = "other"
        transport.queue(ScriptedReply("GET", "/v1/sessions/session", httpx.Response(200, json=raw)))
        with pytest.raises(ScopeViolation):
            await verify_archive_readback(backend, entry, archive(entry), operation_id="archive")


async def test_borrowed_and_unknown_cannot_become_owned_cleanup():
    async with case() as (backend, entry, raw, transport):
        borrowed = entry.model_copy(
            update={"creation": None, "creation_key": None, "creation_ack": None}
        )
        unknown = entry.model_copy(
            update={
                "resource": None,
                "creation_ack": None,
                "creation": entry.creation.model_copy(update={"status": "outcome_unknown"}),
            }
        )
        assert borrowed.ownership == "borrowed" and unknown.ownership == "unknown"
        for row in (borrowed, unknown):
            result = await verify_archive_readback(
                backend, row, archive(entry), operation_id="archive"
            )
            assert result.status == "retained" and result.archived is None
        assert len(transport.requests) == 1
        assert freeze_resource_manifest((unknown,))[0].resource is None


@pytest.mark.parametrize(
    "fault", ("ack", "key", "tenant", "account", "platform", "run", "duplicate", "unknown_id")
)
async def test_manifest_refuses_forged_cross_scope_or_duplicate_resources(fault):
    async with case() as (backend, entry, raw, transport):
        rows = (entry,)
        if fault == "ack":
            entry = entry.model_copy(
                update={"creation_ack": entry.resource.model_copy(update={"id": "other"})}
            )
        elif fault == "key":
            entry = entry.model_copy(update={"creation_key": "other"})
        elif fault == "tenant":
            entry = entry.model_copy(
                update={"scope": SCOPE.model_copy(update={"tenant_id": "other"})}
            )
        elif fault == "account":
            entry = entry.model_copy(
                update={"scope": SCOPE.model_copy(update={"account_id": "other"})}
            )
        elif fault == "platform":
            entry = entry.model_copy(
                update={"scope": Scope.platform(reason="operator is not owned QA")}
            )
        elif fault == "unknown_id":
            entry = entry.model_copy(
                update={"creation": entry.creation.model_copy(update={"status": "outcome_unknown"})}
            )
        elif fault == "run":
            rows = (entry, entry.model_copy(update={"run_id": uuid4()}))
        elif fault == "duplicate":
            rows = (entry, entry)
        with pytest.raises(ValueError):
            freeze_resource_manifest((entry,) if fault not in ("run", "duplicate") else rows)


async def test_manifest_exports_no_destructive_eligibility_for_borrowed_resources():
    async with case() as (backend, entry, raw, transport):
        borrowed = entry.model_copy(
            update={"creation": None, "creation_key": None, "creation_ack": None}
        )
        assert borrowed.eligible_actions == ("cancel",)
        unknown = entry.model_copy(
            update={
                "resource": None,
                "creation_ack": None,
                "creation": entry.creation.model_copy(update={"status": "outcome_unknown"}),
            }
        )
        assert unknown.eligible_actions == ()
        assert "archive" in entry.eligible_actions and "delete" in entry.eligible_actions
        serialized = entry.model_dump_json()
        assert "status" in serialized and "native" not in ResourceManifestEntry.model_fields
        with pytest.raises(ValueError):
            ResourceManifestEntry.model_validate({**entry.model_dump(), "ownership": "created"})


async def test_missing_archive_readback_and_unknown_ack_do_not_verify():
    async with case() as (backend, entry, raw, transport):

        class WithoutReadback:
            def capabilities(self):
                return backend.capabilities()

            def extension(self, *args, **kwargs):
                raise UnsupportedCapability(("session_reads",), "unavailable")

        result = await verify_archive_readback(
            WithoutReadback(), entry, archive(entry), operation_id="archive"
        )
        assert result.status == "retained" and result.archived is None
        receipt = archive(entry).model_copy(update={"status": "outcome_unknown"})
        result = await verify_archive_readback(backend, entry, receipt, operation_id="archive")
        assert result.status == "pending" and result.stopped is None
        assert len(transport.requests) == 1


@pytest.mark.parametrize("fault", ("key", "request_key", "resource", "action"))
async def test_archive_receipt_binding_refuses_before_get(fault):
    async with case() as (backend, entry, raw, transport):
        receipt = archive(entry)
        if fault == "key":
            receipt = receipt.model_copy(update={"id": "other"})
        elif fault == "request_key":
            receipt = receipt.model_copy(update={"key": "other"})
        elif fault == "resource":
            receipt = receipt.model_copy(
                update={
                    "resource": entry.resource.model_copy(update={"account_scope_id": "foreign"})
                }
            )
        else:
            entry = entry.model_copy(update={"actions": ("cancel",)})
        with pytest.raises(ScopeViolation):
            await verify_archive_readback(backend, entry, receipt, operation_id="archive")
        assert len(transport.requests) == 1


async def test_naive_creation_time_is_refused_before_archive_read():
    async with case() as (backend, entry, raw, transport):
        row = entry.model_copy(
            update={
                "creation": entry.creation.model_copy(
                    update={"created_at": NOW.replace(tzinfo=None)}
                )
            }
        )
        with pytest.raises(ValueError):
            await verify_archive_readback(backend, row, archive(entry), operation_id="archive")
        assert len(transport.requests) == 1


async def test_creation_journal_without_resource_uses_its_separate_actual_sdk_ack():
    from mux.state.memory import MemoryStateStore

    async with case() as (backend, entry, raw, transport):
        store = MemoryStateStore()
        await store.begin_operation(
            SCOPE,
            key="actual-create",
            operation_id="actual-create",
            request_digest=entry.creation.request_digest,
            now=NOW,
        )
        await store.claim_send(SCOPE, "actual-create", now=NOW, fence=None)
        record = await store.advance_operation(
            SCOPE, "actual-create", "processed", now=NOW, fence=None, result=raw
        )
        assert record.operation.resource is None
        observed = entry.model_copy(
            update={"creation": record.operation, "creation_key": "actual-create"}
        )
        manifest = freeze_resource_manifest((observed,))
        assert manifest[0].ownership == "created" and manifest[0].creation_ack == entry.resource
        raw["archived_at"] = datetime.now(UTC).isoformat()
        transport.queue(ScriptedReply("GET", "/v1/sessions/session", httpx.Response(200, json=raw)))
        assert (
            await verify_archive_readback(
                backend, manifest[0], archive(entry), operation_id="archive"
            )
        ).archived
