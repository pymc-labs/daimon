"""What one turn ran with, recorded so evals can group results by it."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import cache
from types import CodeType
from typing import TYPE_CHECKING

from pydantic import JsonValue

if TYPE_CHECKING:
    from daimon.core.features.resolver import ResolvedFeature
    from daimon.core.features.snapshot import FeatureSnapshot
    from daimon.core.session_snapshot import SessionSnapshot


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _strings(code: CodeType) -> list[str]:
    """Every string literal in `code` and the functions nested in it."""
    found: list[str] = []
    for const in code.co_consts:
        if isinstance(const, str):
            found.append(const)
        elif isinstance(const, CodeType):
            found.extend(_strings(const))
    return found


@cache
def _prompt_hashes() -> tuple[str, str, str]:
    """Safety block, guidance and turn-controls template, each by content."""
    from daimon.core.agent_guidance import CREDENTIAL_GUIDANCE_BLOCK, SAFETY_BLOCK
    from daimon.core.turn_origin import ROUTINE_WEB_SENTENCE, WEB_SENTENCE, render_turn_origin

    doc = render_turn_origin.__doc__
    template = [s for s in _strings(render_turn_origin.__code__) if s != doc]
    template += [WEB_SENTENCE, ROUTINE_WEB_SENTENCE]
    return (
        _sha256(SAFETY_BLOCK),
        _sha256(CREDENTIAL_GUIDANCE_BLOCK),
        _sha256("\0".join(sorted(template))),
    )


@dataclass(frozen=True)
class EvalKey:
    """`identity` and `tools_sha256` are None where the backend records no session snapshot.
    `framing` is the platform whose adapter framed the turn; with `release` on the turn's row
    it names that adapter's wording. `features` maps each key to (value, deciding tier)."""

    identity: str | None
    tools_sha256: str | None
    safety_sha256: str
    guidance_sha256: str
    turn_controls_sha256: str
    framing: str
    features: dict[str, tuple[str, str]] = field(default_factory=dict[str, tuple[str, str]])

    @classmethod
    def of(
        cls, snapshot: SessionSnapshot | None, features: FeatureSnapshot | None, platform: str
    ) -> EvalKey:
        from daimon.core.session_snapshot import fingerprint_identity

        safety, guidance, controls = _prompt_hashes()
        resolved: Mapping[str, ResolvedFeature] = {} if features is None else features.features
        return cls(
            identity=None if snapshot is None else fingerprint_identity(snapshot),
            tools_sha256=None if snapshot is None else snapshot.tools_sha256,
            safety_sha256=safety,
            guidance_sha256=guidance,
            turn_controls_sha256=controls,
            framing=platform,
            features={key: (f.value, f.tier) for key, f in sorted(resolved.items())},
        )

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "identity": self.identity,
            "tools_sha256": self.tools_sha256,
            "safety_sha256": self.safety_sha256,
            "guidance_sha256": self.guidance_sha256,
            "turn_controls_sha256": self.turn_controls_sha256,
            "framing": self.framing,
            "features": {k: {"value": v, "tier": t} for k, (v, t) in self.features.items()},
        }
