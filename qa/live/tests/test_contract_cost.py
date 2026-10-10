from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from qa.live.config import Config, Pricing, Target
from qa.live.cost import Ledger, estimate
from qa.live.schema import Assertion, Scenario, Step, load_catalog
from qa.live.types import Usage, utcnow


@pytest.mark.parametrize(
    "values",
    [
        {"do": "mention"},
        {"do": "burst", "texts": []},
        {"do": "wait", "s": -1},
        {"do": "react", "emoji": "👍"},
        {"do": "new_unknown_kind"},
        {"do": "headless_interrupt", "text": "work"},
    ],
)
def test_invalid_steps_rejected(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Step.model_validate(values)


@pytest.mark.parametrize(
    "values",
    [
        {"kind": "text_present", "turn": 1},
        {"kind": "text_present", "turn": 1, "pattern": "["},
        {"kind": "done_within_s", "turn": 1},
        {"kind": "log_absent", "turn": 1},
        {"kind": "attachments", "turn": 1, "min": 2, "max": 1},
        {"kind": "interrupt_within_s"},
    ],
)
def test_invalid_assertions_rejected(values: dict[str, object]) -> None:
    with pytest.raises((ValueError, ExceptionGroup)):
        Assertion.model_validate(values)


def test_catalog_uses_external_directory_and_rejects_duplicates(
    tmp_path: Path, scenario: Scenario
) -> None:
    import yaml

    root = tmp_path / "scenarios"
    root.mkdir()
    value = yaml.safe_dump(scenario.model_dump(by_alias=True))
    (root / "one.yaml").write_text(value)
    assert load_catalog(tmp_path)[0].id == scenario.id
    (root / "two.yaml").write_text(value)
    with pytest.raises(ValueError, match="duplicate"):
        load_catalog(tmp_path)


def test_empty_catalog_and_human_checklist_fail(tmp_path: Path, scenario: Scenario) -> None:
    with pytest.raises(ValueError, match="no scenarios"):
        load_catalog(tmp_path)
    with pytest.raises(ValidationError, match="human checklist"):
        Scenario.model_validate(scenario.model_dump(by_alias=True) | {"set": "B"})


def test_trigger_count_canary_and_weekly_restart(scenario: Scenario) -> None:
    original = scenario.model_dump(by_alias=True)
    for changes in [
        {"est_turns": 1},
        {"assert": [{"kind": "text_present", "turn": 3, "pattern": "x"}]},
        {"setup": [{"do": "restart_workers"}]},
    ]:
        with pytest.raises(ValidationError):
            Scenario.model_validate(original | changes)


def test_only_exact_model_and_qa_guild(pricing: Pricing) -> None:
    with pytest.raises(ValidationError):
        Config.model_validate({"pricing": pricing.model_dump(), "model": "claude-sonnet-4-5"})
    with pytest.raises(ValidationError, match="QA guild"):
        Config(pricing=pricing, staging=Target(guild_id="customer"))
    with pytest.raises(ValidationError, match="customer"):
        Config(pricing=pricing, prod=Target(guild_allowlist=["customer"]))
    config = Config(pricing=pricing)
    assert config.models.backends["anthropic"].primary == "claude-haiku-5-5"
    with pytest.raises(ValueError, match="disabled"):
        config.target("prod")
    with pytest.raises(ValidationError, match="explicitly allowed"):
        Config(pricing=pricing, prod=Target(enabled=True, guild_id="745261709622771773"))


def test_estimate_includes_all_judges(pricing: Pricing, scenario: Scenario) -> None:
    scenario.assertions = [
        Assertion(kind="judge", turn=1, rubric="one"),
        Assertion(kind="judge", turn=2, rubric="two"),
    ]
    assert estimate(scenario, pricing) == pytest.approx(0.527)


def test_daily_cap_shared_receipts_and_reservations(ledger: Ledger) -> None:
    ledger.path.parent.mkdir(parents=True)
    yesterday = utcnow() - timedelta(days=1)
    ledger.path.write_text(
        json.dumps({"ts": yesterday.isoformat(), "usd": 9})
        + "\n"
        + json.dumps({"ts": utcnow().isoformat(), "usd": 8.5})
        + "\n"
    )
    ledger.reserve("a", 1)
    with pytest.raises(ValueError, match="cap"):
        Ledger(ledger.path).reserve("b", 0.6)
    ledger.reserve("b", 0.5)
    ledger.receipt("a", [Usage(usd=0.1, source="turn_outcomes")], 1)
    assert "a" not in json.loads(ledger.reservations.read_text())
    Ledger(ledger.path).reserve("c", 0.8)


def test_unknown_usage_retains_estimate(ledger: Ledger) -> None:
    ledger.reserve("a", 0.5)
    ledger.receipt("a", [Usage()], 0.5)
    row = json.loads(ledger.path.read_text())
    assert row["usd"] == 0.5
    assert row["actual_usd"] is None
    assert row["accounting"] == "conservative_estimate"


def test_corrupt_ledger_fails_closed(ledger: Ledger) -> None:
    ledger.path.parent.mkdir(parents=True)
    ledger.path.write_text("not-json\n")
    with pytest.raises(ValueError):
        ledger.reserve("a", 0.1)


@pytest.mark.parametrize("usd", [-1, float("nan"), float("inf")])
def test_nonfinite_or_negative_estimate_refused(ledger: Ledger, usd: float) -> None:
    with pytest.raises(ValueError):
        ledger.reserve("a", usd)


def test_declared_proposals_are_pending_entries(tmp_path: Path, scenario: Scenario) -> None:
    import yaml

    from qa.live.schema import ProposedScenario

    values = scenario.model_dump(by_alias=True)
    values["steps"][2]["mention"] = True
    values["assert"] = [{"kind": "message_count", "turn": 2, "max": 3}]
    (tmp_path / "proposal.yaml").write_text(yaml.safe_dump(values))
    entry = load_catalog(tmp_path)[0]
    assert isinstance(entry, ProposedScenario)
    assert "assertion message_count" in entry.unsupported


def test_invalid_known_step_not_hidden_by_proposal(tmp_path: Path, scenario: Scenario) -> None:
    import yaml

    values = scenario.model_dump(by_alias=True)
    values["steps"][0].pop("text")
    values["assert"] = [{"kind": "message_count", "turn": 2, "max": 3}]
    (tmp_path / "invalid.yaml").write_text(yaml.safe_dump(values))
    with pytest.raises(ValueError):
        load_catalog(tmp_path)


@pytest.mark.parametrize(
    "assertion",
    [
        {"kind": "attachments", "turn": 2, "max": 0, "name_pattern": r"\.typ$"},
        {"kind": "reaction_present", "turn": 2, "emoji": "👍", "within_s": 5},
    ],
)
def test_unimplemented_parameter_semantics_are_pending(
    tmp_path: Path, scenario: Scenario, assertion: dict[str, object]
) -> None:
    import yaml

    from qa.live.schema import ProposedScenario

    values = scenario.model_dump(by_alias=True)
    values["assert"] = [assertion]
    (tmp_path / "later.yaml").write_text(yaml.safe_dump(values))
    entry = load_catalog(tmp_path)[0]
    assert isinstance(entry, ProposedScenario)
    assert entry.unsupported


def test_approved_placeholders_and_regex_quantifiers_load(
    tmp_path: Path, scenario: Scenario
) -> None:
    import yaml

    values = scenario.model_dump(by_alias=True)
    values["assert"][0]["pattern"] = "[a-z]{12}"
    (tmp_path / "valid.yaml").write_text(yaml.safe_dump(values))
    assert isinstance(load_catalog(tmp_path)[0], Scenario)
    values["steps"][0]["text"] = "Use {nonce}"
    (tmp_path / "valid.yaml").write_text(yaml.safe_dump(values))
    assert isinstance(load_catalog(tmp_path)[0], Scenario)


def test_attachment_resolves_against_catalog_root(tmp_path: Path, scenario: Scenario) -> None:
    import yaml

    root = tmp_path / "scenarios"
    root.mkdir()
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    asset = fixtures / "test.csv"
    asset.write_text("n\n1\n")
    values = scenario.model_dump(by_alias=True)
    values["steps"][0]["file"] = "fixtures/test.csv"
    (root / "one.yaml").write_text(yaml.safe_dump(values))
    entry = load_catalog(tmp_path)[0]
    assert isinstance(entry, Scenario)
    assert entry.steps[0].file == str(asset.resolve())
    asset.unlink()
    with pytest.raises(ValueError, match="attachment not found"):
        load_catalog(tmp_path)


def test_manual_checklist_with_no_automated_steps(scenario: Scenario) -> None:
    entry = Scenario.model_validate(
        scenario.model_dump(by_alias=True)
        | {
            "set": "B",
            "tier": "manual",
            "est_turns": 0,
            "steps": [],
            "assert": [],
            "human": [{"click": "Open the panel", "expect": "The panel opens"}],
        }
    )
    assert entry.set == "B"


@pytest.mark.parametrize("section", ["steps", "assert"])
def test_unknown_kind_only_makes_its_scenario_pending(
    section: str, tmp_path: Path, scenario: Scenario
) -> None:
    import yaml

    from qa.live.schema import ProposedScenario

    values = scenario.model_dump(by_alias=True)
    (tmp_path / "supported.yaml").write_text(yaml.safe_dump(values))
    values["id"] = "QA-FUTURE"
    values["tier"] = "full"
    values[section].append({"do" if section == "steps" else "kind": "future_kind", "future_arg": 2})
    (tmp_path / "future.yaml").write_text(yaml.safe_dump(values))
    entries = load_catalog(tmp_path)
    assert len(entries) == 2
    future = next(entry for entry in entries if entry.id == "QA-FUTURE")
    assert isinstance(future, ProposedScenario)
    assert any("future_kind" in feature for feature in future.unsupported)
    assert isinstance(next(entry for entry in entries if entry.id == scenario.id), Scenario)


def test_approved_ledger_kind_is_typed_pending(tmp_path: Path, scenario: Scenario) -> None:
    import yaml

    from qa.live.schema import ProposedScenario

    values = scenario.model_dump(by_alias=True)
    values["assert"] = [{"kind": "ledger_matches_usage", "turn": 1, "tol_pct": 2}]
    (tmp_path / "ledger.yaml").write_text(yaml.safe_dump(values))
    entry = load_catalog(tmp_path)[0]
    assert isinstance(entry, ProposedScenario)
    assert "assertion ledger_matches_usage" in entry.unsupported


@pytest.mark.parametrize("section", ["setup", "steps", "assert", "teardown"])
def test_canary_unknown_kind_fails_catalog_validation(
    section: str, tmp_path: Path, scenario: Scenario
) -> None:
    import yaml

    values = scenario.model_dump(by_alias=True)
    values[section].append({"kind" if section == "assert" else "do": "typo_kind"})
    (tmp_path / "canary.yaml").write_text(yaml.safe_dump(values))
    with pytest.raises(ValueError, match="unknown .* kind in canary: typo_kind"):
        load_catalog(tmp_path)


def test_canary_approved_unimplemented_kind_remains_pending(
    tmp_path: Path, scenario: Scenario
) -> None:
    import yaml

    from qa.live.schema import ProposedScenario

    values = scenario.model_dump(by_alias=True)
    values["assert"].append({"kind": "ledger_matches_usage", "turn": 1, "tol_pct": 2})
    (tmp_path / "canary.yaml").write_text(yaml.safe_dump(values))
    assert isinstance(load_catalog(tmp_path)[0], ProposedScenario)
