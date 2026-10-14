"""Consume N3's pinned Anthropic inputs; SDK construction stays with its owner."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from daimon.core.turn.state import TurnState
from daimon.testing.anthropic_fixtures import (
    AnthropicEntry,
    AnthropicIndex,
    AnthropicReplay,
    AnthropicScenario,
    AnthropicTurn,
    anthropic_replay,
    anthropic_turn_tape,
)
from daimon.testing.provider_fixtures import FixtureGap
from daimon.testing.provider_replay import SourcePin

if __package__:
    from .catalog_runner import ScenarioPlan, expand_text, sha
else:
    from catalog_runner import ScenarioPlan, expand_text, sha


@dataclass(frozen=True)
class AnthropicRecipe:
    entry: AnthropicEntry
    scenario: AnthropicScenario
    codec: SourcePin
    manifest: Path
    repository_root: Path
    manifest_sha256: str

    def current(self) -> None:
        """Reject changed or copied metadata before any SDK allocation."""
        if sha(self.manifest.read_bytes()) != self.manifest_sha256:
            raise ValueError("Anthropic manifest changed after selection")
        index = AnthropicIndex.model_validate_json(self.manifest.read_bytes())
        entry = next((e for e in index.entries if e.scenario_id == self.entry.scenario_id), None)
        if entry != self.entry or index.codec != self.codec:
            raise ValueError("Anthropic recipe differs from its pinned manifest")
        self.codec.resolve(self.repository_root)
        index.authoring.resolve(self.repository_root)
        index.input_index.verify(self.manifest.parent.parent)
        self.entry.input.verify(self.manifest.parent.parent)
        self.entry.replay.verify(self.manifest.parent)
        saved = AnthropicScenario.model_validate_json(
            (self.manifest.parent / self.entry.replay.path).read_bytes()
        )
        if saved != self.scenario:
            raise ValueError("Anthropic recipe differs from its pinned native input")

    def validate(self, plan: ScenarioPlan) -> None:
        self.current()
        row = self.scenario
        if (
            plan.backend != row.backend
            or plan.scenario.id != row.scenario_id
            or plan.scenario.sha256 != row.scenario_sha256
            or any(
                b.revision.profile != row.profile or b.agent_model != row.agent_model
                for b in plan.bindings
            )
        ):
            raise ValueError("Anthropic recipe differs from the selected catalog backend")
        planned = {i.turn: i for i in plan.invocations if i.operation == "host_turn"}
        provided = {t.turn for t in row.turns}
        bindings = {b.turn: b for b in self.entry.bindings}
        missing = {
            g.location
            for g in self.entry.gaps
            if g.code in {"MANUAL_TRIGGER", "NATIVE_TOOL_SCHEMA_UNBOUND", "HOST_GATE"}
        }
        if (
            provided != set(bindings)
            or len(bindings) != len(self.entry.bindings)
            or provided - set(planned)
            or any(i.location not in missing for n, i in planned.items() if n not in provided)
        ):
            raise ValueError("Anthropic recipe silently omits or invents a host turn")
        slots: dict[tuple[str, str], str] = {}
        for turn in row.turns:
            i = planned[turn.turn]
            binding = bindings[turn.turn]
            if (
                i.text is None
                or i.channel is None
                or binding.location != i.location
                or binding.root_id != turn.events[0]["id"]
                or turn.request_text != expand_text(i.text, row.values)
            ):
                raise ValueError("Anthropic recipe differs from the original invocation")
            slot = (i.channel, i.thread or "")
            if (slot in slots and slots[slot] != binding.session_id) or (
                slot not in slots and binding.session_id in slots.values()
            ):
                raise ValueError("Anthropic recipe changes or aliases a selected session")
            slots[slot] = binding.session_id

    @property
    def gaps(self) -> tuple[FixtureGap, ...]:
        # Releasing a concurrency gate is N3's separate execution hook. Never
        # release it just because a scripted tool result already exists.
        return self.entry.gaps + tuple(
            FixtureGap(
                code="RUNNER_HOOK",
                location=b.location,
                reason="Completion gate needs an owned concurrency hook.",
            )
            for b in self.entry.bindings
            if b.completion_gate
        )

    @property
    def execution_gaps(self) -> tuple[FixtureGap, ...]:
        # These pinned recipe-wide missing effects have no Anthropic native
        # invocation binding. Keep their original locations in the report and
        # conservatively stop the scenario, rather than guessing an MCP hook.
        globals_ = {("mcp", "RUNNER_HOOK"), ("deepwiki", "EXTERNAL_TOOL_SCHEMA_UNBOUND")}
        for gap in self.gaps:
            if gap.location in {"mcp", "deepwiki"} and (gap.location, gap.code) not in globals_:
                raise ValueError("Anthropic prerequisite has no owned execution scope")
        return tuple(
            gap.model_copy(update={"location": "scenario"})
            if (gap.location, gap.code) in globals_
            else gap
            for gap in self.gaps
        )

    def session_id(self, turn: int) -> str:
        return next(b.session_id for b in self.entry.bindings if b.turn == turn)

    def turn(self, number: int) -> AnthropicTurnSource:
        return AnthropicTurnSource(self, next(t for t in self.scenario.turns if t.turn == number))


@dataclass(frozen=True)
class AnthropicTurnSource:
    recipe: AnthropicRecipe
    turn: AnthropicTurn

    @asynccontextmanager
    async def client(self, session_id: str) -> AsyncIterator[AnthropicReplay]:
        self.recipe.current()
        if session_id != self.recipe.session_id(self.turn.turn):
            raise ValueError("Anthropic host differs from its selected session")
        tape = anthropic_turn_tape(
            self.recipe.entry, self.recipe.scenario, self.turn, codec=self.recipe.codec
        )
        async with anthropic_replay(
            tape, source_root=self.recipe.manifest.parent, codec_root=self.recipe.repository_root
        ) as replay:
            yield replay

    def verify_usage(self, state: TurnState) -> None:
        usage = next(
            e["model_usage"] for e in self.turn.events if e["type"] == "span.model_request_end"
        )
        actual = {
            "input_tokens": state.usage_totals.input_tokens,
            "cache_read_input_tokens": state.usage_totals.cache_read_input_tokens,
            "cache_creation_input_tokens": state.usage_totals.cache_creation_input_tokens,
            "output_tokens": state.usage_totals.output_tokens,
        }
        if actual != usage:
            raise ValueError("Anthropic host did not fold its pinned native usage")
