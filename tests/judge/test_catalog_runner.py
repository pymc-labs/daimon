"""Catalog planning uses fixture-only inputs and never performs the actions."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from mux.contracts.ids import Provider

from . import catalog_runner as catalog

HEAD = "a" * 40


def source(id_: str = "QA-FIXTURE") -> dict[str, Any]:
    return {
        "id": id_,
        "title": "fixture turn",
        "set": "A",
        "surface": "discord",
        "setup": [],
        "steps": [
            {"do": "new_channel"},
            {"do": "mention", "text": "Write {nonce}."},
            {"do": "wait_done"},
            {"do": "thread_reply", "mention": True, "text": "Read it."},
            {"do": "wait_done"},
        ],
        "assert": [{"kind": "text_present", "turn": 2, "pattern": "KEPT"}],
        "teardown": [],
    }


def write_source(root: Path, row: dict[str, Any]) -> None:
    (root / "scenarios").mkdir(exist_ok=True)
    (root / "scenarios" / f"{row['id']}.yaml").write_text(yaml.safe_dump(row))


def planned(
    root: Path, row: dict[str, Any], backend: Provider = "anthropic"
) -> catalog.ScenarioPlan:
    write_source(root, row)
    return catalog.plan_scenario(catalog.load_catalog(root)[0], root, backend, run_id="fixture")


@pytest.mark.parametrize("backend", catalog.PROVIDERS)
def test_explicit_channel_binding_pins_provider_profile_model_and_followup(
    tmp_path: Path, backend: Provider
) -> None:
    row = source()
    original = copy.deepcopy(row)
    plan = planned(tmp_path, row, backend)
    assert row == original and plan.scenario.scenario == original
    binding = plan.bindings[0]
    profile, model = catalog.SELECTIONS[backend]
    assert binding.revision.backend == backend and binding.revision.profile == profile
    assert binding.agent_model == model
    assert binding.revision.model == (None if backend == "anthropic" else model)
    assert binding.revision.channel.platform == "discord"
    assert backend in binding.revision.channel.channel_id
    assert binding.revision.channel.tenant_id == "qa-fixture"
    turns = [i for i in plan.invocations if i.operation == "host_turn"]
    assert [i.turn for i in turns] == [1, 2]
    assert turns[0].channel == turns[1].channel == "default"
    assert turns[0].thread == turns[1].thread == "turn1"
    assert plan.classification == "runnable_headless"
    assert plan.assertions == tuple(row["assert"])
    assert len(plan.global_assertions) == 6
    assert plan.evidence_status == "pending" and plan.execution_gaps
    assert any("awaits G" in gap for gap in plan.execution_gaps) == (backend != "anthropic")


def test_context_and_bursts_keep_catalog_turn_numbering(tmp_path: Path) -> None:
    row = source()
    row["steps"] = [
        {"do": "new_channel", "ref": "A"},
        {"do": "new_channel", "ref": "B"},
        {"do": "channel_message", "text": "context", "channel": "B"},
        {"do": "mention", "text": "question", "channel": "B"},
        {"do": "thread_reply", "mention": False, "channel": "B", "text": "ignored"},
        {"do": "burst", "channel": "A", "texts": ["one", "two"], "interval_s": 1},
    ]
    plan = planned(tmp_path, row)
    triggers = [i for i in plan.invocations if i.turn is not None]
    assert [i.turn for i in triggers] == [1, 2, 3, 4, 5]
    assert [i.operation for i in triggers] == [
        "context",
        "host_turn",
        "context",
        "host_turn",
        "host_turn",
    ]
    assert triggers[1].channel == "B" and triggers[1].thread == "turn2"
    assert triggers[2].thread == "turn2"
    assert triggers[3].channel == triggers[4].channel == "A"
    assert plan.classification == "needs_adapter_surface"


@pytest.mark.parametrize("section", ["setup", "steps", "assert", "teardown"])
def test_unknown_kind_is_retained_and_only_that_scenario_is_pending(
    tmp_path: Path, section: str
) -> None:
    row = source()
    unknown = {"kind" if section == "assert" else "do": "future_hook", "custom": {"keep": 1}}
    row[section].append(unknown)
    write_source(tmp_path, row)
    write_source(tmp_path, source("QA-PEER"))
    entries = catalog.load_catalog(tmp_path)
    plans = {
        e.id: catalog.plan_scenario(e, tmp_path, "anthropic", run_id="fixture") for e in entries
    }
    affected = plans["QA-FIXTURE"]
    assert affected.classification == "needs_unsupported_capability"
    assert unknown in affected.scenario.scenario[section]
    assert any(g.location == f"{section}[{len(row[section]) - 1}]" for g in affected.gaps)
    assert plans["QA-PEER"].classification == "runnable_headless"


@pytest.mark.parametrize(
    "mutation",
    [
        "step_parameter",
        "assert_parameter",
        "invalid_kind",
        "invalid_channel",
        "invalid_regex",
        "foreign_turn",
    ],
)
def test_malformed_or_unimplemented_parameters_never_classify_as_runnable(
    tmp_path: Path, mutation: str
) -> None:
    row = source()
    if mutation == "step_parameter":
        row["steps"][1]["future_required_mode"] = "yes"
    elif mutation == "assert_parameter":
        row["assert"][0]["future_required_match"] = "yes"
    elif mutation == "invalid_kind":
        row["steps"][1]["do"] = ["mention"]
    elif mutation == "invalid_channel":
        row["steps"][1]["channel"] = {"bad": "shape"}
    elif mutation == "invalid_regex":
        row["assert"][0]["pattern"] = "["
    else:
        row["assert"][0]["turn"] = 3
    plan = planned(tmp_path, row)
    assert plan.classification == "needs_unsupported_capability" and plan.gaps
    assert plan.evidence_status == "pending"
    assert plan.scenario.scenario == row


def test_manual_and_ui_checks_stay_adapter_gaps(tmp_path: Path) -> None:
    row = source()
    row.update(
        {"set": "B", "steps": [], "assert": [], "human": [{"click": "Approve", "expect": "done"}]}
    )
    plan = planned(tmp_path, row)
    assert plan.classification == "needs_adapter_surface"
    assert plan.scenario.scenario["human"] == row["human"]
    row = source()
    row["assert"].append({"kind": "reaction_present", "turn": 1, "emoji": "eyes"})
    plan = planned(tmp_path, row)
    assert plan.classification == "needs_adapter_surface"
    assert plan.assertions[-1]["kind"] == "reaction_present"


def test_invalid_yaml_does_not_stop_catalog_loading(tmp_path: Path) -> None:
    write_source(tmp_path, source())
    (tmp_path / "scenarios/QA-BROKEN.yaml").write_text("id: [\n")
    entries = catalog.load_catalog(tmp_path)
    assert len(entries) == 2
    bad = next(e for e in entries if e.id == "QA-BROKEN")
    assert bad.load_error and bad.sha256
    assert (
        catalog.plan_scenario(bad, tmp_path, "anthropic", run_id="fixture").evidence_status
        == "pending"
    )


@pytest.mark.parametrize("kind", ["cli", "http_check", "pytest"])
def test_planning_never_executes_catalog_shell_http_or_test_jobs(tmp_path: Path, kind: str) -> None:
    row = source()
    marker = tmp_path / "executed"
    if kind == "http_check":
        row["assert"].append({"kind": kind, "url": "http://127.0.0.1:1"})
    else:
        row["setup"].append(
            {"do": "admin" if kind == "cli" else kind, "tool": kind, "args": f"touch {marker}"}
        )
    plan = planned(tmp_path, row)
    assert plan.classification == "needs_unsupported_capability"
    assert not marker.exists()


@pytest.mark.parametrize("path", ["fixtures/read.csv", "../outside.txt", "fixtures/link.txt"])
def test_fixture_hash_is_bound_and_paths_cannot_escape_catalog(tmp_path: Path, path: str) -> None:
    (tmp_path / "fixtures").mkdir()
    fixture = tmp_path / "fixtures/read.csv"
    fixture.write_text("region,units\nwest,9\n")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    (tmp_path / "fixtures/link.txt").symlink_to(outside)
    row = source()
    row["steps"][1]["file"] = path
    plan = planned(tmp_path, row)
    invocation = next(i for i in plan.invocations if i.operation == "host_turn")
    if path == "fixtures/read.csv":
        assert invocation.fixture_sha256 == catalog.sha(fixture.read_bytes())
        assert plan.classification == "runnable_headless"
    else:
        assert invocation.fixture is None and invocation.fixture_sha256 is None
        assert plan.classification == "needs_unsupported_capability"


def test_placeholders_are_explicit_single_pass_and_preserve_regex_quantifiers() -> None:
    assert (
        catalog.expand_text(r"FILE={nonce}[a-f]{12}\b|{4,}", {"nonce": "abc"})
        == r"FILE=abc[a-f]{12}\b|{4,}"
    )
    assert catalog.expand_text("{nonce}", {"nonce": "{env.secret}"}) == "{env.secret}"
    with pytest.raises(ValueError, match="unbound"):
        catalog.expand_text("{env.secret}", {})


def matrix_fixture(root: Path) -> None:
    targets = [f"QA-TARGET-{n:02}" for n in range(53)]
    for id_ in [*targets, "QA-EXTRA"]:
        write_source(root, source(id_))
    (root / "TARGET-53.txt").write_text("\n".join(targets) + "\n")
    (root / "SCHEMA.md").write_text("fixture schema")
    (root / "PROPOSED-KINDS.md").write_text("fixture proposals")


def test_frozen_53_denominator_excludes_extras_and_roundtrips_replay_plan(tmp_path: Path) -> None:
    matrix_fixture(tmp_path)
    matrix = catalog.build_matrix(
        tmp_path,
        integration_sha=HEAD,
        run_id="fixture",
        expected_target_sha256=catalog.sha((tmp_path / "TARGET-53.txt").read_bytes()),
    )
    assert matrix.scored_denominator == 159 and len(matrix.target_ids) == 53
    assert sum(p.scored for p in matrix.plans) == 159
    assert len([p for p in matrix.plans if not p.scored]) == 3
    assert matrix.target_sha256 == catalog.sha((tmp_path / "TARGET-53.txt").read_bytes())
    assert (
        catalog.CatalogMatrix.model_validate_json(
            matrix.model_dump_json(), context={"expected_target_sha256": matrix.target_sha256}
        )
        == matrix
    )
    assert {p.evidence_status for p in matrix.plans} == {"pending"}


@pytest.mark.parametrize("failure", ["missing", "duplicate", "too_few"])
def test_missing_or_changed_denominator_refuses_matrix(tmp_path: Path, failure: str) -> None:
    matrix_fixture(tmp_path)
    target_file = tmp_path / "TARGET-53.txt"
    if failure == "missing":
        (tmp_path / "scenarios/QA-TARGET-00.yaml").unlink()
    elif failure == "duplicate":
        target_file.write_text("QA-TARGET-00\n" * 53)
    else:
        target_file.write_text("QA-TARGET-00\n")
    with pytest.raises(ValueError):
        catalog.build_matrix(
            tmp_path,
            integration_sha=HEAD,
            run_id="fixture",
            expected_target_sha256=catalog.sha((tmp_path / "TARGET-53.txt").read_bytes()),
        )


def test_cli_under_optimized_python_refuses_unfrozen_targets(tmp_path: Path) -> None:
    matrix_fixture(tmp_path)
    output = tmp_path / "plans.json"
    result = subprocess.run(
        [
            sys.executable,
            "-O",
            catalog.__file__,
            str(tmp_path),
            "--integration-sha",
            HEAD,
            "--run-id",
            "fixture",
            "--output",
            str(output),
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if result.returncode == 0 or output.exists() or b"frozen target digest" not in result.stderr:
        raise AssertionError("unfrozen target accepted under -O")


def test_swapped_target_file_is_refused(tmp_path: Path) -> None:
    matrix_fixture(tmp_path)
    target = tmp_path / "TARGET-53.txt"
    pinned = catalog.sha(target.read_bytes())
    target.write_text(target.read_text().replace("QA-TARGET-00", "QA-EXTRA"))
    with pytest.raises(ValueError, match="frozen target digest"):
        catalog.build_matrix(
            tmp_path, integration_sha=HEAD, run_id="fixture", expected_target_sha256=pinned
        )


def test_replay_rescoring_with_original_hash_is_refused(tmp_path: Path) -> None:
    matrix_fixture(tmp_path)
    matrix = catalog.build_matrix(
        tmp_path,
        integration_sha=HEAD,
        run_id="fixture",
        expected_target_sha256=catalog.sha((tmp_path / "TARGET-53.txt").read_bytes()),
    )
    body = json.loads(matrix.model_dump_json())
    body["target_ids"][0] = "QA-EXTRA"
    for plan in body["plans"]:
        if plan["scenario"]["id"] in {"QA-EXTRA", "QA-TARGET-00"}:
            plan["scored"] = not plan["scored"]
    with pytest.raises(ValueError, match="frozen target digest"):
        catalog.CatalogMatrix.model_validate_json(
            json.dumps(body), context={"expected_target_sha256": matrix.target_sha256}
        )
    # Even a self-consistent rewritten digest cannot change the production replay pin.
    body["target_sha256"] = catalog.sha(("\n".join(body["target_ids"]) + "\n").encode())
    with pytest.raises(ValueError, match="frozen target digest"):
        catalog.CatalogMatrix.model_validate_json(json.dumps(body))


def test_implicit_followup_and_wait_target_last_trigger_channel(tmp_path: Path) -> None:
    row = source()
    row["steps"] = [
        {"do": "new_channel", "ref": "A"},
        {"do": "new_channel", "ref": "B"},
        {"do": "mention", "channel": "B", "text": "first"},
        {"do": "wait_done"},
        {"do": "thread_reply", "mention": True, "text": "followup"},
        {"do": "wait_done"},
    ]
    plan = planned(tmp_path, row)
    targets = [i for i in plan.invocations if i.operation in ("host_turn", "wait_done")]
    assert [i.channel for i in targets] == ["B", "B", "B", "B"]
    assert [i.turn for i in targets] == [1, 1, 2, 2]
    assert [i.thread for i in targets if i.operation == "host_turn"] == ["turn1", "turn1"]


@pytest.mark.parametrize(
    "mutation", ["missing_backend", "duplicate_cell", "scored_extra", "wrong_target"]
)
def test_replayed_matrix_cannot_change_scored_coverage(tmp_path: Path, mutation: str) -> None:
    matrix_fixture(tmp_path)
    body = json.loads(
        catalog.build_matrix(
            tmp_path,
            integration_sha=HEAD,
            run_id="fixture",
            expected_target_sha256=catalog.sha((tmp_path / "TARGET-53.txt").read_bytes()),
        ).model_dump_json()
    )
    if mutation == "missing_backend":
        body["plans"].pop()
    elif mutation == "duplicate_cell":
        body["plans"].append(body["plans"][0])
    elif mutation == "scored_extra":
        next(p for p in body["plans"] if not p["scored"])["scored"] = True
    else:
        body["target_ids"][0] = "QA-FOREIGN"
    with pytest.raises(ValueError):
        catalog.CatalogMatrix.model_validate_json(
            json.dumps(body),
            context={
                "expected_target_sha256": catalog.sha((tmp_path / "TARGET-53.txt").read_bytes())
            },
        )


def test_replayed_plan_cannot_claim_another_provider_model(tmp_path: Path) -> None:
    body = json.loads(planned(tmp_path, source(), "anthropic").model_dump_json())
    body["bindings"][0]["agent_model"] = "foreign-expensive-model"
    with pytest.raises(ValueError, match="explicit backend"):
        catalog.ScenarioPlan.model_validate_json(json.dumps(body))
