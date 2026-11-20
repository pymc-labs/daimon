"""Drafting a case's expectations with a model, from its recorded answer.

Only scrubbed cases are sent. A draft is a starting point: it sets the `expectations_unreviewed`
flag and records the model in `provenance.expectations_by`, so a person reviews it before it counts.
`estimate_draft` prices a batch first; nothing is called until the caller confirms.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final

from anthropic import AsyncAnthropic
from daimon.core.auxiliary_calls import auxiliary_complete
from daimon.core.auxiliary_models import AUXILIARY_MODELS
from daimon.core.evals.format import (
    Case,
    ExpectedEffects,
    ExpectedOutput,
    JsonObject,
    RubricItem,
    case_hash,
    to_document,
)
from daimon.core.pricing import side_call_rates
from daimon.core.spend_gate import SpendRequest, spend_gate_in_new_session
from daimon.core.usage_recording import record_auxiliary_usage
from mux.contracts.config import DEFAULT_BACKEND
from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

MAX_OUTPUT_TOKENS: Final = 1500
_CHARS_PER_TOKEN: Final = 3
_SYSTEM: Final = """You write the pass criteria for one test case of an AI agent that works \
in team chat. You get the conversation, the tools the agent called, and the answer the agent \
gave. Answer with one JSON object and nothing else:
{"effects": {"mode": "subset", "items": [{"verb": "service.resource.verb", "target": null, \
"min": 1, "hard": true}]} or null,
 "outputs": [{"kind": "reply|message|file|card|reaction|no_reply", "where": "final", \
"text": {"op": "contains", "value": "..."} or null, "hard": true}],
 "rubric": [{"id": "r1", "criterion": "...", "weight": 1, "hard": false}]}
Effects are what the agent changed in the world (for example "github.issue.create"); leave \
them null when it only read or answered. Keep outputs few and only for what must hold. The \
rubric has 2 to 5 short, checkable criteria about whether the answer is correct, grounded in \
what the tools returned, and fits the request. Never copy placeholder names or ids as needed \
facts unless the request itself depends on them."""


class DraftError(Exception):
    """A draft could not be made (refused spend, unusable model answer)."""


class Completion(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str
    input_tokens: int
    output_tokens: int
    model: str


type Complete = Callable[[str, str, int], Awaitable[Completion]]
"""One model call: system text, prompt, output token cap."""


class DraftEstimate(BaseModel):
    """What drafting a batch is expected to cost, at most."""

    cases: int
    model: str
    input_tokens: int
    output_tokens: int
    usd: float


class _Draft(BaseModel):
    model_config = ConfigDict(extra="ignore")

    effects: ExpectedEffects | None = None
    outputs: list[ExpectedOutput] | None = None
    rubric: list[RubricItem] | None = None


def draft_model(configured: str | None) -> str:
    return configured or AUXILIARY_MODELS[DEFAULT_BACKEND]


def _prompt(case: JsonObject) -> str:
    model = Case.model_validate(case)
    calls = [{"tool": x.tool, "args": x.request.args} for x in model.world.exchanges or []]
    return json.dumps(
        {
            "history": [to_document(m) for m in model.history],
            "request": [to_document(m) for m in model.turn.messages],
            "tool_calls": calls,
            "recorded_answer": model.expect.reference.text if model.expect.reference else None,
        },
        ensure_ascii=False,
    )


def estimate_draft(cases: Sequence[JsonObject], model: str) -> DraftEstimate:
    """The most a batch should cost: prompt size by characters, output at its cap."""
    tokens = sum((len(_prompt(c)) + len(_SYSTEM)) // _CHARS_PER_TOKEN + 1 for c in cases)
    out = MAX_OUTPUT_TOKENS * len(cases)
    try:
        rates = side_call_rates(model, tokens // max(len(cases), 1))
    except KeyError as exc:
        raise DraftError(f"No price is known for {model}.") from exc
    usd = (tokens * rates.input + out * rates.output) / 1_000_000
    return DraftEstimate(
        cases=len(cases), model=model, input_tokens=tokens, output_tokens=out, usd=round(usd, 4)
    )


async def draft_case(complete: Complete, case: JsonObject) -> tuple[JsonObject, Completion]:
    """`case` with drafted expectations (the recorded answer stays as the reference)."""
    done = await complete(_SYSTEM, _prompt(case), MAX_OUTPUT_TOKENS)
    start, end = done.text.find("{"), done.text.rfind("}")
    try:
        draft = _Draft.model_validate_json(done.text[start : end + 1])
    except ValidationError as exc:
        raise DraftError("The model's answer was not a usable draft.") from exc
    expect = case.get("expect")
    merged: JsonObject = {**expect} if isinstance(expect, dict) else {}
    for key, value in draft.model_dump(mode="json", exclude_none=True, by_alias=True).items():
        merged[key] = TypeAdapter(JsonValue).validate_python(value)
    out: JsonObject = {**case, "expect": merged}
    prov = case.get("provenance")
    by = prov.get("expectations_by") if isinstance(prov, dict) else None
    entry: JsonValue = {
        "kind": "model", "model": done.model, "action": "drafted",
        "at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }  # fmt: skip
    out["provenance"] = {
        **(prov if isinstance(prov, dict) else {}),
        "expectations_by": [*(by if isinstance(by, list) else []), entry],
    }
    flags = case.get("review_flags")
    flags = list(flags) if isinstance(flags, list) else []
    if not any(isinstance(f, dict) and f.get("reason") == "expectations_unreviewed" for f in flags):
        flags.append({"reason": "expectations_unreviewed"})
    out["review_flags"] = flags
    out["hash"] = case_hash(out)
    return out, done


def auxiliary_completer(
    anthropic: AsyncAnthropic,
    sessions: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    platform: str,
    user_id: str | None,
    model: str | None,
    markup: Decimal,
) -> Complete:
    """A `Complete` on the install's side-call provider: gated on spend, metered to the tenant.

    `user_id` None is the operator path, which only the workspace balance gates.
    """

    async def complete(system: str, prompt: str, max_tokens: int) -> Completion:
        block = await spend_gate_in_new_session(
            sessions,
            request=SpendRequest(
                tenant_id=tenant_id, platform_user_id=user_id, platform=platform,
                channel_id=None, agent_id=None,
            ),
            markup=markup,
        )  # fmt: skip
        if block is not None:
            raise DraftError("Drafting is paused: the balance or allowance is used up.")
        done = await auxiliary_complete(
            anthropic, provider=None, purpose="eval_draft", system=system, prompt=prompt,
            max_tokens=max_tokens, anthropic_model=model,
        )  # fmt: skip
        if done.refused:
            raise DraftError("The model declined to draft this case.")
        await record_auxiliary_usage(
            purpose="eval_draft", provider=done.provider, sessionmaker=sessions,
            tenant_id=tenant_id, platform_user_id=user_id, model_id=done.model_id,
            input_tokens=done.usage.input_tokens, output_tokens=done.usage.output_tokens,
            cache_read_input_tokens=done.usage.cache_read_input_tokens, markup=markup,
        )  # fmt: skip
        return Completion(
            text=done.text, input_tokens=done.usage.input_tokens,
            output_tokens=done.usage.output_tokens, model=done.model_id,
        )  # fmt: skip

    return complete
