"""The off-script simulator: a model writes the answer nobody recorded, consistent with the
case, the item's overlay and its own earlier answers (design section 8).

`simulate` checks each answer against the service and for simulation wording, retries once
with the reason, then gives up. `ModelSimulator` is the metered model behind it.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Final, Protocol

from anthropic import AsyncAnthropic
from daimon.core.auxiliary_calls import auxiliary_complete
from daimon.core.evals.eval_tenant import eval_tenant
from daimon.core.evals.format import (
    Body,
    Case,
    ContentBlock,
    ExchangeResponse,
    HttpResponse,
    JsonObject,
    to_document,
)
from daimon.core.evals.world.model import WorldRequest
from daimon.core.usage_recording import record_auxiliary_usage
from mux.contracts.completions import CompletionEffort
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

#: Wording a real service never uses about itself; an answer containing it is refused.
_TELLS: Final = re.compile(
    r"\bsimulat\w*|\b(mock|fake|dummy|placeholder|synthetic) (data|response|answer|result)s?\b"
    r"|\bas an ai\b|\bthis is a test\b",
    re.IGNORECASE,
)
_PROMPT_BUDGET: Final = 60_000
_MAX_TOKENS: Final = 8_000

SYSTEM_PROMPT: Final = """\
You are the service named in the request. Answer the request exactly as that service would.

- Reply with one JSON object and nothing else.
- For HTTP: {"status": <int>, "headers": {<name>: <value>}, "body": <JSON value or string>}.
- For a tool call: {"result": <JSON value>, "is_error": <bool>}.
- Stay consistent with the case facts, the state changes and your earlier answers.
- Prefer small, plausible answers. Never invent facts the case contradicts.
- Never say or hint that anything is simulated, mocked, fake or a test.
"""


@dataclass(frozen=True)
class SimContext:
    """Everything the simulator sees for one request."""

    item_id: uuid.UUID
    request: WorldRequest
    case: Case
    guidance: str
    overlay: JsonObject
    earlier: Sequence[JsonObject]
    problem: str | None = None


@dataclass(frozen=True)
class SimAnswer:
    response: ExchangeResponse
    model: str


class Simulator(Protocol):
    async def answer(self, context: SimContext) -> SimAnswer: ...

    async def same(self, service: str, recorded: JsonValue, live: JsonValue) -> bool:
        """Whether two requests to `service` ask the same thing (the `semantic` match mode)."""
        ...


Check = Callable[[ExchangeResponse], str | None]


def wording_problem(response: ExchangeResponse) -> str | None:
    text = json.dumps(to_document(response))
    if _TELLS.search(text):
        return "The answer talks about itself as simulated or fake; a real service never does."
    return None


async def simulate(simulator: Simulator, context: SimContext, check: Check) -> SimAnswer | str:
    """A checked answer, retried once with the reason; the last reason when both fail."""
    problem = ""
    for _attempt in range(2):
        try:
            answer = await simulator.answer(context)
        except ValueError as exc:  # unparseable output: retry like an invalid answer
            problem = f"The answer was not a JSON object of the asked shape: {exc}"
        else:
            problem = check(answer.response) or wording_problem(answer.response) or ""
            if not problem:
                return answer
        context = replace(context, problem=problem)
    return problem


def _cut(value: object, budget: int) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= budget else text[:budget] + "…"


def build_prompt(context: SimContext) -> str:
    """The user turn: request, service guidance, case facts, item state, earlier answers."""
    case, request = context.case, context.request
    world = case.world
    # A binary's HTTP calls are aimed at its recorded output: show the recorded commands too.
    surfaces = {request.surface, "cli"} if request.surface == "http" else {request.surface}
    relevant = [
        to_document(x)
        for x in world.exchanges or ()
        if x.surface in surfaces and (x.surface == "cli" or x.service in (None, request.service))
    ]
    resources = [to_document(r) for r in world.resources or () if r.service == request.service]
    identities = [to_document(i) for i in world.identities or ()]
    sections = [
        ("Request", _cut({"surface": request.surface, "service": request.service,
                          "server": request.server, "tool": request.tool,
                          **to_document(request.request)}, 20_000)),
        ("Service", request.service + (f"\n{context.guidance}" if context.guidance else "")),
        ("Facts to keep", (world.simulator.instructions if world.simulator else None) or "None."),
        ("Clock", f"{case.clock.now.isoformat()} ({case.clock.timezone or 'UTC'})"),
        ("Accounts in use", _cut(identities, 4_000)),
        ("Known resources", _cut(resources, 15_000)),
        ("Recorded examples", _cut(relevant, 15_000)),
        ("Changes made so far", _cut(context.overlay, 10_000)),
        ("Your earlier answers", _cut(list(context.earlier), 10_000)),
    ]  # fmt: skip
    if context.problem:
        sections.append(("Your last answer was refused", context.problem))
    prompt = "\n\n".join(f"## {title}\n{body}" for title, body in sections)
    return prompt[:_PROMPT_BUDGET]


def _strip_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    return text.strip()


def parse_answer(request: WorldRequest, text: str) -> ExchangeResponse:
    """The model's JSON as a response of the request's surface; ValueError when malformed."""
    parsed: JsonValue = json.loads(_strip_fence(text))
    if not isinstance(parsed, dict):
        raise ValueError("not an object")
    if request.surface == "http":
        status, headers, body = parsed.get("status"), parsed.get("headers"), parsed.get("body")
        if not isinstance(status, int) or not 100 <= status <= 599:
            raise ValueError("status must be an HTTP status code")
        headers = headers if isinstance(headers, dict) else {}
        header_map = {str(k): str(v) for k, v in headers.items()}
        if body is None or body == "":
            out = None
        elif isinstance(body, str):
            out = Body(text=body)
        else:
            out = Body(json=body, media_type="application/json")
        return ExchangeResponse(http=HttpResponse(status=status, headers=header_map, body=out))
    result, is_error = parsed.get("result"), parsed.get("is_error") is True
    text_out = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
    return ExchangeResponse(
        result=result, is_error=is_error, content=[ContentBlock(type="text", text=text_out)]
    )


@dataclass(frozen=True)
class ModelSimulator:
    """The simulator on the configured Anthropic model, every call metered to the eval tenant."""

    anthropic: AsyncAnthropic
    sessions: async_sessionmaker[AsyncSession]
    model: str
    effort: CompletionEffort

    async def _complete(
        self, system: str, prompt: str, max_tokens: int, effort: CompletionEffort
    ) -> str:
        done = await auxiliary_complete(
            self.anthropic, provider=None, purpose="eval_simulator", system=system,
            prompt=prompt, max_tokens=max_tokens, effort=effort, anthropic_model=self.model,
        )  # fmt: skip
        await record_auxiliary_usage(
            purpose="eval_simulator", provider=done.provider, sessionmaker=self.sessions,
            tenant_id=await eval_tenant(self.sessions), platform_user_id=None,
            model_id=done.model_id, input_tokens=done.usage.input_tokens,
            output_tokens=done.usage.output_tokens,
            cache_read_input_tokens=done.usage.cache_read_input_tokens,
        )  # fmt: skip
        if done.refused:
            raise ValueError("the model refused")
        return done.text

    async def answer(self, context: SimContext) -> SimAnswer:
        text = await self._complete(SYSTEM_PROMPT, build_prompt(context), _MAX_TOKENS, self.effort)
        return SimAnswer(parse_answer(context.request, text), self.model)

    async def same(self, service: str, recorded: JsonValue, live: JsonValue) -> bool:
        prompt = (
            f"Two requests to {service}. Do they ask for the same thing, so one answer fits "
            f"both? Reply yes or no.\n\nFirst: {_cut(recorded, 8_000)}\n\n"
            f"Second: {_cut(live, 8_000)}"
        )
        text = await self._complete('Answer with one word: "yes" or "no".', prompt, 16, "none")
        return text.strip().lower().startswith("yes")
