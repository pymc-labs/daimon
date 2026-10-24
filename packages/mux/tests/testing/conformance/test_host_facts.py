"""Host-fact controls exercise the shared oracle, never certify a provider."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Mapping
from typing import cast

import pytest
from mux.testing.conformance.reference import create
from mux.testing.conformance.runner import Adapter, PendingKind, PendingReason, Result, run_fixture

HOOKS = {
    "C14": "host_backend_selection_evidence",
    "C17": "host_wake_evidence",
    "C18": "host_termination_evidence",
}
PENDING = {
    "C14": "N4/N10 existing/new-thread backend selection and registry seam pending",
    "C17": "N5 host wake generation adapter pending; StateStore fencing alone is not a wake probe",
    "C18": "N4/N8 host TerminationReason/outcome-row adapter pending; mux cannot import daimon",
}


def selection() -> dict[str, object]:
    first = ("openai.persistent_workspace", 1, "original-session", "openai")
    new = ("anthropic.managed_agents", 2, "new-session", "anthropic")
    return {
        "original_config": first[:2],
        "changed_config": new[:2],
        "first": first,
        "existing": first,
        "new_thread": new,
        "first_binding": first,
        "existing_binding": first,
        "new_binding": new,
        "missing_original_refusal": "backend_unsupported",
        "missing_original_mutations": 0,
    }


def wake() -> dict[str, object]:
    return {
        "before_generation": 1,
        "current_generation": 2,
        "replacement_generation": 2,
        "current_session": "replacement-session",
        "stale_binding_refused": True,
        "stale_pump_calls": 0,
        "stale_wake_started": False,
        "stale_wake_settled": False,
        "wake_started": True,
        "wake_settled": True,
        "wake_status": "delivered",
        "pump_calls": 1,
        "lease_released": True,
        "journal_event_present": True,
    }


def terminations() -> dict[str, object]:
    reasons = ("completed", "interrupted", "upstream", "session_terminated", "rate_limited")
    return {
        "required_reasons": reasons,
        "terminations": tuple(
            {
                "case": f"case-{reason}",
                "outcome_id": f"observation-{reason}",
                "persisted_rows": 1,
                "expected_error_class": None,
                "actual_error_class": None,
                "persisted_error_class": None,
                "expected_tenant": "authorized-tenant",
                "persisted_tenant": "authorized-tenant",
                "expected_platform": "discord",
                "persisted_platform": "discord",
                "expected_recovered": False,
                "actual_recovered": False,
                "persisted_recovered": False,
                "expected_reason": reason,
                "actual_reason": reason,
                "persisted_reason": reason,
                "expected_session": f"session-{reason}",
                "actual_session": f"session-{reason}",
                "persisted_session": f"session-{reason}",
                "expected_account": "authorized-account",
                "persisted_account": "authorized-account",
            }
            for reason in reasons
        ),
    }


FACTORIES = {"C14": selection, "C17": wake, "C18": terminations}


async def observe(fid: str, facts: object) -> Result:
    adapter = create()

    async def hook() -> object:
        return facts

    setattr(adapter.transport, HOOKS[fid], hook)
    return await run_fixture(fid, adapter)


@pytest.mark.parametrize("fid", HOOKS)
@pytest.mark.parametrize("absence", ["missing", "none_attribute", "none_result"])
async def test_absence_retains_exact_pending_reason(fid: str, absence: str) -> None:
    if absence == "none_result":
        result = await observe(fid, None)
    else:
        adapter = create()
        if absence == "none_attribute":
            setattr(adapter.transport, HOOKS[fid], None)
        result = await run_fixture(fid, adapter)
    assert result.status == "pending" and result.evidence == (PENDING[fid],)


@pytest.mark.parametrize("fid", HOOKS)
async def test_complete_facts_exercise_the_oracle(fid: str) -> None:
    result = await observe(fid, FACTORIES[fid]())
    assert result.status == "pass" and result.evidence


@pytest.mark.parametrize("fid", HOOKS)
@pytest.mark.parametrize("malformed", [True, "callback-verdict-pass", (), {}])
async def test_present_invalid_facts_fail_instead_of_pending(fid: str, malformed: object) -> None:
    result = await observe(fid, malformed)
    assert result.status == "fail" and "callback-verdict-pass" not in str(result.evidence)


@pytest.mark.parametrize("fid", HOOKS)
async def test_noncallable_hook_fails(fid: str) -> None:
    adapter = create()
    setattr(adapter.transport, HOOKS[fid], True)
    result = await run_fixture(fid, adapter)
    assert result.status == "fail" and "must be callable" in result.evidence[0]


@pytest.mark.parametrize("fid", HOOKS)
async def test_declared_pending_still_prevents_dispatch(fid: str) -> None:
    adapter = create()

    async def forbidden() -> object:
        raise RuntimeError("pending adapter dispatched")

    setattr(adapter.transport, HOOKS[fid], forbidden)
    reason = PendingReason(PendingKind.ADAPTER_DEPENDENCY, "complete native envelope absent")
    adapter = Adapter(adapter.driver, adapter.store, adapter.transport, pending={fid: reason})
    result = await run_fixture(fid, adapter)
    assert result.status == "pending" and result.pending_reason == reason


@pytest.mark.parametrize(
    "fid,key", [(fid, key) for fid, factory in FACTORIES.items() for key in factory()]
)
async def test_every_required_fact_must_be_present(fid: str, key: str) -> None:
    facts = FACTORIES[fid]()
    del facts[key]
    assert (await observe(fid, facts)).status == "fail"


@pytest.mark.parametrize(
    "key,value",
    [
        ("original_config", ("openai.persistent_workspace", True)),
        ("changed_config", ("openai.persistent_workspace", 2)),
        ("changed_config", ("anthropic.managed_agents", 1)),
        ("first", ("openai.persistent_workspace", 1, "", "openai")),
        ("existing", ("anthropic.managed_agents", 2, "new-session", "anthropic")),
        ("existing_binding", ("openai.persistent_workspace", 2, "original-session", "openai")),
        ("existing_binding", ("openai.persistent_workspace", 1, "foreign-session", "openai")),
        ("first_binding", None),
        ("new_binding", ("openai.persistent_workspace", 1, "original-session", "openai")),
        ("missing_original_refusal", None),
        ("missing_original_mutations", 1),
        ("missing_original_mutations", False),
    ],
)
async def test_c14_retention_and_missing_driver_refusal_remain_required(
    key: str, value: object
) -> None:
    facts = selection()
    facts[key] = value
    assert (await observe("C14", facts)).status == "fail"


async def test_c14_disclosed_product_defect_is_a_failure_even_with_a_finding() -> None:
    facts = selection()
    facts["existing"] = facts["existing_binding"] = facts["new_thread"]
    facts["finding"] = {"kind": "existing_thread_binding_changed", "owner": "N4"}
    result = await observe("C14", facts)
    assert result.status == "fail"
    assert result.evidence == (
        "check failed: C14: existing thread must retain original native binding and revision",
    )


def wrong_provider_selection(changed: bool) -> dict[str, object]:
    facts = selection()
    if changed:
        facts["new_thread"] = facts["new_binding"] = (
            "anthropic.managed_agents",
            2,
            "original-session",
            "openai",
        )
    else:
        wrong = ("openai.persistent_workspace", 1, "original-session", "gemini")
        for key in ("first", "existing", "first_binding", "existing_binding"):
            facts[key] = wrong
    return facts


@pytest.mark.parametrize("changed", [False, True])
async def test_c14_selected_provider_must_match_native_and_persisted_provider(
    changed: bool,
) -> None:
    result = await observe("C14", wrong_provider_selection(changed))
    assert result.status == "fail"
    assert result.evidence == (
        "check failed: C14: selected profile provider must agree with prepared "
        "and persisted identities",
    )


async def test_c14_unknown_profile_cannot_certify_provider_agreement() -> None:
    facts = selection()
    facts["original_config"] = ("openai.undeclared", 1)
    for key in ("first", "existing", "first_binding", "existing_binding"):
        facts[key] = ("openai.undeclared", 1, "original-session", "openai")
    assert (await observe("C14", facts)).status == "fail"


@pytest.mark.parametrize(
    "key", ["first", "existing", "new_thread", "first_binding", "existing_binding", "new_binding"]
)
async def test_c14_identity_equality_does_not_accept_bool_or_float_revisions(key: str) -> None:
    for malformed in (True, 1.0, 2.0):
        facts = selection()
        value = cast(tuple[object, ...], facts[key])
        facts[key] = (value[0], malformed, *value[2:])
        assert (await observe("C14", facts)).status == "fail"


@pytest.mark.parametrize(
    "key,value",
    [
        ("before_generation", True),
        ("current_generation", 1),
        ("replacement_generation", 3),
        ("replacement_generation", 2.0),
        ("current_session", ""),
        ("stale_binding_refused", False),
        ("stale_pump_calls", 1),
        ("stale_pump_calls", False),
        ("stale_wake_started", True),
        ("stale_wake_settled", True),
        ("wake_started", False),
        ("wake_settled", False),
        ("wake_status", "pending"),
        ("pump_calls", 2),
        ("pump_calls", True),
        ("lease_released", False),
        ("journal_event_present", False),
    ],
)
async def test_c17_stale_ownership_and_actual_host_effects_cannot_pass(
    key: str, value: object
) -> None:
    facts = wake()
    facts[key] = value
    assert (await observe("C17", facts)).status == "fail"


@pytest.mark.parametrize(
    "key,value",
    [
        ("case", ""),
        ("outcome_id", ""),
        ("persisted_rows", 0),
        ("persisted_rows", 2),
        ("persisted_rows", True),
        ("actual_reason", None),
        ("expected_error_class", ""),
        ("actual_error_class", "ForeignError"),
        ("persisted_error_class", "ForeignError"),
        ("actual_session", "foreign-session"),
        ("expected_tenant", ""),
        ("persisted_tenant", "foreign-tenant"),
        ("expected_platform", ""),
        ("persisted_platform", "slack"),
        ("expected_recovered", True),
        ("actual_recovered", True),
        ("persisted_recovered", True),
        ("persisted_recovered", None),
        ("persisted_recovered", 0),
        ("expected_reason", "unknown-reason"),
        ("actual_reason", "interrupted"),
        ("persisted_reason", None),
        ("expected_session", ""),
        ("persisted_session", "foreign-session"),
        ("expected_account", ""),
        ("persisted_account", "foreign-account"),
    ],
)
async def test_c18_outcomes_must_retain_authorized_session_and_account(
    key: str, value: object
) -> None:
    facts = terminations()
    rows = cast(tuple[Mapping[str, object], ...], facts["terminations"])
    facts["terminations"] = ({**rows[0], key: value}, *rows[1:])
    assert (await observe("C18", facts)).status == "fail"


@pytest.mark.parametrize(
    "corruption", ["no_roots", "missing_case", "duplicate_case", "duplicate_reason"]
)
async def test_c18_independent_coverage_cannot_be_inferred_from_observations(
    corruption: str,
) -> None:
    facts = terminations()
    rows = cast(tuple[Mapping[str, object], ...], facts["terminations"])
    if corruption == "no_roots":
        facts["required_reasons"] = ("completed",)
        facts["terminations"] = rows[:1]
    elif corruption == "missing_case":
        facts["terminations"] = rows[:-1]
    elif corruption == "duplicate_case":
        facts["terminations"] = (*rows, rows[0])
    else:
        facts["required_reasons"] = (*cast(tuple[str, ...], facts["required_reasons"]), "completed")
    assert (await observe("C18", facts)).status == "fail"


@pytest.mark.parametrize("index", [0, 4])
@pytest.mark.parametrize(
    "key", list(cast(tuple[Mapping[str, object], ...], terminations()["terminations"])[0])
)
async def test_c18_every_snapshot_field_is_required_in_every_record(index: int, key: str) -> None:
    facts = terminations()
    rows = [dict(row) for row in cast(tuple[Mapping[str, object], ...], facts["terminations"])]
    del rows[index][key]
    facts["terminations"] = tuple(rows)
    assert (await observe("C18", facts)).status == "fail"


async def test_c18_legacy_minimal_rows_do_not_certify_committed_outcomes() -> None:
    facts = terminations()
    legacy_fields = {
        "case",
        "expected_reason",
        "actual_reason",
        "persisted_reason",
        "expected_session",
        "persisted_session",
        "expected_account",
        "persisted_account",
    }
    facts["terminations"] = tuple(
        {key: value for key, value in row.items() if key in legacy_fields}
        for row in cast(tuple[Mapping[str, object], ...], facts["terminations"])
    )
    assert (await observe("C18", facts)).status == "fail"


async def test_c18_fresh_observation_cannot_be_reused_between_cases() -> None:
    facts = terminations()
    rows = [dict(row) for row in cast(tuple[Mapping[str, object], ...], facts["terminations"])]
    rows[-1]["outcome_id"] = rows[0]["outcome_id"]
    facts["terminations"] = tuple(rows)
    assert (await observe("C18", facts)).status == "fail"


def null_sessions(reason: str | None) -> dict[str, object]:
    facts = terminations()
    facts["terminations"] = tuple(
        {
            **row,
            "expected_session": None,
            "actual_session": None,
            "persisted_session": None,
        }
        if reason is None or row["expected_reason"] == reason
        else dict(row)
        for row in cast(tuple[Mapping[str, object], ...], facts["terminations"])
    )
    return facts


@pytest.mark.parametrize("reason", [None, "completed", "session_terminated"])
async def test_c18_native_terminal_outcomes_cannot_claim_null_sessions(reason: str | None) -> None:
    result = await observe("C18", null_sessions(reason))
    if result.status != "fail":
        raise AssertionError("C18 accepted missing native session retention")


def queued_stop() -> dict[str, object]:
    """wait_for_slot records a cancelled first turn before native preparation."""
    facts = terminations()
    rows = cast(tuple[Mapping[str, object], ...], facts["terminations"])
    interrupted = next(row for row in rows if row["expected_reason"] == "interrupted")
    facts["terminations"] = (
        *rows,
        {
            **interrupted,
            "case": "queued-first-turn-stop",
            "outcome_id": "queued-stop-observation",
            "expected_platform": "slack",
            "persisted_platform": "slack",
            "expected_session": None,
            "actual_session": None,
            "persisted_session": None,
        },
    )
    return facts


async def test_c18_queued_stop_truthfully_retains_no_native_session() -> None:
    if (await observe("C18", queued_stop())).status != "pass":
        raise AssertionError("C18 refused a queued Stop before native preparation")


@pytest.mark.parametrize("key", ["actual_session", "persisted_session"])
async def test_c18_queued_stop_cannot_invent_a_native_session(key: str) -> None:
    facts = queued_stop()
    rows = cast(tuple[Mapping[str, object], ...], facts["terminations"])
    facts["terminations"] = (*rows[:-1], {**rows[-1], key: "phantom-session"})
    if (await observe("C18", facts)).status != "fail":
        raise AssertionError("C18 accepted a phantom native session for a queued Stop")


async def test_c18_nullable_pre_native_session_is_explicit_and_retained() -> None:
    facts = terminations()
    rows = [dict(row) for row in cast(tuple[Mapping[str, object], ...], facts["terminations"])]
    before_native = {
        **rows[-1],
        "case": "pre-native-refusal",
        "outcome_id": "new-pre-native-observation",
        "expected_reason": "missing_config",
        "actual_reason": "missing_config",
        "persisted_reason": "missing_config",
        "expected_session": None,
        "actual_session": None,
        "persisted_session": None,
    }
    facts["required_reasons"] = (
        *cast(tuple[str, ...], facts["required_reasons"]),
        "missing_config",
    )
    facts["terminations"] = (*rows, before_native)
    assert (await observe("C18", facts)).status == "pass"
    for key in ("actual_session", "persisted_session"):
        facts["terminations"] = (*rows, {**before_native, key: "phantom-session"})
        assert (await observe("C18", facts)).status == "fail"


async def test_c18_error_and_recovery_facts_are_independent_of_the_reason() -> None:
    facts = terminations()
    rows = [dict(row) for row in cast(tuple[Mapping[str, object], ...], facts["terminations"])]
    row = {
        **rows[-1],
        "expected_error_class": "RateLimitError",
        "actual_error_class": "RateLimitError",
        "persisted_error_class": "RateLimitError",
        "expected_recovered": True,
        "actual_recovered": True,
        "persisted_recovered": True,
    }
    facts["terminations"] = (*rows[:-1], row)
    assert (await observe("C18", facts)).status == "pass"
    for key, value in (
        ("actual_error_class", None),
        ("persisted_error_class", None),
        ("actual_recovered", False),
        ("persisted_recovered", False),
    ):
        facts["terminations"] = (*rows[:-1], {**row, key: value})
        assert (await observe("C18", facts)).status == "fail"


def test_optimized_python_retains_oracle_checks() -> None:
    code = """
import asyncio
from packages.mux.tests.testing.conformance.test_host_facts import (
    FACTORIES, null_sessions, observe, queued_stop, wrong_provider_selection,
)
async def main():
    for fid, factory in FACTORIES.items():
        if (await observe(fid, None)).status != 'pending':
            raise RuntimeError('absent facts certified')
        if (await observe(fid, factory())).status != 'pass':
            raise RuntimeError('valid oracle control refused')
        for key in factory():
            facts = factory()
            del facts[key]
            if (await observe(fid, facts)).status != 'fail':
                raise RuntimeError('optimized oracle accepted missing fact')
    for reason in (None, 'completed', 'session_terminated'):
        if (await observe('C18', null_sessions(reason))).status != 'fail':
            raise RuntimeError('optimized oracle certified null native sessions')
    if (await observe('C18', queued_stop())).status != 'pass':
        raise RuntimeError('optimized oracle refused a truthful queued Stop')
    for key in ('actual_session', 'persisted_session'):
        facts = queued_stop()
        rows = facts['terminations']
        facts['terminations'] = (*rows[:-1], {**rows[-1], key: 'phantom-session'})
        if (await observe('C18', facts)).status != 'fail':
            raise RuntimeError('optimized oracle certified a phantom queued-Stop session')
    good = FACTORIES['C18']()
    for index, row in enumerate(good['terminations']):
        for key in row:
            facts = FACTORIES['C18']()
            rows = [dict(item) for item in facts['terminations']]
            del rows[index][key]
            facts['terminations'] = tuple(rows)
            if (await observe('C18', facts)).status != 'fail':
                raise RuntimeError('optimized oracle accepted missing snapshot field')
    for changed in (False, True):
        if (await observe('C14', wrong_provider_selection(changed))).status != 'fail':
            raise RuntimeError('selected provider disagreement certified')
    facts = FACTORIES['C14']()
    facts['existing'] = facts['new_thread']
    if (await observe('C14', facts)).status != 'fail':
        raise RuntimeError('retention regression hidden')
asyncio.run(main())
"""
    result = subprocess.run(
        [sys.executable, "-O", "-c", code], capture_output=True, text=True, timeout=180, check=False
    )
    assert result.returncode == 0, result.stderr
