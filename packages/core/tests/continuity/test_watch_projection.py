"""Projection bounds and internal-compatible watch comparison semantics."""

import json
import math
import random

import pytest
from daimon.core.continuity.watch_projection import (
    BASELINE,
    DIGEST_CHARS,
    ITEM_BYTES,
    MAX_EVIDENCE_PATHS,
    MAX_ITEMS,
    MAX_KEYS,
    PAYLOAD_BYTES,
    SOURCE,
    bounded,
    compare,
    evaluate,
    kept,
    payload,
    project,
    projection_path,
    validate_rule,
)


@pytest.mark.parametrize(
    "expression", ["", "..", ".[]", ".x | length", "env", ".x()", ".[0]", ".x[-1]", ".x "]
)
def test_projection_refuses_evaluation_and_expansion(expression):
    with pytest.raises(ValueError):
        projection_path(expression)


def test_projection_safe_subset_and_missing_fields():
    value = {"rows": [{"status": "done"}]}
    assert project(value, ".rows[0].status") == "done"
    assert project(value, ".rows[1].status") is None
    assert project(value, ".missing") is None
    assert project(value) == value


@pytest.mark.parametrize(
    "value", ["x" * 4097, "😀" * 1000, [0] * 1025, {"x" * 4097: 1}, math.nan, math.inf, 2**129]
)
def test_projection_output_is_bounded(value):
    with pytest.raises(ValueError):
        project(value)


def test_projection_rejects_deep_input_even_when_selected_output_is_small():
    value = {"ok": 1}
    for _ in range(17):
        value = {"ok": 1, "nested": value}
    with pytest.raises(ValueError):
        project(value, ".ok")


def test_projection_rejects_cycles_without_recursive_traversal():
    value = []
    value.append(value)
    with pytest.raises(ValueError):
        bounded(value)


@pytest.mark.parametrize("empty", [None, False, "", [], {}])
def test_change_suppresses_empty_and_false(empty):
    assert evaluate("change", "old", empty) is None


def test_change_zero_is_meaningful_and_boolean_is_distinct():
    assert evaluate("change", False, 0) == 0
    assert evaluate("change", 1, True) is True
    assert evaluate("change", {"a": 1, "b": 2}, {"b": 2, "a": 1}) is None


def test_added_ignores_removals_and_deduplicates_new_elements():
    assert evaluate("added", [1, 2], [2]) is None
    assert evaluate("added", [1], [1, True, 2, 2]) == [True, 2]


def test_updated_ignores_deletions_and_includes_new_and_changed_keys():
    assert evaluate("updated", {"a": 1, "b": 2}, {"b": 2}) is None
    assert evaluate("updated", {"a": 1}, {"a": True, "b": 2}) == {"a": True, "b": 2}


@pytest.mark.parametrize(
    "rule,previous,current",
    [("added", {}, []), ("added", [], {}), ("updated", [], {}), ("updated", {}, [])],
)
def test_rules_refuse_wrong_baseline_shapes(rule, previous, current):
    with pytest.raises(ValueError):
        evaluate(rule, previous, current)


def test_map_step_projects_each_element_once_and_only_for_polls():
    value = {"rows": [{"id": 1, "x": {"y": "a"}}, {"id": 2}, "loose"]}
    assert project(value, ".rows[].id", allow_map=True) == [1, 2, None]
    assert project(value, ".rows[].x.y", allow_map=True) == ["a", None, None]
    assert project(value, ".missing[].id", allow_map=True) is None
    for expression in (".rows[].id", ".rows[]"):
        with pytest.raises(ValueError):
            projection_path(expression)
    for expression in (".[]", ".rows[][]", ".rows[].x[].y", "[]"):
        with pytest.raises(ValueError):
            projection_path(expression, allow_map=True)


def test_keyed_added_keeps_digests_never_keys_and_fires_on_new_keys():
    secret = "private-key-value"
    before = {"rows": [{"id": secret, "text": "a"}, {"text": "no key"}]}
    after = {"rows": [{"id": secret, "text": "edited"}, {"id": 7, "text": "b"}]}
    saved = kept(before, rule="added", key=".rows[].id")
    assert isinstance(saved, dict) and len(saved["keys"]) == 1
    assert secret not in json.dumps(saved)
    assert all(len(digest) == DIGEST_CHARS for digest in saved["keys"])
    assert compare(saved, before, rule="added", key=".rows[].id") is None
    # Edits under a known key do not fire; only the new key does.
    assert compare(saved, after, rule="added", key=".rows[].id") == [{"id": 7, "text": "b"}]
    assert compare(
        saved, after, rule="added", key=".rows[].id", evidence=[".text", ".missing"]
    ) == [{".text": "b", ".missing": None}]
    # Distinct JSON types are distinct keys.
    assert (
        len(kept({"rows": [{"id": 1}, {"id": "1"}]}, rule="added", key=".rows[].id")["keys"]) == 2
    )


def test_key_and_evidence_validation():
    validate_rule("added", key=".rows[].id", evidence=[".a"] * MAX_EVIDENCE_PATHS)
    for rule, key, evidence in (
        ("change", ".rows[].id", []),
        ("updated", None, [".a"]),
        ("added", ".rows.id", []),
        ("added", None, [".a"] * (MAX_EVIDENCE_PATHS + 1)),
        ("added", None, [".a[].b"]),
    ):
        with pytest.raises(ValueError):
            validate_rule(rule, key=key, evidence=evidence)
    with pytest.raises(ValueError, match="at most"):
        kept({"rows": [{"id": i} for i in range(MAX_KEYS + 1)]}, rule="added", key=".rows[].id")


def test_source_limits_are_larger_than_kept_limits():
    # 20 000 nodes and depth 32 pass at the source tier; one more of either fails.
    flat = {"rows": [0] * (SOURCE.nodes - 2)}
    assert bounded(flat, SOURCE) is flat
    with pytest.raises(ValueError):
        bounded({"rows": [0] * (SOURCE.nodes - 1)}, SOURCE)
    deep: list[object] = []
    for _ in range(SOURCE.depth):
        deep = [deep]
    bounded(deep, SOURCE)
    with pytest.raises(ValueError):
        bounded([deep], SOURCE)
    # A 256 KiB raw document passes; one byte over fails. Neither fits the old 4 KiB bound.
    text = "x" * (SOURCE.bytes - 2)
    assert bounded(text, SOURCE) == text
    with pytest.raises(ValueError):
        bounded(text + "x", SOURCE)
    with pytest.raises(ValueError):
        bounded(text)
    # A baseline holds 64 KiB.
    kept("x" * (BASELINE.bytes - 2), rule="change", key=None)
    with pytest.raises(ValueError):
        kept("x" * (BASELINE.bytes - 1), rule="change", key=None)


def test_payload_clips_items_and_counts_the_omitted():
    found = [{"text": "y" * 5000, "n": i} for i in range(30)]
    text = payload(found, items=True)
    decoded = json.loads(text)
    assert len(text) <= PAYLOAD_BYTES
    assert len(decoded["items"]) + decoded["omitted"] == 30
    assert 0 < len(decoded["items"]) <= MAX_ITEMS
    assert all(len(json.dumps(item)) <= ITEM_BYTES for item in decoded["items"])
    small = payload([{"n": i} for i in range(25)], items=True)
    assert json.loads(small)["omitted"] == 5
    # Unkeyed evidence is kept as before when it fits, and clipped when it does not.
    assert payload({"a": 1}, items=False) == json.dumps({"a": 1})
    big = payload("z" * 40000, items=False)
    assert len(big) <= PAYLOAD_BYTES and json.loads(big)["truncated"].startswith('"zzz')
    # Escaping cannot push a clipped item past its bound.
    escaped = payload(["\u0001" * 3000], items=True)
    assert len(escaped) <= PAYLOAD_BYTES
    assert all(len(json.dumps(item)) <= ITEM_BYTES for item in json.loads(escaped)["items"])


def test_adversarial_trees_are_refused_or_bounded():
    rng = random.Random(1613)

    budget = [0]

    def tree(depth: int) -> object:
        # A node budget keeps the generator finite; depth still exceeds the limits.
        budget[0] -= 1
        choice = rng.randrange(6)
        if depth > 40 or choice == 0 or budget[0] < 0:
            return rng.choice([None, True, 0, 1.5, "s" * rng.randrange(0, 300), 2**200])
        if choice < 3:
            return [tree(depth + 1) for _ in range(rng.randrange(0, 8))]
        return {f"k{n}": tree(depth + 1) for n in range(rng.randrange(0, 8))}

    for _ in range(120):
        budget[0] = rng.choice([50, 2000, 2000, 25000])
        document = {"rows": [tree(0) for _ in range(rng.randrange(0, 6))]}
        try:
            projected = project(document, ".rows[].k1", limits=SOURCE, allow_map=True)
            saved = kept(projected, rule="added", key=None)
        except ValueError:
            continue
        assert len(json.dumps(saved)) <= BASELINE.bytes
        found = compare([], projected, rule="added", key=None)
        if found is not None:
            assert len(payload(found, items=True)) <= PAYLOAD_BYTES
