"""Tests for the ApplyReport bucket routing and is_failure semantics."""

from __future__ import annotations

from daimon.core.defaults.report import Action, ApplyReport, ResourceOutcome


def test_apply_report_routes_system_config_outcome_to_its_bucket() -> None:
    report = ApplyReport()
    outcome = ResourceOutcome(kind="system_config", name="system", action=Action.UPDATED)
    report.add(outcome)
    assert report.system_config == [outcome]
    assert report.agents == []
    assert report.environments == []
    assert report.skills == []


def test_apply_report_is_failure_covers_system_config_bucket() -> None:
    report = ApplyReport()
    report.add(
        ResourceOutcome(kind="system_config", name="system", action=Action.FAILED, error="boom")
    )
    assert report.is_failure() is True
