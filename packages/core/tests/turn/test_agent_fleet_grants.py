"""Per-agent fleet ceilings use the real preference CAS and audit owners."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from daimon.core._models import AgentBackendRevisionRecord
from daimon.core.channel_backend import AgentBackendRefused
from daimon.core.neutral_resources import AgentFleetGrant, WorkerLimits
from daimon.core.stores import security_audit
from daimon.core.stores.domain import FLEET_OFF, AgentBackendRevision
from mux.contracts.ids import ModelRef
from mux.profiles import backend_selection
from pydantic import ValidationError
from sqlalchemy import update

from ..test_agent_backend import enable
from ..test_agent_backend import owned as owned
from .test_fleet_source_permissions import captured as captured

NOW = datetime(2026, 10, 13, 21, 0, tzinfo=UTC)


def grant():
    return AgentFleetGrant(
        max_workers=4,
        choices=(
            (backend_selection("anthropic"), ModelRef(provider="anthropic", id="claude-haiku-5-5")),
            (backend_selection("openai"), ModelRef(provider="openai", id="gpt-6-luna")),
            (
                backend_selection("gemini").model_copy(update={"model": "gemini-3.5-flash-lite"}),
                ModelRef(provider="gemini", id="gemini-3.5-flash-lite"),
            ),
        ),
        limits=WorkerLimits(
            budget_usd=Decimal("0.9"),
            deadline=NOW + timedelta(hours=1),
            max_turns=1,
        ),
        max_run_seconds=600,
        max_worker_seconds=120,
        max_root_turns=3,
    )


async def set_grant(service, agent, actor, value=None):
    value = value or grant()
    preview = await service.preview_fleet_grant(agent, value, now=NOW)
    return await service.set_fleet_grant(
        agent,
        value,
        preview.setting_revision,
        actor,
        expected_preview_digest=preview.digest,
        now=NOW,
    )


async def revoke_grant(service, agent, actor):
    preview = await service.preview_fleet_grant(agent, None, now=NOW)
    return await service.revoke_fleet_grant(
        agent,
        preview.setting_revision,
        actor,
        expected_preview_digest=preview.digest,
        now=NOW,
    )


async def test_grant_roundtrip_is_finite_scoped_and_audited(owned):
    service, agent, actor, _ = owned
    await enable(service, agent, actor)
    assert await service.read_fleet_grant(agent.tenant_id, agent.agent_id, now=NOW) is None
    revision = await set_grant(service, agent, actor)
    loaded = await service.read_fleet_grant(agent.tenant_id, agent.agent_id, now=NOW)
    assert loaded.model_dump(mode="json") == revision.fleet_grant
    assert loaded.epoch == revision.local == 1
    assert loaded.limits.budget_usd == Decimal("0.9")
    assert loaded.limits.deadline == NOW + timedelta(hours=1)
    assert loaded.max_workers == 4 and loaded.max_root_turns == 3
    assert loaded.max_run_seconds == 600 and loaded.max_worker_seconds == 120
    assert loaded.choices == grant().choices
    assert loaded.backends == grant().backends
    assert loaded.models == grant().models
    assert {
        (item.backend, item.profile, item.model, item.thread_mode) for item in loaded.backends
    } == {
        ("anthropic", "anthropic.managed_agents", None, "per_caller"),
        ("openai", "openai.persistent_workspace", "gpt-6-luna", "per_caller"),
        ("gemini", "gemini.inline_reuse", "gemini-3.5-flash-lite", "per_caller"),
    }
    rows = await service.audit(agent.tenant_id, agent_id=agent.agent_id)
    assert len(rows) == 1
    assert rows[0].account_id == actor.account_id and rows[0].outcome == "allowed"


async def test_backend_revert_after_grant_only_append_keeps_undo_pointer(owned):
    service, agent, actor, _ = owned
    await enable(service, agent, actor)
    first = await service.set_backend(agent, "openai", 0, actor)
    second = await service.set_backend(agent, "gemini", first.local, actor)
    granted = await set_grant(service, agent, actor)
    assert granted.backend == second.backend
    assert granted.previous_local == second.previous_local == first.local
    reverted = await service.revert(agent, actor, expected_revision=granted.local)
    assert reverted.backend == first.backend
    assert reverted.fleet_grant == granted.fleet_grant
    assert reverted.fleet_grant["epoch"] == granted.local


async def test_backend_revert_never_restores_a_revoked_grant(owned):
    service, agent, actor, _ = owned
    await enable(service, agent, actor)
    granted = await set_grant(service, agent, actor)
    selected = await service.set_backend(agent, "openai", granted.local, actor)
    assert selected.fleet_grant == granted.fleet_grant
    revoked = await revoke_grant(service, agent, actor)
    assert revoked.fleet_grant == FLEET_OFF and revoked.fleet_off
    reverted = await service.revert(agent, actor, expected_revision=revoked.local)
    assert reverted.backend is None and reverted.fleet_grant == FLEET_OFF
    assert await service.read_fleet_grant(agent.tenant_id, agent.agent_id, now=NOW) is None
    assert (await service.revision(agent, granted.local)).fleet_grant == granted.fleet_grant
    regranted = await set_grant(service, agent, actor)
    assert regranted.fleet_grant["epoch"] == regranted.local > granted.fleet_grant["epoch"]


async def test_backend_changes_carry_current_grant_without_renewing_expiry(owned):
    service, agent, actor, _ = owned
    await enable(service, agent, actor)
    granted = await set_grant(service, agent, actor)
    selected = await service.set_backend(agent, "openai", granted.local, actor)
    cleared = await service.clear_backend(agent, selected.local, actor)
    reverted = await service.revert(agent, actor, expected_revision=cleared.local)
    assert (
        selected.fleet_grant == cleared.fleet_grant == reverted.fleet_grant == granted.fleet_grant
    )
    assert (
        await service.read_fleet_grant(
            agent.tenant_id, agent.agent_id, now=NOW + timedelta(hours=1)
        )
        is None
    )


async def test_grant_cas_has_one_winner_and_one_audit(owned):
    service, agent, actor, _ = owned
    await enable(service, agent, actor)
    value = grant()
    preview = await service.preview_fleet_grant(agent, value, now=NOW)

    async def apply():
        return await service.set_fleet_grant(
            agent,
            value,
            preview.setting_revision,
            actor,
            expected_preview_digest=preview.digest,
            now=NOW,
        )

    results = await asyncio.gather(apply(), apply(), return_exceptions=True)
    assert sum(isinstance(result, AgentBackendRefused) for result in results) == 1
    assert (await service.current(agent)).local == 1
    assert len(await service.audit(agent.tenant_id, agent_id=agent.agent_id)) == 1


async def test_grant_stale_preview_is_not_reapplied(owned):
    service, agent, actor, _ = owned
    await enable(service, agent, actor)
    value = grant()
    preview = await service.preview_fleet_grant(agent, value, now=NOW)
    changed = agent.model_copy(update={"configuration_digest": "changed-source"})
    with pytest.raises(AgentBackendRefused, match="stale preview"):
        await service.set_fleet_grant(
            changed,
            value,
            preview.setting_revision,
            actor,
            expected_preview_digest=preview.digest,
            now=NOW,
        )
    assert await service.current(agent) is None


@pytest.mark.parametrize("via_key", (False, True))
async def test_untrusted_grant_actor_refuses_without_writes(owned, via_key):
    service, agent, actor, _ = owned
    await enable(service, agent, actor)
    subject = replace(actor.subject, is_admin=via_key, via_agent_key=via_key)
    with pytest.raises(AgentBackendRefused, match="admin required"):
        await set_grant(service, agent, replace(actor, subject=subject))
    assert await service.current(agent) is None
    assert await service.audit(agent.tenant_id, agent_id=agent.agent_id) == ()


async def test_corrupt_grant_refuses_fleet_without_breaking_backend_read(owned):
    service, agent, actor, sessions = owned
    await enable(service, agent, actor)
    granted = await set_grant(service, agent, actor)
    async with sessions.begin() as db:
        await db.execute(
            update(AgentBackendRevisionRecord)
            .where(
                AgentBackendRevisionRecord.tenant_id == agent.tenant_id,
                AgentBackendRevisionRecord.agent_id == agent.agent_id,
                AgentBackendRevisionRecord.local == granted.local,
            )
            .values(fleet_grant={"unexpected": "not-authority"})
        )
    ordinary = await service.current(agent)
    assert ordinary.backend is None and ordinary.fleet_grant is None
    with pytest.raises(AgentBackendRefused):
        await service.read_fleet_grant(agent.tenant_id, agent.agent_id, now=NOW)


async def test_audit_failure_rolls_back_fleet_grant(owned, monkeypatch):
    service, agent, actor, _ = owned
    await enable(service, agent, actor)

    async def unavailable(*args, **kwargs):
        raise RuntimeError("offline audit failure")

    monkeypatch.setattr(security_audit, "append_event", unavailable)
    with pytest.raises(RuntimeError, match="offline audit failure"):
        await set_grant(service, agent, actor)
    assert await service.current(agent) is None


async def test_expired_grant_cannot_be_persisted(owned):
    service, agent, actor, _ = owned
    await enable(service, agent, actor)
    with pytest.raises(AgentBackendRefused):
        await set_grant(
            service,
            agent,
            actor,
            grant().model_copy(
                update={"limits": grant().limits.model_copy(update={"deadline": NOW})}
            ),
        )
    assert await service.current(agent) is None


def _capped(*caps: str):
    return grant().model_copy(
        update={
            "limits": grant().limits.model_copy(
                update={
                    "provider_limits_usd": dict(
                        zip(("anthropic", "openai", "gemini"), map(Decimal, caps), strict=True)
                    )
                }
            )
        }
    )


async def test_provider_caps_above_the_run_budget_are_refused_at_preview(owned):
    """Fleet r6: caps of 3 x $1.20 under a $2 run passed the grant, then failed every spawn."""
    service, agent, actor, _ = owned
    await enable(service, agent, actor)
    over = _capped("0.40", "0.40", "0.40")  # $1.20 of caps under the $0.90 run budget
    with pytest.raises(AgentBackendRefused, match="fleet provider limits exceed budget"):
        await service.preview_fleet_grant(agent, over, now=NOW)
    with pytest.raises(AgentBackendRefused, match="fleet provider limits exceed budget"):
        await service.set_fleet_grant(
            agent, over, 0, actor, expected_preview_digest="0" * 64, now=NOW
        )
    assert await service.current(agent) is None
    exact = await set_grant(service, agent, actor, _capped("0.30", "0.30", "0.30"))
    assert exact.fleet_grant["limits"]["provider_limits_usd"]["gemini"] == "0.30"


@pytest.mark.parametrize("fault", ["provider", "model", "duplicate", "wildcard", "options"])
def test_grant_choices_are_exact_pairs_not_a_cartesian_product(fault):
    value = grant().model_dump(mode="json")
    choices = value["choices"]
    if fault == "provider":
        choices[2][1]["provider"] = "openai"
    elif fault == "model":
        choices[2][1]["id"] = "gemini-3.8-flash"
    elif fault == "duplicate":
        choices.append(choices[0])
    elif fault == "wildcard":
        choices[0][1]["id"] = "*"
    else:
        choices[0][1]["options"] = {"ungranted": True}
    import json

    with pytest.raises(ValueError):
        AgentFleetGrant.model_validate_json(json.dumps(value))


@pytest.mark.parametrize(
    "field", ["max_workers", "max_run_seconds", "max_worker_seconds", "max_root_turns"]
)
@pytest.mark.parametrize("invalid", [0, -1, True, 1.5])
def test_grant_integer_ceilings_are_finite_and_strict(field, invalid):
    import json

    value = grant().model_dump(mode="json")
    value[field] = invalid
    with pytest.raises(ValueError):
        AgentFleetGrant.model_validate_json(json.dumps(value))


@pytest.mark.parametrize("fault", ["shape", "pending_epoch", "future_epoch"])
def test_invalid_persisted_grant_is_unavailable_without_breaking_backend_facts(fault):
    import uuid

    from structlog.testing import capture_logs

    value = grant().model_copy(update={"epoch": 1}).model_dump(mode="json")
    if fault == "shape":
        value = {"unexpected": "must-not-be-logged"}
    else:
        value["epoch"] = 0 if fault == "pending_epoch" else 9
    with capture_logs() as logs:
        revision = AgentBackendRevision(
            tenant_id=uuid.uuid4(),
            agent_id=uuid.uuid4(),
            local=1,
            backend="openai",
            agent_name="helper",
            preview_digest="preview",
            digest="digest",
            previous_local=None,
            created_at=NOW,
            fleet_grant=value,
        )
    assert revision.backend == "openai" and revision.fleet_grant is None
    assert revision.fleet_grant_invalid
    assert "fleet_grant_invalid" not in revision.model_dump()
    assert len(logs) == 1
    assert "must-not-be-logged" not in str(logs)


def test_grant_json_roundtrip_preserves_decimal_and_expiry():
    value = grant().model_copy(update={"epoch": 7})
    parsed = AgentFleetGrant.model_validate_json(value.model_dump_json())
    assert parsed == value
    assert isinstance(parsed.limits.budget_usd, Decimal)
    assert parsed.expires_at == NOW + timedelta(hours=1)
    assert parsed.epoch == 7


@pytest.mark.parametrize("invalid", ["0", "-0.01", "NaN", "Infinity", "-Infinity"])
def test_grant_authority_budget_is_positive_and_finite(invalid):
    import json

    value = grant().model_dump(mode="json")
    value["limits"]["budget_usd"] = invalid
    with pytest.raises(ValueError):
        AgentFleetGrant.model_validate_json(json.dumps(value))


def test_grant_expiry_requires_a_timezone_and_worker_duration_is_attenuated():
    import json

    value = grant().model_dump(mode="json")
    value["limits"]["deadline"] = "2026-10-13T22:00:00"
    with pytest.raises(ValueError):
        AgentFleetGrant.model_validate_json(json.dumps(value))
    value = grant().model_dump(mode="json")
    value["max_worker_seconds"] = value["max_run_seconds"] + 1
    with pytest.raises(ValueError):
        AgentFleetGrant.model_validate_json(json.dumps(value))


async def test_disabled_fleet_grant_read_has_no_database_access():
    import uuid

    from daimon.core.channel_backend import AgentBackendService

    def forbidden_database():
        raise AssertionError("disabled fleet grants accessed the database")

    service = AgentBackendService(forbidden_database, enabled=False)
    assert await service.read_fleet_grant(uuid.uuid4(), uuid.uuid4(), now=NOW) is None


async def test_no_grant_backend_digest_preserves_existing_bytes(owned):
    from daimon.core.channel_backend import _digest

    service, agent, actor, _ = owned
    await enable(service, agent, actor)
    revision = await service.set_backend(agent, "openai", 0, actor)
    assert revision.fleet_grant is None
    assert revision.digest == _digest(
        {"tenant_id": str(agent.tenant_id), "agent_id": str(agent.agent_id), "backend": "openai"}
    )
    assert "fleet_grant" not in revision.model_dump(mode="json")


async def test_policy_change_invalidates_grant_preview_without_writes(owned):
    service, agent, actor, _ = owned
    await enable(service, agent, actor)
    value = grant()
    preview = await service.preview_fleet_grant(agent, value, now=NOW)
    policy = await service.tenant_policy(agent.tenant_id)
    await service.set_tenant_allowlist(agent.tenant_id, ("anthropic",), policy.revision, actor)
    with pytest.raises(AgentBackendRefused):
        await service.set_fleet_grant(
            agent, value, 0, actor, expected_preview_digest=preview.digest, now=NOW
        )
    assert await service.current(agent) is None


async def test_grant_revoke_still_works_after_backend_allowlist_is_narrowed(owned):
    service, agent, actor, _ = owned
    await enable(service, agent, actor)
    granted = await set_grant(service, agent, actor)
    await service.set_backend(agent, "openai", granted.local, actor)
    policy = await service.tenant_policy(agent.tenant_id)
    await service.set_tenant_allowlist(agent.tenant_id, ("anthropic",), policy.revision, actor)
    revoked = await revoke_grant(service, agent, actor)
    assert revoked.backend == "openai" and revoked.fleet_grant == FLEET_OFF
    assert await service.read_fleet_grant(agent.tenant_id, agent.agent_id, now=NOW) is None


async def test_grant_read_is_tenant_and_agent_scoped(owned):
    import uuid

    service, agent, actor, _ = owned
    await enable(service, agent, actor)
    await set_grant(service, agent, actor)
    assert await service.read_fleet_grant(uuid.uuid4(), agent.agent_id, now=NOW) is None
    assert await service.read_fleet_grant(agent.tenant_id, uuid.uuid4(), now=NOW) is None


@pytest.mark.parametrize("operation", ["set", "clear", "revert"])
@pytest.mark.parametrize("grant_first", [False, True])
async def test_mixed_backend_and_grant_cas_has_one_shared_winner(owned, operation, grant_first):
    service, agent, actor, _ = owned
    await enable(service, agent, actor)
    first = await service.set_backend(agent, "openai", 0, actor)
    second = await service.set_backend(agent, "gemini", first.local, actor)
    current = await set_grant(service, agent, actor)
    value = None if operation == "clear" else grant()
    preview = await service.preview_fleet_grant(agent, value, now=NOW)

    async def backend_write():
        if operation == "set":
            return await service.set_backend(agent, "anthropic", current.local, actor)
        if operation == "clear":
            return await service.clear_backend(agent, current.local, actor)
        return await service.revert(agent, actor, expected_revision=current.local)

    async def grant_write():
        if value is None:
            return await service.revoke_fleet_grant(
                agent, current.local, actor, expected_preview_digest=preview.digest, now=NOW
            )
        return await service.set_fleet_grant(
            agent, value, current.local, actor, expected_preview_digest=preview.digest, now=NOW
        )

    writes = (grant_write(), backend_write()) if grant_first else (backend_write(), grant_write())
    results = await asyncio.gather(*writes, return_exceptions=True)
    if grant_first:
        results.reverse()
    failures = [result for result in results if isinstance(result, AgentBackendRefused)]
    assert len(failures) == 1 and failures[0].reason == "stale_revision"
    winner = results[0] if not isinstance(results[0], Exception) else results[1]
    latest = await service.current(agent)
    assert latest == winner and latest.local == current.local + 1
    if not isinstance(results[0], Exception):
        assert latest.previous_local == current.local
        assert latest.fleet_grant == current.fleet_grant
        assert (
            latest.backend
            == {"set": "anthropic", "clear": None, "revert": first.backend}[operation]
        )
    else:
        assert latest.backend == second.backend and latest.previous_local == current.previous_local
        assert (
            latest.fleet_grant == FLEET_OFF
            if value is None
            else latest.fleet_grant["epoch"] == latest.local
        )
    assert len(await service.audit(agent.tenant_id, agent_id=agent.agent_id)) == 4


@pytest.mark.parametrize("operation", ["backend", "grant_set", "grant_revoke"])
async def test_all_mutations_translate_insert_conflicts_and_leave_no_audit(
    owned, monkeypatch, operation
):
    from daimon.core.stores import agent_backends
    from sqlalchemy.exc import IntegrityError

    service, agent, actor, _ = owned
    await enable(service, agent, actor)

    async def conflict(*args, **kwargs):
        raise IntegrityError("offline insert", {}, RuntimeError("same revision"))

    monkeypatch.setattr(agent_backends, "put_revision", conflict)
    with pytest.raises(AgentBackendRefused) as refused:
        if operation == "backend":
            await service.set_backend(agent, "openai", 0, actor)
        elif operation == "grant_set":
            await set_grant(service, agent, actor)
        else:
            await revoke_grant(service, agent, actor)
    assert refused.value.reason == "stale_revision"
    assert await service.current(agent) is None
    assert await service.audit(agent.tenant_id, agent_id=agent.agent_id) == ()


@pytest.mark.parametrize("revoke", [False, True])
async def test_grant_preview_remains_mandatory_in_shared_mutation(owned, revoke):
    service, agent, actor, _ = owned
    await enable(service, agent, actor)
    with pytest.raises(AgentBackendRefused) as refused:
        if revoke:
            await service.revoke_fleet_grant(agent, 0, actor, expected_preview_digest=None, now=NOW)
        else:
            await service.set_fleet_grant(
                agent, grant(), 0, actor, expected_preview_digest=None, now=NOW
            )
    assert refused.value.reason == "stale_preview"
    assert await service.current(agent) is None
    assert await service.audit(agent.tenant_id, agent_id=agent.agent_id) == ()


async def test_backend_append_after_malformed_grant_revokes_without_rewriting_history(owned):
    from sqlalchemy import select

    service, agent, actor, sessions = owned
    await enable(service, agent, actor)
    granted = await set_grant(service, agent, actor)
    invalid = {"unknown-grant-shape": True}
    async with sessions.begin() as db:
        await db.execute(
            update(AgentBackendRevisionRecord)
            .where(
                AgentBackendRevisionRecord.tenant_id == agent.tenant_id,
                AgentBackendRevisionRecord.agent_id == agent.agent_id,
                AgentBackendRevisionRecord.local == granted.local,
            )
            .values(fleet_grant=invalid)
        )
    changed = await service.set_backend(agent, "openai", granted.local, actor)
    assert changed.backend == "openai" and changed.fleet_grant is None
    assert not changed.fleet_grant_invalid
    assert await service.read_fleet_grant(agent.tenant_id, agent.agent_id, now=NOW) is None
    async with sessions() as db:
        retained = await db.scalar(
            select(AgentBackendRevisionRecord.fleet_grant).where(
                AgentBackendRevisionRecord.tenant_id == agent.tenant_id,
                AgentBackendRevisionRecord.agent_id == agent.agent_id,
                AgentBackendRevisionRecord.local == granted.local,
            )
        )
        assert retained == invalid


async def test_root_controls_use_the_existing_audited_grant_and_preserve_worker_limits(owned):
    service, agent, actor, _ = owned
    await enable(service, agent, actor)
    value = grant().model_copy(
        update={"native_root_budget_usd": Decimal("0.20"), "root_cash_budget_usd": Decimal("1.00")}
    )
    value = AgentFleetGrant.model_validate_json(value.model_dump_json())
    revision = await set_grant(service, agent, actor, value)
    loaded = await service.read_fleet_grant(agent.tenant_id, agent.agent_id, now=NOW)
    assert loaded is not None
    assert loaded.native_root_budget_usd == Decimal("0.20")
    assert loaded.root_cash_budget_usd == Decimal("1.00")
    assert loaded.limits == grant().limits
    assert loaded.epoch == revision.local
    assert loaded.model_dump(mode="json") == revision.fleet_grant
    audit = await service.audit(agent.tenant_id, agent_id=agent.agent_id)
    assert len(audit) == 1 and audit[0].outcome == "allowed"
    await revoke_grant(service, agent, actor)
    assert await service.read_fleet_grant(agent.tenant_id, agent.agent_id, now=NOW) is None


def test_omitted_root_controls_preserve_the_existing_grant_encoding():
    value = grant()
    serialized = value.model_dump(mode="json")
    assert set(serialized) == {
        "epoch",
        "max_workers",
        "choices",
        "limits",
        "max_run_seconds",
        "max_worker_seconds",
        "max_root_turns",
    }
    assert (
        AgentFleetGrant.model_validate_json(value.model_dump_json()).model_dump(mode="json")
        == serialized
    )
    assert "native_budget_usd" not in serialized["limits"]


@pytest.mark.parametrize(
    "changes",
    [
        {"native_root_budget_usd": Decimal("0")},
        {"native_root_budget_usd": Decimal("-0.01")},
        {"native_root_budget_usd": Decimal("NaN")},
        {"native_root_budget_usd": Decimal("Infinity")},
        {"native_root_budget_usd": Decimal("0.001")},
        {"native_root_budget_usd": Decimal("10000000000.00")},
        {"native_root_budget_usd": "0.20"},
        {"native_root_budget_usd": 0.20},
        {"native_root_budget_usd": True},
        {"root_cash_budget_usd": Decimal("0")},
        {"root_cash_budget_usd": Decimal("-1")},
        {"root_cash_budget_usd": Decimal("NaN")},
        {"root_cash_budget_usd": Decimal("Infinity")},
        {"root_cash_budget_usd": "1.00"},
        {"root_cash_budget_usd": 1.00},
        {"root_cash_budget_usd": True},
    ],
)
def test_root_budget_pair_refuses_partial_and_malformed_operator_inputs(changes):
    data = grant().model_dump()
    data.update(native_root_budget_usd=Decimal("0.20"), root_cash_budget_usd=Decimal("1.00"))
    with pytest.raises(ValidationError):
        AgentFleetGrant.model_validate({**data, **changes})


@pytest.mark.parametrize("turns", [2, 3])
def test_shared_native_budget_annotation_keeps_single_input_worker_dispatch(turns):
    from daimon.core.neutral_resources import WorkerSpecRefused

    limits = WorkerLimits(
        budget_usd=Decimal("1.00"),
        deadline=NOW + timedelta(seconds=120),
        max_turns=turns,
        native_budget_usd=Decimal("0.20"),
    )
    with pytest.raises(WorkerSpecRefused) as refused:
        limits.require_native_dispatch(NOW)
    assert (refused.value.field, refused.value.reason) == ("max_turns", "not_subset")


@pytest.mark.parametrize("field", ["native_root_budget_usd", "root_cash_budget_usd"])
def test_root_budget_pair_requires_both_operator_fields(field):
    data = grant().model_dump()
    data[field] = Decimal("0.20")
    with pytest.raises(ValidationError, match="supplied together"):
        AgentFleetGrant.model_validate(data)


async def test_argon_fleet_grant_is_accepted_only_inside_its_price_window(owned, monkeypatch):
    from daimon.core import pricing

    clock = {"now": datetime(2026, 12, 1, tzinfo=UTC)}

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"]

    monkeypatch.setattr(pricing, "datetime", Clock)
    service, agent, actor, _ = owned
    await enable(service, agent, actor)
    argon = (
        backend_selection("gemini").model_copy(update={"model": "gemini-4-argon"}),
        ModelRef(provider="gemini", id="gemini-4-argon"),
    )
    # After its price window Argon has no tariff, so the grant refuses up front.
    with pytest.raises(AgentBackendRefused, match="invalid fleet grant"):
        await set_grant(service, agent, actor, grant().model_copy(update={"choices": (argon,)}))
    assert await service.current(agent) is None
    # Inside it, a worker grant may name Argon (it runs on the bounded envelope).
    clock["now"] = datetime(2026, 11, 30, tzinfo=UTC)
    await set_grant(service, agent, actor, grant().model_copy(update={"choices": (argon,)}))
    assert await service.current(agent) is not None


async def test_turning_fleet_on_refuses_an_agent_whose_setup_cant_run_one(captured, owned):
    from daimon.core.channel_backend import AgentBackendService
    from daimon.core.fleet_configuration import configuration_refusal
    from mux.contracts.resources import ResourceBinding

    source, _, value, _, _, _ = captured
    _, _, _, sessions = owned
    login = "agent-mcp:internal-login"
    with_login = source.model_copy(
        update={
            "resource_requirements": (
                *source.resource_requirements,
                ResourceBinding(id=login, kind="credential", credential_ref=login),
            )
        }
    )
    captures = []

    async def capture(agent):
        captures.append(agent)
        return with_login

    from .test_fleet_source_permissions import NOW as now

    service = AgentBackendService(sessions, enabled=True, capture_source=capture)
    with pytest.raises(AgentBackendRefused) as refused:
        await service.preview_fleet_grant(source, value, now=now, check_root=True)
    assert refused.value.reason == "fleet_root_credentials"
    assert configuration_refusal(refused.value.reason) == (
        "This agent can't run a fleet yet: it uses a connected tool or repository "
        "with its own login."
    )
    # Showing or revoking never captures; an agent that can run a fleet previews.
    await service.preview_fleet_grant(source, value, now=now)
    await service.preview_fleet_grant(source, None, now=now, check_root=True)
    assert captures == [source]

    async def plain(agent):
        return source

    fine = AgentBackendService(sessions, enabled=True, capture_source=plain)
    await fine.preview_fleet_grant(source, value, now=now, check_root=True)
