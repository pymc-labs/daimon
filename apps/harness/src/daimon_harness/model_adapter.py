"""All model/SDK-specific translation and Azure credentials live at this edge."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack
from typing import Literal, Protocol, cast, overload
from uuid import uuid4

import httpx
from agents import Agent, RunConfig, Runner
from agents.agent_output import AgentOutputSchemaBase
from agents.handoffs import Handoff
from agents.items import ModelResponse, TResponseInputItem
from agents.mcp import MCPServerStreamableHttp
from agents.model_settings import ModelSettings
from agents.models.interface import ModelTracing
from agents.models.openai_responses import OpenAIResponsesModel
from agents.tool import FunctionTool, Tool
from agents.tool_context import ToolContext
from azure.identity.aio import get_bearer_token_provider
from openai import AsyncAzureOpenAI, AsyncOpenAI, BadRequestError
from openai.types.responses import Response, ResponseStreamEvent
from openai.types.responses.response_prompt_param import ResponsePromptParam
from pydantic import JsonValue, TypeAdapter

from .credentials import (
    credential_log_redaction,
    mcp_headers,
    process_credentials,
    remember_log_secret,
)
from .errors import HarnessError
from .identity import AsyncIdentity, CapturedIdentity
from .isolation import validate_credential_sources
from .runtime import TurnContext

JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


def bound_mcp_http_client(
    headers: dict[str, str] | None = None,
    timeout: httpx.Timeout | None = None,
    auth: httpx.Auth | None = None,
) -> httpx.AsyncClient:
    # Redirects must never forward operator headers to an unbound origin.
    return httpx.AsyncClient(
        headers=headers,
        timeout=timeout or httpx.Timeout(30, read=300),
        auth=auth,
        follow_redirects=False,
    )


class ModelAdapter(Protocol):
    async def run(self, context: TurnContext, history: list[JsonValue]) -> list[JsonValue]: ...


class MeteredResponsesModel(OpenAIResponsesModel):
    def __init__(self, context: TurnContext, client: AsyncOpenAI) -> None:
        super().__init__(model=context.request.model, openai_client=client)
        self.context = context
        self.response_error: HarnessError | None = None

    @overload
    async def _fetch_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        previous_response_id: str | None,
        conversation_id: str | None,
        stream: Literal[True],
        prompt: ResponsePromptParam | None = None,
    ) -> AsyncIterator[ResponseStreamEvent]: ...

    @overload
    async def _fetch_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        previous_response_id: str | None,
        conversation_id: str | None,
        stream: Literal[False],
        prompt: ResponsePromptParam | None = None,
    ) -> Response: ...

    async def _fetch_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        previous_response_id: str | None = None,
        conversation_id: str | None = None,
        stream: Literal[True, False] = False,
        prompt: ResponsePromptParam | None = None,
    ) -> Response | AsyncIterator[ResponseStreamEvent]:
        response = await super()._fetch_response(
            system_instructions,
            input,
            model_settings,
            tools,
            output_schema,
            handoffs,
            previous_response_id,
            conversation_id,
            stream,
            prompt,
        )
        if isinstance(response, Response):
            # Provider-edge receipt excludes prompts, outputs and credentials.
            print(
                json.dumps(
                    {
                        "event": "harness.azure_usage",
                        "response_id": response.id,
                        "model": response.model,
                        "usage": response.usage.model_dump(mode="json") if response.usage else None,
                    }
                ),
                flush=True,
            )
        # The SDK's ModelResponse conversion drops status and provider errors.
        if isinstance(response, Response) and response.status != "completed":
            status = response.status or "unknown"
            if status == "incomplete":
                details = response.incomplete_details
                reason = details.reason if details and details.reason else "unknown"
                self.response_error = HarnessError(
                    "model_incomplete", "Model response incomplete: " + reason
                )
            else:
                error = response.error
                reason = f"{error.code}: {error.message}" if error else "unknown"
                self.response_error = HarnessError(
                    "model_failed", f"Model response {status}: {reason}"
                )
        return response

    async def get_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        previous_response_id: str | None = None,
        conversation_id: str | None = None,
        prompt: ResponsePromptParam | None = None,
    ) -> ModelResponse:
        self.context.model_input = cast(
            list[JsonValue],
            JSON.validate_python(
                [{"role": "user", "content": input}] if isinstance(input, str) else input
            ),
        )
        self.response_error = None
        request_id = str(uuid4())
        await self.context.emit(
            "model_request_started", {"request_id": request_id, "model": self.context.request.model}
        )
        try:
            response = await super().get_response(
                system_instructions,
                input,
                model_settings,
                [
                    tool
                    for tool in tools
                    if not isinstance(tool, FunctionTool)
                    or self.context.permission(self.policy_name(tool.name)) != "deny"
                ],
                output_schema,
                handoffs,
                tracing,
                previous_response_id=previous_response_id,
                conversation_id=conversation_id,
                prompt=prompt,
            )
        except (Exception, asyncio.CancelledError) as exc:
            await self.context.emit(
                "error",
                {
                    "kind": "usage_unavailable",
                    "message": "Model request ended without usage",
                    "retryable": False,
                    "request_id": request_id,
                },
            )
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise HarnessError("usage_unavailable", "Model request ended without usage") from exc
        usage = response.usage
        if usage.requests != 1 or not response.response_id:
            await self.context.emit(
                "error",
                {
                    "kind": "usage_unavailable",
                    "message": "Model response did not report usage",
                    "retryable": False,
                    "request_id": request_id,
                },
            )
            raise HarnessError("usage_missing", "Model response did not report usage")
        await self.context.emit(
            "usage",
            {
                "request_id": request_id,
                "model": self.context.request.model,
                "input_tokens": usage.input_tokens,
                "cached_input_tokens": usage.input_tokens_details.cached_tokens,
                "output_tokens": usage.output_tokens,
                "reasoning_tokens": usage.output_tokens_details.reasoning_tokens,
            },
        )
        for item in list(response.output):
            if item.type == "function_call":
                name = self.policy_name(item.name)
                if self.context.permission(name) == "deny" and not any(
                    isinstance(tool, FunctionTool) and tool.name == item.name for tool in tools
                ):
                    # Keep the model's call and reasoning intact. Register only a
                    # local rejection handler, omitted from every model request.
                    def rejection(tool_name: str, policy_name: str) -> FunctionTool:
                        async def invoke(ctx: ToolContext[TurnContext], arguments: str) -> str:
                            async def never_execute() -> JsonValue:
                                raise AssertionError("denied tool must not execute")

                            output = await self.context.tool(
                                ctx.tool_call_id,
                                policy_name,
                                {"arguments": arguments},
                                never_execute,
                                mcp="/" in policy_name,
                            )
                            return json.dumps(output)

                        return FunctionTool(
                            name=tool_name,
                            description="Local policy rejection",
                            params_json_schema={"type": "object", "properties": {}},
                            strict_json_schema=False,
                            on_invoke_tool=invoke,
                        )

                    tools.append(rejection(item.name, name))
            if item.type == "message":
                text = "".join(part.text for part in item.content if part.type == "output_text")
                message_id = str(uuid4())
                await self.context.emit(
                    "agent_message_delta", {"message_id": message_id, "text": text}
                )
                await self.context.emit(
                    "agent_message",
                    {"message_id": message_id, "parts": [{"type": "text", "text": text}]},
                )
        if self.response_error is not None:
            raise self.response_error
        return response

    def policy_name(self, name: str) -> str:
        for server in sorted(
            self.context.request.mcp_servers, key=lambda config: len(config.name), reverse=True
        ):
            prefix = server.name + "__"
            if name.startswith(prefix):
                return server.name + "/" + name[len(prefix) :]
        return name


def builtin_tools(context: TurnContext, *, include_denied: bool = False) -> list[Tool]:
    schemas = {
        "bash": {"command": {"type": "string"}},
        "read": {"path": {"type": "string"}},
        "write": {"path": {"type": "string"}, "content": {"type": "string"}},
        "edit": {"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}},
    }
    tools: list[Tool] = []
    for name, properties in schemas.items():
        if not include_denied and context.permission(name) == "deny":
            continue

        def make_tool(tool_name: str, fields: dict[str, dict[str, str]]) -> FunctionTool:
            async def invoke(ctx: ToolContext[TurnContext], arguments: str) -> str:
                args = cast(dict[str, JsonValue], JSON.validate_json(arguments))

                async def execute() -> JsonValue:
                    match tool_name:
                        case "bash":
                            return await context.bash(str(args["command"]))
                        case "read" | "write" | "edit":
                            return await context.async_file_operation(
                                tool_name,
                                str(args["path"]),
                                content=str(args.get("content", "")),
                                old=str(args.get("old", "")),
                                new=str(args.get("new", "")),
                            )
                        case _:
                            raise ValueError("unknown builtin tool")

                return json.dumps(await context.tool(ctx.tool_call_id, tool_name, args, execute))

            return FunctionTool(
                name=tool_name,
                description=f"{tool_name} in the persistent workspace",
                params_json_schema={
                    "type": "object",
                    "properties": fields,
                    "required": list(fields),
                    "additionalProperties": False,
                },
                on_invoke_tool=invoke,
            )

        tools.append(make_tool(name, properties))
    return tools


async def context_tools(
    context: TurnContext,
    stack: AsyncExitStack,
    *,
    include_denied: bool = False,
    policy_names: dict[str, str] | None = None,
) -> list[Tool]:
    """Share the harness executors and MCP credential boundary between loops."""
    tools = builtin_tools(context, include_denied=include_denied)
    if policy_names is not None:
        policy_names.update(
            {tool.name: tool.name for tool in tools if isinstance(tool, FunctionTool)}
        )
    for config in context.request.mcp_servers:
        headers = mcp_headers(config, context.credentials, context.credential_bindings)
        server = await stack.enter_async_context(
            MCPServerStreamableHttp(
                name=config.name,
                params={
                    "url": config.url,
                    "headers": headers,
                    "httpx_client_factory": bound_mcp_http_client,
                },
                cache_tools_list=True,
            )
        )
        for remote in await server.list_tools():
            if not include_denied and context.permission(f"{config.name}/{remote.name}") == "deny":
                continue

            def make_remote(
                connection: MCPServerStreamableHttp,
                name: str,
                description: str,
                schema: dict[str, object],
            ) -> FunctionTool:
                async def invoke(ctx: ToolContext[TurnContext], arguments: str) -> str:
                    args = cast(dict[str, JsonValue], JSON.validate_json(arguments))

                    async def execute() -> JsonValue:
                        result = await connection.call_tool(name, args)
                        if result.isError:
                            raise ValueError("MCP tool returned an error")
                        return JSON.validate_python(result.model_dump(mode="json"))

                    return json.dumps(
                        await context.tool(
                            ctx.tool_call_id,
                            f"{connection.name}/{name}",
                            args,
                            execute,
                            mcp=True,
                        )
                    )

                return FunctionTool(
                    name=f"{connection.name}__{name}",
                    description=description,
                    params_json_schema=schema,
                    on_invoke_tool=invoke,
                    strict_json_schema=False,
                )

            if policy_names is not None:
                alias = f"{config.name}__{remote.name}"
                if alias in policy_names:
                    raise HarnessError("ambiguous_tool_name", "MCP function aliases collide")
                policy_names[alias] = f"{config.name}/{remote.name}"
            tools.append(
                make_remote(
                    server,
                    remote.name,
                    remote.description or "MCP tool",
                    remote.inputSchema,
                )
            )
    return tools


class AzureModelAdapter:
    def __init__(self) -> None:
        validate_credential_sources()
        self.log_credentials = process_credentials()
        self.live_probe = os.environ.get("HARNESS_LIVE_PROBE", "")
        if self.live_probe not in {"", "openai"}:
            raise RuntimeError("HARNESS_LIVE_PROBE must be empty or openai")
        if self.live_probe:
            if os.environ.get("HARNESS_RUNTIME_MODE", "production") != "probe":
                raise RuntimeError("OpenAI live provider is forbidden in production mode")
            if any(
                value and (name.startswith("AZURE_") or name.startswith("IDENTITY_"))
                for name, value in os.environ.items()
            ):
                raise RuntimeError("OpenAI live provider cannot override Azure configuration")
            self.key = os.environ.get("OPENAI_API_KEY")
            if not self.key:
                raise RuntimeError("OpenAI live probe requires an explicitly supplied API key")
            self.credential = None
            self.endpoint = "https://api.openai.com/v1"
            self.api_version = ""
            return
        self.key = os.environ.get("AZURE_OPENAI_API_KEY")
        if not self.key and not (
            os.environ.get("IDENTITY_ENDPOINT") and os.environ.get("IDENTITY_HEADER")
        ):
            raise RuntimeError("managed identity requires a header-protected IDENTITY_ENDPOINT")
        self.identity = CapturedIdentity() if not self.key else None
        self.credential = AsyncIdentity(self.identity) if self.identity is not None else None
        self.endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT", "")
        self.api_version = os.environ.get("AZURE_OPENAI_API_VERSION", "2025-04-01-preview")

    def validate_probe_model(self, context: TurnContext) -> None:
        if self.live_probe and context.request.model != "gpt-6-luna":
            raise HarnessError("probe_model_refused", "Live probes require gpt-6-luna")

    async def run(self, context: TurnContext, history: list[JsonValue]) -> list[JsonValue]:
        self.validate_probe_model(context)
        with credential_log_redaction(
            self.log_credentials | context.credentials
        ) as log_credentials:
            return await self._run(context, history, log_credentials)

    async def _run(
        self, context: TurnContext, history: list[JsonValue], log_credentials: dict[str, str]
    ) -> list[JsonValue]:
        async with AsyncExitStack() as stack:
            key = self.key
            token_provider: Callable[[], Awaitable[str]] | None = None
            if not key:
                credential = self.credential
                assert credential is not None
                azure_token_provider = get_bearer_token_provider(
                    credential,
                    "https://cognitiveservices.azure.com/.default",
                )

                async def azure_bearer_token() -> str:
                    token = await azure_token_provider()
                    remember_log_secret(log_credentials, "azure_bearer", token)
                    return token

                token_provider = azure_bearer_token

            model_client = (
                AsyncOpenAI(api_key=key, base_url=self.endpoint, max_retries=0)
                if self.live_probe
                else AsyncAzureOpenAI(
                    azure_endpoint=self.endpoint,
                    api_version=self.api_version,
                    api_key=key,
                    azure_ad_token_provider=token_provider,
                    max_retries=0,
                )
            )
            client = await stack.enter_async_context(model_client)
            tools = await context_tools(context, stack)
            parts: list[dict[str, JsonValue]] = []
            for part in context.request.input:
                if part.type == "text":
                    parts.append({"type": "input_text", "text": part.text})
                else:
                    parts.append(
                        {
                            "type": "input_image",
                            "image_url": f"data:{part.media_type};base64,{part.data_b64}",
                            "detail": "auto",
                        }
                    )
            items = cast(list[TResponseInputItem], history + [{"role": "user", "content": parts}])
            instructions = context.request.instructions + "\n\n" + await context.skills()
            agent = Agent[TurnContext](
                name="Daimon",
                instructions=instructions,
                tools=tools,
                model=MeteredResponsesModel(context, client),
                model_settings=ModelSettings(
                    parallel_tool_calls=False,
                    store=False,
                    response_include=["reasoning.encrypted_content"],
                ),
            )
            for attempt in range(2):
                try:
                    result = await Runner.run(
                        agent,
                        input=items,
                        context=context,
                        max_turns=context.request.limits.max_tool_calls + 2,
                        run_config=RunConfig(tracing_disabled=True),
                    )
                    return cast(list[JsonValue], JSON.validate_python(result.to_input_list()))
                except Exception as exc:
                    root = exc
                    while root.__cause__ is not None and not isinstance(root, HarnessError):
                        root = root.__cause__
                    if isinstance(root, HarnessError) and root.kind == "tool_limit":
                        raise HarnessError("tool_limit", "Maximum tool calls exceeded") from exc
                    if isinstance(root, HarnessError) and root.kind in {
                        "model_incomplete",
                        "model_failed",
                    }:
                        raise HarnessError(root.kind, str(root)) from exc
                    cause = root.__cause__
                    if not (
                        attempt == 0
                        and isinstance(cause, BadRequestError)
                        and cause.code in {"context_length_exceeded", "context_window_exceeded"}
                    ):
                        raise
                    # Preserve reasoning and paired calls/results so the stateless
                    # retry stays valid and cannot repeat completed side effects.
                    # Each attempt retains its usage inventory.
                    await context.emit(
                        "error",
                        {
                            "kind": "context_truncated",
                            "message": "Context limit reached; older history was removed",
                            "retryable": True,
                        },
                    )
                    recent = context.model_input[len(history) :]
                    compact: list[JsonValue] = []
                    for item in recent:
                        if isinstance(item, dict) and item.get("type") == "function_call_output":
                            item = dict(item)
                            output = item.get("output")
                            if isinstance(output, str) and len(output) > 8192:
                                item["output"] = output[:8192] + "\n[output truncated for context]"
                        compact.append(item)
                    items = cast(list[TResponseInputItem], compact)
            raise AssertionError("context recovery exhausted")
