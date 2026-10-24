"""Scripted, offline fake of the OpenAI Responses, Chat Completions and Anthropic Messages APIs.

Pure ASGI on httpx and pydantic only. Mount ``FakeModelServer`` in-process with
``httpx.ASGITransport``, or serve it on loopback with ``mux.testing.fake_model_server.runner.serve``
and point any engine at the base URL.
"""

from .app import CONTROL_PREFIX, FakeModelServer, route_api
from .script import (
    CONVERSATION_HEADER,
    DEFAULT_CONVERSATION,
    Fail,
    RecordedRequest,
    Reply,
    ScriptBook,
    Step,
    ToolCall,
    Usage,
    call_tool,
    fail,
    say,
)

__all__ = [
    "CONTROL_PREFIX",
    "CONVERSATION_HEADER",
    "DEFAULT_CONVERSATION",
    "Fail",
    "FakeModelServer",
    "RecordedRequest",
    "Reply",
    "ScriptBook",
    "Step",
    "ToolCall",
    "Usage",
    "call_tool",
    "fail",
    "route_api",
    "say",
]
