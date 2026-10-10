"""Approved catalog substitutions, leaving numeric regex quantifiers untouched."""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import JsonValue

from qa.live.report import Result
from qa.live.schema import PLACEHOLDER, Step
from qa.live.types import Pending


class Context:
    def __init__(self, values: dict[str, str], result: Result, fixtures: Path) -> None:
        self.values = values
        self.result = result
        self.fixtures = fixtures

    def resolve(self, value: str) -> str:
        def replace(match: re.Match[str]) -> str:
            key = match[0][1:-1]
            if key in self.values:
                return self.values[key]
            turn_match = re.fullmatch(r"turn([1-9][0-9]*)\.thread_id", key)
            if turn_match:
                turn = next((t for t in self.result.turns if t.number == int(turn_match[1])), None)
                if turn and turn.thread_id:
                    return turn.thread_id
            raise Pending(f"catalog context is unavailable: {key}")

        return PLACEHOLDER.sub(replace, value)

    def substitute(self, value: JsonValue) -> JsonValue:
        if isinstance(value, str):
            return self.resolve(value)
        if isinstance(value, list):
            return [self.substitute(item) for item in value]
        if isinstance(value, dict):
            return {key: self.substitute(item) for key, item in value.items()}
        return value

    def step(self, step: Step) -> Step:
        resolved = Step.model_validate(self.substitute(step.model_dump(mode="json", by_alias=True)))
        if resolved.file and Path(resolved.file).suffix.lower() in {".yaml", ".yml", ".md"}:
            original = Path(resolved.file)
            contents = self.resolve(original.read_text())
            self.fixtures.mkdir(parents=True, exist_ok=True)
            folder = self.fixtures / str(len(list(self.fixtures.iterdir())))
            folder.mkdir()
            destination = folder / original.name
            destination.write_text(contents)
            resolved.file = str(destination)
        return resolved
