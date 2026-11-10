"""What one turn ran with, recorded so evals can group results by it."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cache
from typing import TYPE_CHECKING

from pydantic import JsonValue

if TYPE_CHECKING:
    from daimon.core.features.resolver import ResolvedFeature
    from daimon.core.features.snapshot import FeatureSnapshot
    from daimon.core.session_snapshot import SessionSnapshot


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def sample_controls() -> list[str]:
    """The turn controls of one fixed sample turn, with every optional part present, at each
    level a workspace can choose."""
    from daimon.core.features.resolver import ResolvedFeature
    from daimon.core.features.snapshot import FeaturePlace, FeatureSnapshot
    from daimon.core.stores.domain import Role, TurnOriginRow
    from daimon.core.turn_origin import (
        HandoffNotice,
        ResponderAccount,
        SessionState,
        render_turn_origin,
    )
    from daimon.core.web_links import TenantLinks

    nobody, when = uuid.UUID(int=0), datetime(2000, 1, 1, tzinfo=UTC)
    origin = TurnOriginRow(
        id=nobody,
        tenant_id=nobody,
        account_id=nobody,
        platform="slack",
        parent_channel_id="C0",
        thread_id="T0",
        responder_ma_agent_id="agent_0",
        responder_name="Agent",
        configuration_target_ma_agent_id="agent_1",
        configuration_target_name="Target",
        role=Role.USER,
        created_at=when,
        expires_at=when,
    )
    rendered: list[str] = []
    for level, tools in (("full", "on"), ("origin", "on"), ("minimal", "off")):
        found = {
            key: ResolvedFeature(key, value, "default")
            for key, value in (("turn_controls", level), ("daimon_tools", tools))
        }
        found["web_awareness"] = ResolvedFeature("web_awareness", "on", "default")
        rendered.append(
            render_turn_origin(
                origin,
                effective_is_admin=True,
                responder_handle="@agent",
                responder_account=ResponderAccount(user_id="U0", mention="<@U0>"),
                session_state=SessionState(state="replaced", applied=("a",), lost=("b",)),
                handoff=HandoffNotice(
                    from_name="Other",
                    from_ma_agent_id="agent_2",
                    requested_by="person",
                    requested_work="work",
                    workspace="transferred",
                ),
                running_on={"provider": "anthropic", "model": "m"},
                moved={"kind": "moved"},
                web=TenantLinks("https://example.test", nobody, frozenset(), True),
                features=FeatureSnapshot(FeaturePlace(nobody, "slack"), 0, found),
            )
        )
    return rendered


@cache
def _prompt_hashes() -> tuple[str, str, str]:
    """Safety block, guidance block and turn controls, each by the text a model reads, so any
    wording change moves the hash."""
    from daimon.core.agent_guidance import CREDENTIAL_GUIDANCE_BLOCK, SAFETY_BLOCK
    from daimon.core.turn_origin import ROUTINE_WEB_SENTENCE

    return (
        _sha256(SAFETY_BLOCK),
        _sha256(CREDENTIAL_GUIDANCE_BLOCK),
        _sha256("\0".join([*sample_controls(), ROUTINE_WEB_SENTENCE])),
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
