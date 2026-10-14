"""Per-agent fleet ceilings use the real preference CAS and audit owners."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from daimon.core._models import AgentBackendRevisionRecord
from daimon.core.authz import Subject
from daimon.core.channel_backend import AgentBackendRefused
from daimon.core.neutral_resources import AgentFleetGrant, WorkerLimits
from daimon.core.stores import security_audit
from daimon.core.stores.domain import AgentBackendRevision
from mux.contracts.ids import ModelRef
from mux.profiles import backend_selection
from sqlalchemy import update

from ..test_agent_backend import enable
from ..test_agent_backend import owned as owned

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
    assert revoked.fleet_grant is None
    reverted = await service.revert(agent, actor, expected_revision=revoked.local)
    assert reverted.backend is None and reverted.fleet_grant is None
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


@pytest.mark.parametrize("fault", ["tenant", "account", "channel_admin"])
async def test_foreign_or_insufficient_grant_actor_refuses(owned, fault):
    import uuid

    service, agent, actor, _ = owned
    await enable(service, agent, actor)
    if fault == "tenant":
        actor = replace(actor, tenant_id=uuid.uuid4())
    elif fault == "account":
        actor = replace(actor, account_id=uuid.uuid4())
    else:
        actor = replace(actor, subject=Subject(administered_channel_ids=frozenset({"chan"})))
    with pytest.raises(AgentBackendRefused):
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
    assert revoked.backend == "openai" and revoked.fleet_grant is None
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
            latest.fleet_grant is None
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
