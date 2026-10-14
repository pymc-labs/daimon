"""Equivalent provider input exposes the catalog's expected rendering failure."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from daimon.adapters.discord.tables import render_discord_tables
from daimon.testing.provider_fixtures import load_provider_fixtures
from pydantic import JsonValue, TypeAdapter

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "packages/testing/fixtures/target53"
PACK = load_provider_fixtures(DATA / "index.json", catalog_root=DATA / "catalog")
SID = "QA-NEW37-MARKDOWN-TABLE-READABLE"
OBJECT = TypeAdapter[dict[str, JsonValue]](dict[str, JsonValue])


def answers() -> tuple[str, str, str]:
    scenario, gemini = PACK.select(SID, "gemini")
    assert scenario.anthropic is not None
    scenario.anthropic.pin.verify(ROOT)
    records = TypeAdapter[list[dict[str, JsonValue]]](list[dict[str, JsonValue]]).validate_json(
        (ROOT / scenario.anthropic.pin.path).read_bytes()
    )
    source = next(row for row in records if row["scenario_id"] == SID)
    turns = source["turns"]
    assert isinstance(turns, list) and len(turns) == 1
    turn = OBJECT.validate_python(turns[0])
    events = turn["events"]
    assert isinstance(events, list)
    messages = [
        OBJECT.validate_python(e)
        for e in events
        if isinstance(e, dict) and e.get("type") == "agent.message"
    ]
    assert len(messages) == 1
    content = messages[0]["content"]
    assert isinstance(content, list) and len(content) == 1
    anthropic = OBJECT.validate_python(content[0])["text"]
    assert isinstance(anthropic, str)
    assert source["scenario_sha256"] == scenario.source.sha256
    assert turn["request_text"] == gemini.turns[0].request_text
    _, openai = PACK.select(SID, "openai")
    native_contents: list[str] = []
    for provider in (openai, gemini):
        if provider.backend == "gemini":
            steps = provider.turns[0].snapshot["steps"]
            assert isinstance(steps, list)
            output = OBJECT.validate_python(steps[0])
        else:
            items = [
                OBJECT.validate_python(f["item"])
                for f in provider.turns[0].frames
                if f.get("type") == "agent.session.turn.item.done"
            ]
            output = next(
                item
                for item in items
                if item.get("type") == "message" and item.get("role") == "assistant"
            )
        parts = output["content"]
        assert isinstance(parts, list) and len(parts) == 1
        text = OBJECT.validate_python(parts[0])["text"]
        assert isinstance(text, str)
        native_contents.append(text)
    return anthropic, native_contents[0], native_contents[1]


@pytest.mark.asyncio
async def test_new37_matches_all_backends_and_fails_raw_pipe_predicate_at_default() -> None:
    scenario, _ = PACK.select(SID, "openai")
    assertion = scenario.scenario["assert"]
    assert isinstance(assertion, list) and len(assertion) == 1
    predicate = OBJECT.validate_python(assertion[0])
    assert predicate["kind"] == "text_absent"
    pattern = predicate["pattern"]
    assert isinstance(pattern, str)
    pattern = pattern.replace("\\\\", "\\")
    replies = answers()
    assert len(set(replies)) == 1
    for text in replies:
        rows = text.splitlines()
        assert len(rows) == 6
        assert [v.strip() for v in rows[0].strip("|").split("|")] == ["Fruit", "Colour", "Price"]
        assert all(row.startswith("|") and row.endswith("|") for row in rows)
        rendered, files = await render_discord_tables(text)
        assert rendered == text and files == []
        assert re.search(pattern, rendered), (
            "raw table must trigger the frozen forbidden-text predicate"
        )
    for source in PACK.scenarios:
        if source.anthropic is not None:
            source.anthropic.pin.verify(ROOT)


def test_prior_non_equivalent_table_evaded_the_same_predicate() -> None:
    scenario, _ = PACK.select(SID, "openai")
    assertions = scenario.scenario["assert"]
    assert isinstance(assertions, list)
    pattern = OBJECT.validate_python(assertions[0])["pattern"]
    assert isinstance(pattern, str)
    pattern = pattern.replace("\\\\", "\\")
    old = "Fruit | Color | Taste\nApple | Red | Sweet\nPear | Green | Sweet\nLemon | Yellow | Sour\nGrape | Purple | Sweet"
    assert re.search(pattern, old) is None
    assert re.search(pattern, answers()[0]) is not None
