"""Wire shapes: turn a scripted step into each API's JSON body or SSE frames."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from typing import Any, Literal

from .script import Fail, Reply, ToolCall, Usage, as_dict

Api = Literal["responses", "chat_completions", "anthropic_messages"]
Frame = tuple[str | None, dict[str, Any] | str]
"""One SSE frame: optional event name and a JSON payload (or a raw string such as [DONE])."""

_CHUNK = re.compile(r"\S+\s*|\s+")


def text_chunks(text: str) -> list[str]:
    return _CHUNK.findall(text)


def encode_frame(frame: Frame) -> bytes:
    event, data = frame
    payload = data if isinstance(data, str) else json.dumps(data, separators=(",", ":"))
    head = f"event: {event}\n" if event else ""
    return f"{head}data: {payload}\n\n".encode()


class Ids:
    """Deterministic identifiers, so assertions can name what the model returned."""

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}

    def next(self, prefix: str) -> str:
        self._counts[prefix] = self._counts.get(prefix, 0) + 1
        return f"{prefix}_{self._counts[prefix]}"


def resolved_calls(reply: Reply, ids: Ids) -> list[ToolCall]:
    return [
        call if call.call_id else call.model_copy(update={"call_id": ids.next("call")})
        for call in reply.tool_calls
    ]


# --- OpenAI Responses ---------------------------------------------------------------------


def _responses_usage(usage: Usage) -> dict[str, Any]:
    return {
        "input_tokens": usage.input_tokens,
        "input_tokens_details": {"cached_tokens": usage.cached_input_tokens},
        "output_tokens": usage.output_tokens,
        "output_tokens_details": {"reasoning_tokens": usage.reasoning_tokens},
        "total_tokens": usage.input_tokens + usage.output_tokens,
    }


def _responses_items(reply: Reply, calls: list[ToolCall], ids: Ids) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if reply.text or not calls:
        items.append(
            {
                "id": ids.next("msg"),
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": reply.text, "annotations": []}],
            }
        )
    for call in calls:
        items.append(
            {
                "id": ids.next("fc"),
                "type": "function_call",
                "call_id": call.call_id,
                "name": call.name,
                "arguments": json.dumps(call.arguments),
                "status": "completed",
            }
        )
    return items


def _responses_object(
    response_id: str, model: str, status: str, output: list[dict[str, Any]], usage: Usage | None
) -> dict[str, Any]:
    return {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "model": model,
        "status": status,
        "output": output,
        "usage": _responses_usage(usage) if usage else None,
        "parallel_tool_calls": False,
        "tool_choice": "auto",
        "tools": [],
    }


def responses_body(reply: Reply, model: str, ids: Ids) -> dict[str, Any]:
    calls = resolved_calls(reply, ids)
    output = _responses_items(reply, calls, ids)
    return _responses_object(ids.next("resp"), model, "completed", output, reply.usage)


def responses_frames(reply: Reply, model: str, ids: Ids) -> list[Frame]:
    calls = resolved_calls(reply, ids)
    output = _responses_items(reply, calls, ids)
    response_id = ids.next("resp")
    frames: list[Frame] = []

    def emit(kind: str, **fields: object) -> None:
        frames.append((kind, {"type": kind, "sequence_number": len(frames), **fields}))

    skeleton = _responses_object(response_id, model, "in_progress", [], None)
    emit("response.created", response=skeleton)
    emit("response.in_progress", response=skeleton)
    for index, item in enumerate(output):
        if item["type"] == "message":
            text = item["content"][0]["text"]
            emit(
                "response.output_item.added",
                output_index=index,
                item={**item, "status": "in_progress", "content": []},
            )
            empty: dict[str, Any] = {"type": "output_text", "text": "", "annotations": []}
            emit(
                "response.content_part.added",
                item_id=item["id"],
                output_index=index,
                content_index=0,
                part=empty,
            )
            for chunk in text_chunks(text):
                emit(
                    "response.output_text.delta",
                    item_id=item["id"],
                    output_index=index,
                    content_index=0,
                    delta=chunk,
                    logprobs=[],
                )
            emit(
                "response.output_text.done",
                item_id=item["id"],
                output_index=index,
                content_index=0,
                text=text,
                logprobs=[],
            )
            emit(
                "response.content_part.done",
                item_id=item["id"],
                output_index=index,
                content_index=0,
                part=item["content"][0],
            )
        else:
            emit(
                "response.output_item.added",
                output_index=index,
                item={**item, "status": "in_progress", "arguments": ""},
            )
            emit(
                "response.function_call_arguments.delta",
                item_id=item["id"],
                output_index=index,
                delta=item["arguments"],
            )
            emit(
                "response.function_call_arguments.done",
                item_id=item["id"],
                output_index=index,
                name=item["name"],
                arguments=item["arguments"],
            )
        emit("response.output_item.done", output_index=index, item=item)
    emit(
        "response.completed",
        response=_responses_object(response_id, model, "completed", output, reply.usage),
    )
    return frames


# --- OpenAI Chat Completions --------------------------------------------------------------


def _chat_usage(usage: Usage) -> dict[str, Any]:
    return {
        "prompt_tokens": usage.input_tokens,
        "completion_tokens": usage.output_tokens,
        "total_tokens": usage.input_tokens + usage.output_tokens,
        "prompt_tokens_details": {"cached_tokens": usage.cached_input_tokens},
        "completion_tokens_details": {"reasoning_tokens": usage.reasoning_tokens},
    }


def _chat_finish(calls: list[ToolCall]) -> str:
    return "tool_calls" if calls else "stop"


def chat_body(reply: Reply, model: str, ids: Ids) -> dict[str, Any]:
    calls = resolved_calls(reply, ids)
    message: dict[str, Any] = {"role": "assistant", "content": reply.text or None}
    if calls:
        message["tool_calls"] = [
            {
                "id": call.call_id,
                "type": "function",
                "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
            }
            for call in calls
        ]
    return {
        "id": ids.next("chatcmpl"),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": _chat_finish(calls)}],
        "usage": _chat_usage(reply.usage),
    }


def chat_frames(reply: Reply, model: str, ids: Ids, *, include_usage: bool) -> list[Frame]:
    calls = resolved_calls(reply, ids)
    completion_id, created = ids.next("chatcmpl"), int(time.time())

    def chunk(delta: dict[str, Any], finish: str | None = None, **extra: object) -> Frame:
        body = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            **extra,
        }
        return (None, body)

    frames = [chunk({"role": "assistant", "content": ""})]
    frames += [chunk({"content": piece}) for piece in text_chunks(reply.text)]
    for index, call in enumerate(calls):
        head = {
            "index": index,
            "id": call.call_id,
            "type": "function",
            "function": {"name": call.name, "arguments": ""},
        }
        frames.append(chunk({"tool_calls": [head]}))
        args = {"index": index, "function": {"arguments": json.dumps(call.arguments)}}
        frames.append(chunk({"tool_calls": [args]}))
    frames.append(chunk({}, _chat_finish(calls)))
    if include_usage:
        usage_chunk: dict[str, Any] = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [],
            "usage": _chat_usage(reply.usage),
        }
        frames.append((None, usage_chunk))
    frames.append((None, "[DONE]"))
    return frames


# --- Anthropic Messages -------------------------------------------------------------------


def _anthropic_usage(usage: Usage, *, output_tokens: int) -> dict[str, Any]:
    return {
        "input_tokens": max(0, usage.input_tokens - usage.cached_input_tokens),
        "output_tokens": output_tokens,
        "cache_read_input_tokens": usage.cached_input_tokens,
        "cache_creation_input_tokens": 0,
    }


def _anthropic_blocks(reply: Reply, calls: list[ToolCall]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    if reply.text or not calls:
        blocks.append({"type": "text", "text": reply.text})
    blocks += [
        {"type": "tool_use", "id": call.call_id, "name": call.name, "input": call.arguments}
        for call in calls
    ]
    return blocks


def _anthropic_stop(calls: list[ToolCall]) -> str:
    return "tool_use" if calls else "end_turn"


def anthropic_body(reply: Reply, model: str, ids: Ids) -> dict[str, Any]:
    calls = resolved_calls(reply, ids)
    return {
        "id": ids.next("msg"),
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": _anthropic_blocks(reply, calls),
        "stop_reason": _anthropic_stop(calls),
        "stop_sequence": None,
        "usage": _anthropic_usage(reply.usage, output_tokens=reply.usage.output_tokens),
    }


def anthropic_frames(reply: Reply, model: str, ids: Ids) -> list[Frame]:
    calls = resolved_calls(reply, ids)
    blocks = _anthropic_blocks(reply, calls)
    frames: list[Frame] = []

    def emit(kind: str, **fields: object) -> None:
        frames.append((kind, {"type": kind, **fields}))

    message: dict[str, Any] = {
        "id": ids.next("msg"),
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": _anthropic_usage(reply.usage, output_tokens=0),
    }
    emit("message_start", message=message)
    for index, block in enumerate(blocks):
        if block["type"] == "text":
            emit("content_block_start", index=index, content_block={"type": "text", "text": ""})
            for piece in text_chunks(block["text"]):
                emit(
                    "content_block_delta",
                    index=index,
                    delta={"type": "text_delta", "text": piece},
                )
        else:
            start: dict[str, Any] = {**block, "input": {}}
            emit("content_block_start", index=index, content_block=start)
            emit(
                "content_block_delta",
                index=index,
                delta={"type": "input_json_delta", "partial_json": json.dumps(block["input"])},
            )
        emit("content_block_stop", index=index)
    emit(
        "message_delta",
        delta={"stop_reason": _anthropic_stop(calls), "stop_sequence": None},
        usage={"output_tokens": reply.usage.output_tokens},
    )
    emit("message_stop")
    return frames


# --- Errors -------------------------------------------------------------------------------

_OPENAI_ERRORS = {
    400: ("invalid_request_error", None),
    401: ("invalid_request_error", "invalid_api_key"),
    403: ("invalid_request_error", None),
    404: ("invalid_request_error", None),
    429: ("rate_limit_error", "rate_limit_exceeded"),
}
_ANTHROPIC_ERRORS = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    413: "request_too_large",
    429: "rate_limit_error",
    529: "overloaded_error",
}


def error_body(api: Api, failure: Fail) -> dict[str, Any]:
    if api == "anthropic_messages":
        kind = failure.error_type or _ANTHROPIC_ERRORS.get(failure.status, "api_error")
        return {"type": "error", "error": {"type": kind, "message": failure.message}}
    default_kind, code = _OPENAI_ERRORS.get(failure.status, ("server_error", "server_error"))
    return {
        "error": {
            "message": failure.message,
            "type": failure.error_type or default_kind,
            "param": None,
            "code": code,
        }
    }


Renderer = Callable[[Reply, str, Ids], dict[str, Any]]
BODY_RENDERERS: dict[Api, Renderer] = {
    "responses": responses_body,
    "chat_completions": chat_body,
    "anthropic_messages": anthropic_body,
}


def stream_frames(
    api: Api, reply: Reply, model: str, ids: Ids, request_body: dict[str, Any]
) -> list[Frame]:
    if api == "responses":
        return responses_frames(reply, model, ids)
    if api == "chat_completions":
        options = as_dict(request_body.get("stream_options"))
        return chat_frames(reply, model, ids, include_usage=bool(options.get("include_usage")))
    return anthropic_frames(reply, model, ids)
