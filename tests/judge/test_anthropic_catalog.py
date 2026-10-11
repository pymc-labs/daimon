"""Pinned Anthropic recipes execute the real mux host, never a provider network."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from daimon.core.turn.state import TurnState
from daimon.testing.admin_bindings import AdminBindings
from daimon.testing.anthropic_fixtures import AnthropicFixtures, load_anthropic_fixtures
from daimon.testing.provider_replay import SourcePin, WireFrame

from . import anthropic_catalog_replay as consumer
from . import headless_executor as executor
from .catalog_product_context import ProductContext
from .catalog_runner import ScenarioPlan, load_catalog, plan_scenario, sha

REPO = Path(__file__).resolve().parents[2]
INDEX = REPO / "packages/testing/fixtures/target53/anthropic/index.json"
CATALOG = INDEX.parent.parent / "catalog"
TABLE = "QA-NEW37-MARKDOWN-TABLE-READABLE"


def require(condition: object, message: str) -> None:
    if not condition:
        raise AssertionError(message)


@pytest.fixture(scope="module")
def pack() -> AnthropicFixtures:
    return load_anthropic_fixtures(INDEX, source_root=REPO)


def selected(
    pack: AnthropicFixtures, id_: str = TABLE
) -> tuple[ScenarioPlan, consumer.AnthropicRecipe]:
    entry, row = pack.select(id_)
    source = next(s for s in load_catalog(CATALOG) if s.id == id_)
    # The public projection retains the original source pin, as in the other
    # provider consumer tests. Scored matrix runs use the frozen source catalog.
    source = source.model_copy(update={"sha256": row.scenario_sha256})
    return (
        plan_scenario(source, CATALOG, "anthropic", run_id="anthropic-offline"),
        consumer.AnthropicRecipe(
            entry, row, pack.index.codec, INDEX, REPO, sha(INDEX.read_bytes())
        ),
    )


def replay(recipe: consumer.AnthropicRecipe) -> executor.ScenarioReplay:
    return executor.ScenarioReplay.model_validate(recipe.scenario.model_dump(mode="json"))


async def test_all_frozen_recipes_preserve_predicates_and_execute_owned_prefixes(
    pack: AnthropicFixtures, monkeypatch: pytest.MonkeyPatch
) -> None:
    usage_turns: list[int] = []
    original = consumer.AnthropicTurnSource.verify_usage

    def usage(self: consumer.AnthropicTurnSource, state: TurnState) -> None:
        original(self, state)
        usage_turns.append(self.turn.turn)

    monkeypatch.setattr(consumer.AnthropicTurnSource, "verify_usage", usage)
    counts = {"PASS": 0, "FAIL": 0, "EXPECTED-FAIL": 0, "PENDING": 0}
    executed = requests = turns = 0
    for row in pack.scenarios:
        plan, recipe = selected(pack, row.scenario_id)
        result = await executor.execute_cell(plan, replay(recipe), native_recipe=recipe)
        counts[result.reported_status] += 1
        require(result.input_provenance == "authored-sdk-wire", "native provenance lost")
        require(
            len(result.catalog_outcome.checks)
            == len(plan.assertions) + len(plan.global_assertions),
            "catalog predicates dropped",
        )
        require(result.request_count == 3 * len(result.evidence.turns), "wire not fully consumed")
        requests += result.request_count
        turns += len(result.evidence.turns)
        if result.evidence.turns:
            executed += 1
            require(result.host_outcome.status == "PASS", "real host evidence failed")
            require(len(result.headless_outputs) == len(result.evidence.turns), "return missing")
            for observed in result.evidence.turns:
                binding = next(b for b in recipe.entry.bindings if b.turn == observed.turn)
                require(observed.session_id == binding.session_id, "selected session changed")
                require(observed.root_turn_id == binding.root_id, "accepted root changed")
                require(observed.terminal_capture_complete, "journal coverage lost")
                require(observed.terminals[0].authority == "record", "terminal guessed")
                require(
                    not (
                        observed.tool_capture_complete
                        or observed.card_capture_complete
                        or observed.approval_capture_complete
                    ),
                    "unsupported capture claimed",
                )
        else:
            require(result.status == "PENDING" and result.request_count == 0, "missing hook passed")
    require(len(pack.scenarios) == 53, "frozen denominator changed")
    require(executed > 5, "native recipes did not extend the old five cells")
    require(requests == 3 * len(usage_turns) == 3 * turns, "native usage check omitted")
    require(counts == {"PASS": 0, "FAIL": 0, "EXPECTED-FAIL": 1, "PENDING": 52}, "score overstated")


async def test_table_uses_actual_discord_adapter_posts(pack: AnthropicFixtures) -> None:
    plan, recipe = selected(pack)
    result = await executor.execute_cell(plan, replay(recipe), native_recipe=recipe)
    require(result.reported_status == "EXPECTED-FAIL", "table false pass")
    require(result.discord_surfaces[0].boundary == "discord_adapter_posts", "wrong surface")
    require(result.evidence.turns[0].text_capture_complete, "actual surface text not bound")
    require(result.request_count == 3, "wrong native request count")
    require(result.rendered_text[0].startswith("| Fruit |"), "table control not equivalent")


@pytest.mark.parametrize(
    "mutation", ["scenario", "source", "provider", "model", "profile", "text", "location"]
)
async def test_wrong_selected_plan_refuses_before_sdk(
    pack: AnthropicFixtures, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    plan, recipe = selected(pack)
    if mutation == "scenario":
        plan = plan.model_copy(
            update={"scenario": plan.scenario.model_copy(update={"id": "foreign"})}
        )
    elif mutation == "source":
        plan = plan.model_copy(
            update={"scenario": plan.scenario.model_copy(update={"sha256": "0" * 64})}
        )
    elif mutation == "provider":
        plan = plan.model_copy(update={"backend": "openai"})
    elif mutation in {"model", "profile"}:
        b = plan.bindings[0]
        b = (
            b.model_copy(update={"agent_model": "foreign"})
            if mutation == "model"
            else b.model_copy(
                update={"revision": b.revision.model_copy(update={"profile": "foreign"})}
            )
        )
        plan = plan.model_copy(update={"bindings": (b, *plan.bindings[1:])})
    else:
        invocations = tuple(
            i.model_copy(update={"text" if mutation == "text" else "location": "foreign"})
            if i.operation == "host_turn"
            else i
            for i in plan.invocations
        )
        plan = plan.model_copy(update={"invocations": invocations})

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("SDK allocation happened before refusal")

    monkeypatch.setattr(consumer, "anthropic_replay", forbidden)
    with pytest.raises(ValueError):
        await executor.execute_cell(plan, replay(recipe), native_recipe=recipe)


@pytest.mark.parametrize(
    "mutation",
    ["manifest", "codec", "entry", "row", "missing_usage", "root", "session", "missing_turn"],
)
async def test_recipe_corruption_refuses_before_sdk(
    pack: AnthropicFixtures, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    plan, recipe = selected(pack)
    if mutation == "manifest":
        recipe = replace(recipe, manifest_sha256="0" * 64)
    elif mutation == "codec":
        recipe = replace(recipe, codec=SourcePin(path=recipe.codec.path, sha256="0" * 64))
    elif mutation in {"entry", "root", "session"}:
        entry = recipe.entry
        if mutation == "entry":
            entry = entry.model_copy(update={"scenario_id": "foreign"})
        else:
            b = entry.bindings[0].model_copy(
                update={"root_id" if mutation == "root" else "session_id": "foreign"}
            )
            entry = entry.model_copy(update={"bindings": (b, *entry.bindings[1:])})
        recipe = replace(recipe, entry=entry)
    elif mutation == "row":
        recipe = replace(
            recipe, scenario=recipe.scenario.model_copy(update={"scenario_sha256": "0" * 64})
        )
    elif mutation == "missing_turn":
        recipe = replace(recipe, scenario=recipe.scenario.model_copy(update={"turns": ()}))
    else:
        turn = recipe.scenario.turns[0]
        turn = turn.model_copy(
            update={
                "events": tuple(e for e in turn.events if e["type"] != "span.model_request_end")
            }
        )
        recipe = replace(recipe, scenario=recipe.scenario.model_copy(update={"turns": (turn,)}))

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("SDK allocated for a corrupted recipe")

    monkeypatch.setattr(consumer, "anthropic_replay", forbidden)
    with pytest.raises(ValueError):
        await executor.execute_cell(plan, replay(recipe), native_recipe=recipe)


async def test_old_replay_cannot_replace_selected_native_events(pack: AnthropicFixtures) -> None:
    plan, recipe = selected(pack)
    raw = replay(recipe).model_dump(mode="json")
    raw["turns"][0]["events"][2]["id"] = "copied-old-record"
    with pytest.raises(ValueError, match="selected native recipe"):
        await executor.execute_cell(
            plan, executor.ScenarioReplay.model_validate(raw), native_recipe=recipe
        )


@pytest.mark.parametrize("gap", ["manual", "mcp", "gate"])
async def test_unowned_hooks_do_not_allocate_or_release_sdk(
    pack: AnthropicFixtures, monkeypatch: pytest.MonkeyPatch, gap: str
) -> None:
    entry = next(
        e
        for e in pack.index.entries
        if (
            any(g.code == "MANUAL_TRIGGER" for g in e.gaps)
            if gap == "manual"
            else any(g.location == "mcp" for g in e.gaps)
            if gap == "mcp"
            else any(b.completion_gate for b in e.bindings)
        )
    )
    plan, recipe = selected(pack, entry.scenario_id)

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("unbound hook reached a provider dispatch")

    monkeypatch.setattr(consumer, "anthropic_replay", forbidden)
    result = await executor.execute_cell(plan, replay(recipe), native_recipe=recipe)
    require(result.status == "PENDING" and result.request_count == 0, "unbound hook passed")
    if gap == "mcp":
        require(any(g.location == "mcp" for g in result.gaps), "original prerequisite lost")


@pytest.mark.parametrize("mutation", ["missing_usage", "reply", "usage"])
async def test_native_wire_corruption_cannot_claim_complete_capture(
    pack: AnthropicFixtures, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    from daimon.testing.anthropic_fixtures import AnthropicTape

    plan, recipe = selected(pack)
    original = consumer.anthropic_turn_tape

    def damaged(*args: Any, **kwargs: Any) -> AnthropicTape:
        tape = original(*args, **kwargs)
        stream = tape.replies[-1]
        frames: list[WireFrame] = []
        for frame in stream.frames or ():
            payload = dict(frame.payload)
            if payload.get("type") == "span.model_request_end":
                if mutation == "missing_usage":
                    continue
                if mutation == "usage":
                    usage = payload["model_usage"]
                    if not isinstance(usage, dict):
                        raise AssertionError("fixture usage is not a mapping")
                    payload["model_usage"] = {**usage, "input_tokens": 49}
            if payload.get("type") == "agent.message" and mutation == "reply":
                payload = {**payload, "content": [{"type": "text", "text": "changed response"}]}
            frames.append(frame.model_copy(update={"payload": payload}))
        stream = stream.model_copy(update={"frames": tuple(frames)})
        return tape.model_copy(update={"replies": (*tape.replies[:-1], stream)})

    monkeypatch.setattr(consumer, "anthropic_turn_tape", damaged)
    with pytest.raises(ValueError, match="every record|pinned native usage|pinned recipe"):
        await executor.execute_cell(plan, replay(recipe), native_recipe=recipe)


async def test_changed_input_is_rechecked_at_dispatch_before_sdk(
    pack: AnthropicFixtures, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    _, recipe = selected(pack)
    root = tmp_path / "target53"
    shutil.copytree(INDEX.parent.parent, root)
    recipe = replace(recipe, manifest=root / "anthropic/index.json")
    recipe.current()
    target = recipe.manifest.parent / recipe.entry.replay.path
    target.write_bytes(target.read_bytes() + b"\n")

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("SDK allocated after source pin changed")

    monkeypatch.setattr(consumer, "anthropic_replay", forbidden)
    with pytest.raises(ValueError):
        async with recipe.turn(1).client(recipe.session_id(1)):
            raise AssertionError("changed source reached dispatch")


async def test_native_manifest_is_authoritative_for_all_53_not_a_five_tape_fallback(
    pack: AnthropicFixtures, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from daimon.testing.outcome_oracle import RunEvidence, evaluate

    from .test_catalog_runner import HEAD, source, write_source

    frozen = (CATALOG / "TARGET-53.txt").read_text()
    (tmp_path / "TARGET-53.txt").write_text(frozen)
    (tmp_path / "SCHEMA.md").write_text("offline dispatcher test")
    (tmp_path / "PROPOSED-KINDS.md").write_text("offline dispatcher test")
    for row in pack.scenarios:
        write_source(tmp_path, source(row.scenario_id))
    calls: list[tuple[str, consumer.AnthropicRecipe | None]] = []

    async def dispatch(
        plan: ScenarioPlan,
        scripted: executor.ScenarioReplay | None,
        *,
        native_recipe: consumer.AnthropicRecipe | None = None,
        admin: AdminBindings | None = None,
        admin_values: Mapping[str, str] | None = None,
        product_context: ProductContext | None = None,
    ) -> executor.CellResult:
        # Test selection only; real host execution is proved separately above.
        require(
            admin is None and admin_values is None and product_context is None,
            "default selection discovered an admin run",
        )
        if plan.backend == "anthropic":
            require(native_recipe is not None, "native manifest fell back to old tapes")
            require(
                scripted == replay(cast(consumer.AnthropicRecipe, native_recipe)), "mixed inputs"
            )
            calls.append((plan.scenario.id, native_recipe))
        evidence = RunEvidence(scenario_id=plan.scenario.id, backend=plan.backend, turns=())
        return executor.CellResult(
            scenario_id=plan.scenario.id,
            backend=plan.backend,
            scored=True,
            source_sha256=plan.scenario.sha256,
            gaps=(),
            evidence=evidence,
            catalog_outcome=evaluate(evidence, list(plan.assertions)),
            host_outcome=evaluate(evidence, []),
        )

    monkeypatch.setattr(executor, "execute_cell", dispatch)
    old = executor.ScenarioReplay(scenario_id=TABLE, scenario_sha256="0" * 64, turns=())
    result = await executor.execute_catalog(
        tmp_path,
        integration_sha=HEAD,
        run_id="selection",
        replays=(old,),
        anthropic_fixtures=INDEX,
    )
    require(len(result.cells) == 159 and len(calls) == 53, "frozen native selection incomplete")
    require(
        any(r is not None and not r.scenario.turns for _, r in calls), "empty recipes fell back"
    )


@pytest.mark.parametrize(
    "field",
    ["input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens"],
)
async def test_incorrect_host_usage_fold_is_refused(
    pack: AnthropicFixtures, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    plan, recipe = selected(pack)
    original = consumer.AnthropicTurnSource.verify_usage

    def wrong(self: consumer.AnthropicTurnSource, state: TurnState) -> None:
        totals = replace(state.usage_totals, **{field: getattr(state.usage_totals, field) + 1})
        original(self, replace(state, usage_totals=totals))

    monkeypatch.setattr(consumer.AnthropicTurnSource, "verify_usage", wrong)
    with pytest.raises(ValueError, match="pinned native usage"):
        await executor.execute_cell(plan, replay(recipe), native_recipe=recipe)


@pytest.mark.parametrize(
    "name",
    [
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_CUSTOM_HEADERS",
        "ANTHROPIC_FUTURE_SETTING",
        "ANTHROPIC_WEBHOOK_SECRET",
    ],
)
async def test_real_native_catalog_refuses_ambient_names_before_http(
    pack: AnthropicFixtures, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    import os

    import httpx

    from .test_catalog_sdk_isolation import NamesOnly, no_http_client

    plan, recipe = selected(pack)
    ambient = NamesOnly(name)
    with monkeypatch.context() as guarded:
        guarded.setattr(os, "environ", ambient)
        guarded.setattr(httpx, "AsyncClient", no_http_client)
        guarded.setattr(httpx, "Client", no_http_client)
        with pytest.raises(ValueError, match="ambient SDK"):
            await executor.execute_cell(plan, replay(recipe), native_recipe=recipe)
    require(not ambient.reads, "ambient values were read")
