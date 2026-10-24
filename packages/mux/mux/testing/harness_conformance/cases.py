"""The conformance cases. Each takes a ``CaseContext``, asserts, and may raise ``SkipCase``."""

from __future__ import annotations

import asyncio
import importlib.util
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import uuid4

from pydantic import JsonValue

from mux.drivers.selfhosted.wire import ActionRequest, Event, InlineSkill, MCPServer

from ..fake_model_server import Usage, call_tool, say
from ..fake_model_server.runner import serve_app
from .client import parse_sse
from .mcp_server import SERVER_NAME, TinyMCPServer
from .target import CaseContext, SkipCase, check, text_of, tool_result

BUILTIN_TOOLS = {"bash", "read", "write", "edit"}
WRITE_CALL = {"path": "conformance.txt", "content": "written"}


@dataclass(frozen=True)
class Case:
    name: str
    run: Callable[[CaseContext], Awaitable[None]]


def _position(events: list[Event], event_type: str) -> int:
    for index, event in enumerate(events):
        if event.type == event_type:
            return index
    raise AssertionError(f"no {event_type} event; saw {[e.type for e in events]}")


def _strictly_increasing(events: list[Event]) -> bool:
    return all(a.seq < b.seq for a, b in zip(events, events[1:], strict=False))


async def health_and_auth(ctx: CaseContext) -> None:
    health = await ctx.client.health()
    check(health.protocol == "daimon-harness/0", "bad health")
    anonymous = await ctx.client.request("GET", "/v0/healthz", authenticated=False)
    check(
        anonymous.status_code in {401, 403},
        f"unauthenticated call answered {anonymous.status_code}",
    )
    wrong = await ctx.client.request(
        "GET", "/v0/healthz", headers={"Authorization": "Bearer wrong-token"}, authenticated=False
    )
    check(wrong.status_code in {401, 403}, f"wrong token answered {wrong.status_code}")


async def turn_lifecycle(ctx: CaseContext) -> None:
    ctx.script(
        say("hello world", usage=Usage(input_tokens=11, cached_input_tokens=3, output_tokens=5))
    )
    request = ctx.turn_request("lifecycle-input", instructions="LIFECYCLE-INSTRUCTIONS")
    run = await ctx.run_turn(request)
    check(run.outcome == "completed", f"outcome {run.outcome}")
    check(
        run.types[-1] == "turn_ended" and run.types.count("turn_ended") == 1, "one final turn_ended"
    )
    order = ["model_request_started", "usage", "agent_message", "turn_ended"]
    positions = [_position(run.events, kind) for kind in order]
    check(positions == sorted(positions), f"events out of order: {run.types}")
    parts = run.of("agent_message")[-1].data["parts"]
    check(parts == [{"type": "text", "text": "hello world"}], f"agent_message parts {parts}")
    check(all(e.session_id == ctx.session_id for e in run.events), "wrong session_id on an event")
    recovery = {"history_gap", "checkpoint_warning"} & set(run.types)
    check(not recovery, f"an ordinary turn carried recovery events: {sorted(recovery)}")
    [seen] = ctx.model_requests()
    check("LIFECYCLE-INSTRUCTIONS" in seen.instructions, "model did not receive the instructions")
    check("lifecycle-input" in seen.text(), "model did not receive the user input")
    check(seen.body.get("model") == ctx.conversation, "request model is not the turn's model")


async def sse_stream(ctx: CaseContext) -> None:
    ctx.script(say("streamed"))
    request = ctx.turn_request()
    response = await ctx.client.submit(request, sse=True)
    if "text/event-stream" not in response.headers.get("content-type", ""):
        raise SkipCase("target does not offer SSE on POST /v0/turns")
    check(response.status_code == 200, f"SSE submit answered {response.status_code}")
    streamed = parse_sse(response.text)
    check(
        bool(streamed) and streamed[-1].type == "turn_ended",
        "SSE stream did not end with turn_ended",
    )
    check(_strictly_increasing(streamed), "SSE events not strictly by seq")
    check(all(e.turn_id == request.turn_id for e in streamed), "SSE carried another turn's events")
    polled = await ctx.client.wait_ended(ctx.session_id, request.turn_id)
    check([e.seq for e in polled] == [e.seq for e in streamed], "SSE and polling disagree on seq")
    check(
        [e.type for e in polled] == [e.type for e in streamed], "SSE and polling disagree on types"
    )


async def idempotent_replay(ctx: CaseContext) -> None:
    ctx.script(say("once"))
    request = ctx.turn_request("replay-input")
    first = await ctx.run_turn(request)
    check(len(ctx.model_requests()) == 1, "first run should make one model request")
    again = await ctx.client.submit(request)
    check(again.status_code == 202, f"replay answered {again.status_code}, not 202")
    after = await ctx.client.wait_ended(ctx.session_id, request.turn_id)
    check([e.seq for e in after] == [e.seq for e in first.events], "replay changed the event log")
    check(len(ctx.model_requests()) == 1, "replay called the model again")
    replayed_stream = await ctx.client.submit(request, sse=True)
    if "text/event-stream" in replayed_stream.headers.get("content-type", ""):
        seqs = [e.seq for e in parse_sse(replayed_stream.text)]
        check(seqs == [e.seq for e in first.events], "SSE replay differs from the original events")
    check(len(ctx.model_requests()) == 1, "SSE replay called the model again")

    changed_body = request.model_copy(update={"input": ctx.turn_request("different-input").input})
    conflict = await ctx.client.submit(changed_body)
    check(conflict.status_code == 409, f"same key, different body answered {conflict.status_code}")
    other_key = request.model_copy(update={"idempotency_key": request.idempotency_key + "-other"})
    conflict = await ctx.client.submit(other_key)
    check(conflict.status_code == 409, f"same turn_id, new key answered {conflict.status_code}")
    check(len(ctx.model_requests()) == 1, "a rejected submit reached the model")


async def events_by_sequence(ctx: CaseContext) -> None:
    ctx.script(call_tool(ctx.tool("write"), WRITE_CALL, call_id="seq-call"), say("saved"))
    run = await ctx.run_turn()
    check(run.outcome == "completed", f"outcome {run.outcome}")
    full = (await ctx.client.events(ctx.session_id, 0)).events
    check(_strictly_increasing(full), "session events are not strictly increasing by seq")
    check(
        run.types == [e.type for e in full if e.turn_id == run.request.turn_id], "turn view differs"
    )
    check(len(full) >= 5, f"too few events to test cursors: {[e.type for e in full]}")
    for cursor in (full[0].seq, full[len(full) // 2].seq, full[-1].seq):
        page = await ctx.client.events(ctx.session_id, cursor)
        expected = [e.seq for e in full if e.seq > cursor]
        check([e.seq for e in page.events] == expected, f"after_seq={cursor} returned wrong events")
    tail = await ctx.client.events(ctx.session_id, full[-1].seq)
    check(tail.events == [] and tail.complete, "nothing after the last event, and complete is true")


MAX_CURSOR = 2**53


async def _session_tail(ctx: CaseContext) -> list[Event]:
    return (await ctx.client.events(ctx.session_id, 0)).events


async def _last_seq(ctx: CaseContext) -> int:
    """The session's last event seq; 0 for a session this run has not used yet."""
    events = await _session_tail(ctx)
    return events[-1].seq if events else 0


async def cursor_fence_and_gap(ctx: CaseContext) -> None:
    """``after_seq`` is also a fence: a cursor ahead of the log is a recorded history gap.

    The harness never numbers an event at or below a cursor it has seen. A cursor beyond its
    log is announced once as ``history_gap`` (from the next local seq through the cursor), numbered
    ``cursor + 1``. ``replay_after_seq`` only lowers where the read starts, never the fence.
    """
    ctx.script(say("fence setup"))
    await ctx.run_turn()
    full = await _session_tail(ctx)
    last = full[-1].seq
    exact = await ctx.client.events(ctx.session_id, last)
    check(exact.events == [], "a cursor at the last event must return nothing, not a gap")

    fence = last + 1000
    page = (await ctx.client.events(ctx.session_id, fence)).events
    check(
        [e.type for e in page] == ["history_gap"], f"cursor ahead returned {[e.type for e in page]}"
    )
    gap = page[0]
    expected = {"from_seq": last + 1, "to_seq": fence, "reason": "sandbox_restored"}
    check(gap.data == expected, f"history_gap data {gap.data}, expected {expected}")
    check(gap.seq == fence + 1, f"history_gap numbered {gap.seq}, expected {fence + 1}")
    again = (await ctx.client.events(ctx.session_id, fence)).events
    check([e.seq for e in again] == [gap.seq], "the same fence announced the gap again")

    everything = await _session_tail(ctx)
    check(_strictly_increasing(everything), "sequence not strictly increasing after a gap")
    check(sum(e.type == "history_gap" for e in everything) == 1, "more than one history_gap")
    start = full[-3].seq
    replay = await ctx.client.events(ctx.session_id, fence, replay_after_seq=start)
    check(
        [e.seq for e in replay.events] == [e.seq for e in everything if e.seq > start],
        "replay_after_seq did not select the replay start",
    )
    lower = await ctx.client.events(ctx.session_id, 0, replay_after_seq=fence + 500)
    check(
        [e.seq for e in lower.events] == [e.seq for e in everything],
        "replay above after_seq lowered nothing",
    )
    for bad in (-1, MAX_CURSOR + 1):
        answer = await ctx.client.events_response(ctx.session_id, bad)
        check(answer.status_code == 422, f"after_seq={bad} answered {answer.status_code}, not 422")
    answer = await ctx.client.events_response(ctx.session_id, 0, replay_after_seq=-1)
    check(answer.status_code == 422, f"replay_after_seq=-1 answered {answer.status_code}")


async def post_cursor_fence(ctx: CaseContext) -> None:
    """A POST carries the same fence: a stale one is harmless, one ahead of the log opens a gap."""
    ctx.script(say("stale fence"), say("ahead fence"))
    high = await _last_seq(ctx)

    stale = ctx.turn_request()
    check((await ctx.client.submit(stale, after_seq=0)).status_code == 202, "stale fence refused")
    run = await ctx.client.wait_ended(ctx.session_id, stale.turn_id)
    check(run[-1].data["outcome"] == "completed", f"outcome {run[-1].data}")
    check(min(e.seq for e in run) > high, "a stale after_seq renumbered events at or below the log")

    ahead_fence = await _last_seq(ctx) + 500
    ahead = ctx.turn_request()
    check(
        (await ctx.client.submit(ahead, after_seq=ahead_fence)).status_code == 202,
        "fence ahead of the log refused",
    )
    run = await ctx.client.wait_ended(ctx.session_id, ahead.turn_id)
    check(min(e.seq for e in run) > ahead_fence, "turn events were numbered at or below the fence")
    gaps = [e for e in await _session_tail(ctx) if e.type == "history_gap" and e.seq > ahead_fence]
    check(
        len(gaps) == 1 and gaps[0].seq == ahead_fence + 1,
        f"expected one history_gap at {ahead_fence + 1}, got {[g.seq for g in gaps]}",
    )
    bad = await ctx.client.submit(ctx.turn_request(), after_seq=MAX_CURSOR + 1)
    check(bad.status_code == 422, f"after_seq above the maximum answered {bad.status_code}")


async def _ask_for_approval(ctx: CaseContext, call_id: str) -> tuple[str, str, list[Event]]:
    ctx.script(call_tool(ctx.tool("write"), WRITE_CALL, call_id=call_id), say("finished"))
    request = ctx.turn_request(tool_policy={"write": "ask"})
    await ctx.start(request)
    events = await ctx.client.wait_event(ctx.session_id, request.turn_id, "requires_action")
    [action] = [e for e in events if e.type == "requires_action"]
    check(action.data["call_id"] == call_id, f"approval names call {action.data['call_id']}")
    check(action.data["name"] == "write", f"approval names tool {action.data['name']}")
    check(not any(e.type == "tool_result" for e in events), "tool ran before the decision")
    return request.turn_id, str(action.data["action_id"]), events


async def approval_allow(ctx: CaseContext) -> None:
    turn_id, action_id, _ = await _ask_for_approval(ctx, "allow-call")
    answer = await ctx.client.act(turn_id, ActionRequest(action_id=action_id, decision="allow"))
    check(answer.status_code == 200, f"allow answered {answer.status_code}")
    events = await ctx.client.wait_ended(ctx.session_id, turn_id)
    result = tool_result(events, "allow-call")
    check(result.data["is_error"] is False, f"allowed tool failed: {result.data['output']}")
    check(len(ctx.model_requests()) == 2, "model should continue after the tool result")


async def approval_deny(ctx: CaseContext) -> None:
    turn_id, action_id, _ = await _ask_for_approval(ctx, "deny-call")
    answer = await ctx.client.act(turn_id, ActionRequest(action_id=action_id, decision="deny"))
    check(answer.status_code == 200, f"deny answered {answer.status_code}")
    events = await ctx.client.wait_ended(ctx.session_id, turn_id)
    check(tool_result(events, "deny-call").data["is_error"] is True, "denied tool did not error")
    check(len(ctx.model_requests()) == 2, "model should see the refusal and continue")
    follow_up = text_of(ctx.model_requests()[1].history)
    check("deny-call" in follow_up, "the refusal did not reach the model")


async def approval_client_result(ctx: CaseContext) -> None:
    turn_id, action_id, _ = await _ask_for_approval(ctx, "result-call")
    supplied: dict[str, JsonValue] = {"supplied_by": "client", "n": 7}
    action = ActionRequest(action_id=action_id, decision="result", result=supplied)
    check((await ctx.client.act(turn_id, action)).status_code == 200, "result decision refused")
    events = await ctx.client.wait_ended(ctx.session_id, turn_id)
    result = tool_result(events, "result-call")
    check(result.data["is_error"] is False, "client result marked as error")
    check(result.data["output"] == supplied, f"tool_result output {result.data['output']}")
    check("supplied_by" in text_of(ctx.model_requests()[1].history), "model never saw the result")


async def action_rejections(ctx: CaseContext) -> None:
    turn_id, action_id, _ = await _ask_for_approval(ctx, "reject-call")
    unknown = await ctx.client.act(
        turn_id, ActionRequest(action_id="no-such-action", decision="allow")
    )
    check(unknown.status_code in {404, 409}, f"unknown action answered {unknown.status_code}")
    missing = await ctx.client.act(
        "no-such-turn", ActionRequest(action_id=action_id, decision="allow")
    )
    check(missing.status_code in {404, 409}, f"unknown turn answered {missing.status_code}")
    await ctx.client.act(turn_id, ActionRequest(action_id=action_id, decision="deny"))
    await ctx.client.wait_ended(ctx.session_id, turn_id)


async def cancel_turn(ctx: CaseContext) -> None:
    ctx.script(say("never delivered", delay_s=60))
    request = ctx.turn_request()
    await ctx.start(request)
    await ctx.wait_for_model_requests(1)
    check(await ctx.client.cancelled(request.turn_id), "cancel reported nothing to cancel")
    events = await ctx.client.wait_ended(ctx.session_id, request.turn_id)
    ended = events[-1]
    check(
        ended.type == "turn_ended" and ended.data["outcome"] == "interrupted", f"ended {ended.data}"
    )
    check(not any(e.type == "agent_message" for e in events), "cancelled turn produced a message")
    check(len(ctx.model_requests()) == 1, "cancel triggered another model request")
    check(
        (await ctx.client.cancel(request.turn_id)).status_code == 200, "repeat cancel not answered"
    )


async def usage_per_model_request(ctx: CaseContext) -> None:
    first = Usage(input_tokens=100, cached_input_tokens=25, output_tokens=10, reasoning_tokens=4)
    second = Usage(input_tokens=200, cached_input_tokens=50, output_tokens=20, reasoning_tokens=0)
    ctx.script(call_tool(ctx.tool("write"), WRITE_CALL, usage=first), say("done", usage=second))
    run = await ctx.run_turn()
    check(run.outcome == "completed", f"outcome {run.outcome}")
    starts, usages = run.of("model_request_started"), run.of("usage")
    check(
        len(starts) == len(usages) == 2 == len(ctx.model_requests()),
        "one start and one usage per model request",
    )
    check(
        [e.data["request_id"] for e in starts] == [e.data["request_id"] for e in usages],
        "usage request_ids do not match the starts",
    )
    check(len({str(e.data["request_id"]) for e in starts}) == 2, "request_ids are not unique")
    check(all(s.seq < u.seq for s, u in zip(starts, usages, strict=True)), "usage before its start")
    reported = [
        (
            u.data["input_tokens"],
            u.data["cached_input_tokens"],
            u.data["output_tokens"],
            u.data["reasoning_tokens"],
        )
        for u in usages
    ]
    scripted = [(100, 25, 10, 4), (200, 50, 20, 0)]
    check(reported == scripted, f"usage {reported}, scripted {scripted}")
    check(all(u.data["model"] == ctx.conversation for u in usages), "usage names another model")


async def skills_delivery(ctx: CaseContext) -> None:
    marker = f"SKILL-MARKER-{uuid4().hex}"
    skill = InlineSkill(name="conformance-skill", files={"SKILL.md": f"# Conformance\n{marker}\n"})
    ctx.script(say("skilled"))
    run = await ctx.run_turn(ctx.turn_request(skills=[skill]))
    check(run.outcome == "completed", f"outcome {run.outcome}")
    [seen] = ctx.model_requests()
    check(marker in seen.text(), "the skill content never reached the model")


async def mcp_delivery(ctx: CaseContext) -> None:
    if importlib.util.find_spec("uvicorn") is None:
        raise SkipCase("uvicorn is not installed, so no test MCP server can listen")
    mcp = TinyMCPServer()
    tool = ctx.target.mcp_tool_name(SERVER_NAME, "echo")
    async with serve_app(mcp) as url:
        server = MCPServer(name=SERVER_NAME, url=f"{url}/mcp")
        ctx.script(call_tool(tool, {"text": "hello-mcp"}, call_id="mcp-call"), say("done"))
        run = await ctx.run_turn(ctx.turn_request(mcp_servers=[server]))
        check(run.outcome == "completed", f"outcome {run.outcome}: {run.of('turn_ended')[-1].data}")
        check(
            tool in ctx.model_requests()[0].tool_names,
            f"{tool} not offered: {ctx.model_requests()[0].tool_names}",
        )
        check([c.get("name") for c in mcp.calls] == ["echo"], f"MCP server saw {mcp.calls}")
        result = tool_result(run.events, "mcp-call")
        check(result.data["is_error"] is False, f"MCP tool failed: {result.data['output']}")
        check("hello-mcp" in text_of(result.data["output"]), "MCP output missing")
        use = [e for e in run.events if e.type == "tool_use" and e.data["call_id"] == "mcp-call"]
        check(bool(use) and use[0].data["executor"] == "mcp", "tool_use should carry executor=mcp")

        ctx.script(say("again"))
        denied = ctx.turn_request(mcp_servers=[server], tool_policy={f"{SERVER_NAME}/*": "deny"})
        await ctx.run_turn(denied)
        offered = ctx.model_requests()[-1].tool_names
        check(tool not in offered, f"a denied MCP tool was offered to the model: {offered}")


async def no_builtin_tools(ctx: CaseContext) -> None:
    ctx.script(say("with builtins"), say("without builtins"))
    await ctx.run_turn(ctx.turn_request())
    builtins = {ctx.tool(name) for name in BUILTIN_TOOLS}
    offered = set(ctx.model_requests()[0].tool_names)
    check(builtins <= offered, f"default turn should offer built-ins, offered {sorted(offered)}")
    none = {name: "deny" for name in BUILTIN_TOOLS}
    await ctx.run_turn(ctx.turn_request(tool_policy=none))  # type: ignore[arg-type]
    leaked = builtins & set(ctx.model_requests()[1].tool_names)
    check(not leaked, f"built-in tools reached the model with tools=[]: {sorted(leaked)}")


async def restart_without_rerun(ctx: CaseContext) -> None:
    if ctx.target.restart is None:
        raise SkipCase("no restart hook supplied by the caller")
    marker = f"RAN-ONCE-{uuid4().hex[:8]}"
    command = f"echo {marker} >> conformance-marker.txt; sleep 10; echo finished"
    # No second step: a model call after the restart would find the script exhausted.
    ctx.script(call_tool(ctx.tool("bash"), {"command": command}, call_id="slow-call"))
    request = ctx.turn_request("restart-input")
    await ctx.start(request)
    before = await ctx.client.wait_event(ctx.session_id, request.turn_id, "tool_use")
    await _wait_a_moment(1.0)
    check(len(ctx.model_requests()) == 1, "model should have been called exactly once so far")
    check(len(_of(before, "usage")) == 1, "first model request should have one usage event")

    new_url = await ctx.target.restart()
    if new_url:
        ctx.target.base_url = ctx.client.base_url = new_url

    after = await ctx.client.wait_ended(ctx.session_id, request.turn_id)
    check(after[-1].data["outcome"] == "interrupted", f"restart ended the turn as {after[-1].data}")
    check(len(ctx.model_requests()) == 1, "the model was called again after restart")
    check(len(_of(after, "tool_use")) == 1, "the tool was started again after restart")
    check(len(_of(after, "usage")) == 1, "usage was counted more than once")
    check(len(_of(after, "model_request_started")) == 1, "a model request was started again")
    check(_strictly_increasing(after), "sequence broke across the restart")

    replay = await ctx.client.submit(request)
    check(replay.status_code == 202, f"replay after restart answered {replay.status_code}")
    again = await ctx.client.wait_ended(ctx.session_id, request.turn_id)
    check([e.seq for e in again] == [e.seq for e in after], "replay after restart changed the log")
    check(len(ctx.model_requests()) == 1, "replay after restart called the model")

    ctx.script(
        call_tool(ctx.tool("bash"), {"command": "cat conformance-marker.txt"}, call_id="cat-call"),
        say("ok"),
    )
    follow = await ctx.run_turn(ctx.turn_request("what ran"))
    seen = text_of(tool_result(follow.events, "cat-call").data["output"])
    check(
        seen.count(marker) == 1, f"tool side effect ran {seen.count(marker)} times, expected once"
    )


def _of(events: list[Event], event_type: str) -> list[Event]:
    return [event for event in events if event.type == event_type]


async def _wait_a_moment(seconds: float) -> None:
    await asyncio.sleep(seconds)


CASES: tuple[Case, ...] = (
    Case("health_and_auth", health_and_auth),
    Case("turn_lifecycle", turn_lifecycle),
    Case("sse_stream", sse_stream),
    Case("idempotent_replay", idempotent_replay),
    Case("events_by_sequence", events_by_sequence),
    Case("approval_allow", approval_allow),
    Case("approval_deny", approval_deny),
    Case("approval_client_result", approval_client_result),
    Case("action_rejections", action_rejections),
    Case("cancel_turn", cancel_turn),
    Case("cursor_fence_and_gap", cursor_fence_and_gap),
    Case("post_cursor_fence", post_cursor_fence),
    Case("usage_per_model_request", usage_per_model_request),
    Case("skills_delivery", skills_delivery),
    Case("mcp_delivery", mcp_delivery),
    Case("no_builtin_tools", no_builtin_tools),
    Case("restart_without_rerun", restart_without_rerun),
)
