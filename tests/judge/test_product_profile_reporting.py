"""Separate default/opt-in tallies, frozen matrices and explicit fixture execution."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from daimon.testing import product_profiles as profiles
from daimon.testing.outcome_oracle import CheckResult, OutcomeReport, RunEvidence
from pydantic import BaseModel

from .catalog_runner import FROZEN_TARGET_SHA256, PROVIDERS
from .headless_executor import (
    CatalogProductBindings,
    CellResult,
    ExecutionReport,
    ProductProfileReport,
    execute_catalog_profiles,
    run_product_profiles,
)

HEAD = "a" * 40
TARGETS = tuple(
    Path(__file__).with_name("fixtures").joinpath("TARGET-53.txt").read_text().splitlines()
)


class McpFlags(BaseModel):
    require_routine_destination: bool = False


class DiscordFlags(BaseModel):
    credential_form_expiry: bool = False
    writers_none_notice: bool = False
    tenant_cap_waiting_card: bool = False


class TurnFlags(BaseModel):
    native_footer_cost: bool = False


class CoreFlags(BaseModel):
    turn: TurnFlags = TurnFlags()
    mcp: McpFlags = McpFlags()
    discord: DiscordFlags = DiscordFlags()


class HostFlags(BaseModel):
    html_link_errors: bool = False


def apply_flags(profile):
    configured = profile.apply(CoreFlags(), service="core")
    assert configured.turn.native_footer_cost is True
    assert configured.mcp.require_routine_destination is True
    assert configured.discord.credential_form_expiry is True
    assert configured.discord.writers_none_notice is True
    assert configured.discord.tenant_cap_waiting_card is True
    for service in ("notebook", "report"):
        host = HostFlags()
        assert profile.apply(host, service=service) is host


def board(*, first="FAIL", profile="default") -> ExecutionReport:
    cells = []
    for scenario in TARGETS:
        for backend in PROVIDERS:
            status = first if not cells else "PENDING"
            outcome = OutcomeReport(
                scenario_id=scenario,
                backend=backend,
                checks=(
                    CheckResult(kind="text_present", turn=1, status=status, code="TEST_TALLY"),
                ),
            )
            cells.append(
                CellResult(
                    scenario_id=scenario,
                    backend=backend,
                    scored=True,
                    source_sha256="b" * 64,
                    product_profile=profile,
                    gaps=(),
                    evidence=RunEvidence(scenario_id=scenario, backend=backend, turns=()),
                    catalog_outcome=outcome,
                    host_outcome=outcome,
                )
            )
    return ExecutionReport(
        integration_sha=HEAD,
        target_sha256=FROZEN_TARGET_SHA256,
        target_ids=TARGETS,
        cells=tuple(cells),
        product_profile=profile,
        applied_flags=tuple(profiles.OPT_IN_PRODUCT_PROFILE.flags)
        if profile == "opt-in-product"
        else (),
    )


def test_opt_in_pass_never_increases_default_counts_and_both_profiles_are_in_every_cell():
    result = ProductProfileReport(
        default_board=board(first="FAIL"),
        opt_in_board=board(first="PASS", profile="opt-in-product"),
    )
    assert result.scored_counts == {"PASS": 0, "FAIL": 1, "EXPECTED-FAIL": 0, "PENDING": 158}
    assert result.opt_in_scored_counts == {
        "PASS under opt-in profile": 1,
        "FAIL": 0,
        "EXPECTED-FAIL": 0,
        "PENDING": 158,
    }
    assert sum(result.scored_counts.values()) == sum(result.opt_in_scored_counts.values()) == 159
    payload = result.model_dump(mode="json")
    assert len(payload["cells"]) == 159
    for cell in payload["cells"]:
        assert set(cell["profiles"]) == {"default", "opt-in-product"}
        for name, observation in cell["profiles"].items():
            assert observation["product_profile"] == name
    assert payload["cells"][0]["profiles"]["default"]["reported_status"] == "FAIL"
    assert (
        payload["cells"][0]["profiles"]["opt-in-product"]["reported_status"] == profiles.OPT_IN_PASS
    )
    assert all(payload["profiles"]["opt-in-product"].values())
    assert not any(payload["profiles"]["default"].values())


@pytest.mark.parametrize("bad", ["profile", "head", "source", "missing", "duplicate"])
def test_cannot_combine_unmatched_or_incomplete_profile_boards(bad):
    default = board()
    opt_in = board(profile="opt-in-product")
    if bad == "profile":
        opt_in = board()
    elif bad == "head":
        opt_in = opt_in.model_copy(update={"integration_sha": "c" * 40})
    elif bad == "source":
        opt_in = opt_in.model_copy(
            update={
                "cells": (
                    opt_in.cells[0].model_copy(update={"source_sha256": "c" * 64}),
                    *opt_in.cells[1:],
                )
            }
        )
    else:
        data = opt_in.model_dump(exclude={"scored_counts"})
        for cell in data["cells"]:
            cell.pop("status")
            cell.pop("reported_status")
        data["cells"] = list(data["cells"][:-1])
        if bad == "duplicate":
            data["cells"].append(data["cells"][0])
        with pytest.raises(ValueError):
            ExecutionReport.model_validate(data)
        return
    with pytest.raises(ValueError):
        ProductProfileReport(default_board=default, opt_in_board=opt_in)


def test_default_board_rejects_any_opt_in_cell_even_with_a_plain_pass_status():
    default = board(first="PASS")
    with pytest.raises(ValueError):
        ExecutionReport(
            integration_sha=HEAD,
            target_sha256=FROZEN_TARGET_SHA256,
            target_ids=TARGETS,
            cells=(
                default.cells[0].model_copy(update={"product_profile": "opt-in-product"}),
                *default.cells[1:],
            ),
        )


async def test_one_matrix_job_runs_both_explicit_executors_and_restores_context(monkeypatch):
    monkeypatch.setattr(profiles.QAProductProfile, "require_available", lambda _self: None)
    seen = []

    async def default():
        seen.append(profiles.current_product_profile().name)
        return board(first="FAIL")

    async def opted(profile):
        assert profile == profiles.current_product_profile() == profiles.OPT_IN_PRODUCT_PROFILE
        seen.append(profile.name)
        apply_flags(profile)
        return board(first="PASS")

    result = await run_product_profiles(default, execute_opt_in=opted)
    assert seen == ["default", "opt-in-product"]
    assert result.scored_counts["PASS"] == 0
    assert result.opt_in_scored_counts[profiles.OPT_IN_PASS] == 1
    assert set(result.opt_in_board.applied_flags) == set(profiles.OPT_IN_PRODUCT_PROFILE.flags)
    assert profiles.current_product_profile() == profiles.DEFAULT_PRODUCT_PROFILE


async def test_context_without_real_setting_application_never_earns_an_opt_in_pass(monkeypatch):
    monkeypatch.setattr(profiles.QAProductProfile, "require_available", lambda _self: None)
    raw = board(first="PASS")
    raw = raw.model_copy(
        update={"cells": (raw.cells[0].model_copy(update={"request_count": 3}), *raw.cells[1:])}
    )
    result = await run_product_profiles(
        AsyncMock(return_value=board(first="FAIL")), execute_opt_in=AsyncMock(return_value=raw)
    )
    assert result.opt_in_board.profile_state == "unbound"
    assert result.opt_in_scored_counts[profiles.OPT_IN_PASS] == 0
    assert result.opt_in_scored_counts["PENDING"] == 159
    assert result.opt_in_board.cells[0].request_count == 3, "retain actual dispatch diagnostics"
    assert result.opt_in_board.cells[0].catalog_outcome.status == "PASS"
    assert result.opt_in_board.cells[0].reported_status == "PENDING"
    assert result.scored_counts["FAIL"] == 1 and result.scored_counts["PASS"] == 0


@pytest.mark.parametrize("missing_fields", [False, True])
async def test_missing_flags_or_fixture_binding_produce_no_opt_in_dispatch_or_default_credit(
    monkeypatch, missing_fields
):
    def available(self):
        if self.name == "opt-in-product" and missing_fields:
            raise profiles.ProductProfileUnavailable(("core.turn.native_footer_cost",))

    monkeypatch.setattr(profiles.QAProductProfile, "require_available", available)
    opted = AsyncMock(side_effect=AssertionError("must not dispatch"))
    result = await run_product_profiles(
        AsyncMock(return_value=board(first="PASS")),
        execute_opt_in=opted if missing_fields else None,
    )
    opted.assert_not_awaited()
    assert result.scored_counts["PASS"] == 1
    assert result.opt_in_scored_counts == {
        profiles.OPT_IN_PASS: 0,
        "FAIL": 0,
        "EXPECTED-FAIL": 0,
        "PENDING": 159,
    }
    assert all(not c.evidence.turns and c.request_count == 0 for c in result.opt_in_board.cells)
    assert all(c.gaps[0].code == "PRODUCT_PROFILE_UNAVAILABLE" for c in result.opt_in_board.cells)


@pytest.mark.parametrize(
    "error", [RuntimeError("partial execution"), profiles.ProductProfileUnavailable(("late",))]
)
async def test_partial_execution_failure_propagates_instead_of_inventing_a_zero_request_board(
    monkeypatch, error
):
    monkeypatch.setattr(profiles.QAProductProfile, "require_available", lambda _self: None)
    with pytest.raises(type(error)):
        await run_product_profiles(
            AsyncMock(return_value=board()), execute_opt_in=AsyncMock(side_effect=error)
        )
    assert profiles.current_product_profile() == profiles.DEFAULT_PRODUCT_PROFILE


async def test_catalog_job_builds_and_closes_fresh_surfaces_inside_each_profile(monkeypatch):
    from . import headless_executor as executor

    monkeypatch.setattr(profiles.QAProductProfile, "require_available", lambda _self: None)
    order = []
    actual = []

    @asynccontextmanager
    async def factories(profile):
        if profiles.current_product_profile() != profile:
            raise RuntimeError("factory did not receive its active profile")
        order.append(("open", profile.name))
        if profile.name == "opt-in-product":
            apply_flags(profile)
        instance = CatalogProductBindings(profile=profile)
        actual.append(instance)
        try:
            yield instance
        finally:
            order.append(("close", profile.name))

    async def execute(_root, **kwargs):
        profile = profiles.current_product_profile()
        order.append(("execute", profile.name))
        if kwargs["run_id"] != "same-job" or kwargs["admin_runs"]:
            raise RuntimeError("catalog inputs changed between profiles")
        return board(first="FAIL" if profile.name == "default" else "PASS")

    monkeypatch.setattr(executor, "execute_catalog", execute)
    result = await execute_catalog_profiles(
        Path("unused"), integration_sha=HEAD, run_id="same-job", replays=(), bindings=factories
    )
    if (
        order
        != [
            ("open", "default"),
            ("execute", "default"),
            ("close", "default"),
            ("open", "opt-in-product"),
            ("execute", "opt-in-product"),
            ("close", "opt-in-product"),
        ]
        or actual[0] is actual[1]
    ):
        raise RuntimeError("profile surfaces were not fresh independent executions")
    if result.scored_counts["PASS"] or result.opt_in_scored_counts[profiles.OPT_IN_PASS] != 1:
        raise RuntimeError("opt-in outcome contaminated the default tally")
    if profiles.current_product_profile() != profiles.DEFAULT_PRODUCT_PROFILE:
        raise RuntimeError("profile context leaked outside the job")


@pytest.mark.parametrize("corruption", ["foreign_profile", "shared_observer", "factory_failure"])
async def test_catalog_factory_refuses_cross_profile_surfaces_and_preserves_cleanup(
    monkeypatch, corruption
):
    from daimon.testing.http_observation import HTTPObserver

    from . import headless_executor as executor

    monkeypatch.setattr(profiles.QAProductProfile, "require_available", lambda _self: None)
    observer = HTTPObserver({}, nonce="unbound")
    closed = []
    dispatched = []

    @asynccontextmanager
    async def factories(profile):
        try:
            if profile.name == "opt-in-product" and corruption == "factory_failure":
                raise RuntimeError("partial factory refusal")
            yield CatalogProductBindings(
                profile=profiles.DEFAULT_PRODUCT_PROFILE
                if corruption == "foreign_profile"
                else profile,
                http_observer=observer if corruption == "shared_observer" else None,
            )
        finally:
            closed.append(profile.name)

    async def execute(_root, **_kwargs):
        dispatched.append(profiles.current_product_profile().name)
        return board()

    monkeypatch.setattr(executor, "execute_catalog", execute)
    with pytest.raises(
        RuntimeError if corruption == "factory_failure" else ValueError,
        match="factory refusal|different product profile|reused a mutable product surface",
    ):
        await execute_catalog_profiles(
            Path("unused"), integration_sha=HEAD, run_id="job", replays=(), bindings=factories
        )
    if dispatched != ["default"] or closed != ["default", "opt-in-product"]:
        raise RuntimeError("refused profile dispatched or failed to close its surfaces")
    if profiles.current_product_profile() != profiles.DEFAULT_PRODUCT_PROFILE:
        raise RuntimeError("refused factory leaked its profile context")


@pytest.mark.parametrize("corruption", ["target", "missing", "profile", "flags"])
def test_copied_execution_boards_still_validate_the_frozen_matrix(corruption):
    default = board()
    opted = board(profile="opt-in-product")
    if corruption == "target":
        default = default.model_copy(update={"target_sha256": "0" * 64})
    elif corruption == "missing":
        default = default.model_copy(update={"cells": default.cells[:-1]})
    elif corruption == "profile":
        default = default.model_copy(update={"product_profile": "opt-in-product"})
    else:
        opted = opted.model_copy(update={"applied_flags": ()})
    with pytest.raises(ValueError):
        ProductProfileReport(default_board=default, opt_in_board=opted)


def test_six_flag_receipt_cannot_certify_the_seven_switch_profile():
    opt_in = board(first="PASS", profile="opt-in-product")
    data = opt_in.model_dump(exclude={"scored_counts"})
    data["applied_flags"] = tuple(
        flag for flag in opt_in.applied_flags if flag != "core.discord.tenant_cap_waiting_card"
    )
    for cell in data["cells"]:
        cell.pop("status")
        cell.pop("reported_status")
    with pytest.raises(ValueError, match="opt-in profile requires all declared"):
        ExecutionReport.model_validate(data)
