"""All model/SDK-specific translation and Azure credentials live at this edge."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import AsyncExitStack
from typing import Protocol, cast
from uuid import uuid4

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
from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider
from openai import AsyncAzureOpenAI
from openai.types.responses.response_prompt_param import ResponsePromptParam
from pydantic import JsonValue, TypeAdapter

from .errors import HarnessError
from .runtime import TurnContext

JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


class ModelAdapter(Protocol):
    async def run(self, context: TurnContext, history: list[JsonValue]) -> list[JsonValue]: ...


class MeteredResponsesModel(OpenAIResponsesModel):
    def __init__(self, context: TurnContext, client: AsyncAzureOpenAI) -> None:
        super().__init__(model=context.request.model, openai_client=client)
        self.context = context

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
        request_id = str(uuid4())
        self.context.emit(
            "model_request_started", {"request_id": request_id, "model": self.context.request.model}
        )
        try:
            response = await super().get_response(
                system_instructions,
                input,
                model_settings,
                tools,
                output_schema,
                handoffs,
                tracing,
                previous_response_id=previous_response_id,
                conversation_id=conversation_id,
                prompt=prompt,
            )
        except (Exception, asyncio.CancelledError) as exc:
            self.context.emit(
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
            self.context.emit(
                "error",
                {
                    "kind": "usage_unavailable",
                    "message": "Model response did not report usage",
                    "retryable": False,
                    "request_id": request_id,
                },
            )
            raise HarnessError("usage_missing", "Model response did not report usage")
        self.context.emit(
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
        for item in response.output:
            if item.type == "message":
                text = "".join(part.text for part in item.content if part.type == "output_text")
                self.context.emit("agent_message_delta", {"text": text})
                self.context.emit("agent_message", {"parts": [{"type": "text", "text": text}]})
        return response


def builtin_tools(context: TurnContext) -> list[Tool]:
    schemas = {
        "bash": {"command": {"type": "string"}},
        "read": {"path": {"type": "string"}},
        "write": {"path": {"type": "string"}, "content": {"type": "string"}},
        "edit": {"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}},
    }
    tools: list[Tool] = []
    for name, properties in schemas.items():

        def make_tool(tool_name: str, fields: dict[str, dict[str, str]]) -> FunctionTool:
            async def invoke(ctx: ToolContext[TurnContext], arguments: str) -> str:
                args = cast(dict[str, JsonValue], JSON.validate_json(arguments))

                async def execute() -> JsonValue:
                    match tool_name:
                        case "bash":
                            return await context.bash(str(args["command"]))
                        case "read":
                            return context.read(str(args["path"]))
                        case "write":
                            return context.write(str(args["path"]), str(args["content"]))
                        case "edit":
                            return context.edit(
                                str(args["path"]), str(args["old"]), str(args["new"])
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


class AzureModelAdapter:
    async def run(self, context: TurnContext, history: list[JsonValue]) -> list[JsonValue]:
        async with AsyncExitStack() as stack:
            key = os.environ.get("AZURE_OPENAI_API_KEY")
            token_provider = None
            if not key:
                credential = await stack.enter_async_context(DefaultAzureCredential())
                token_provider = get_bearer_token_provider(
                    credential,
                    "https://cognitiveservices.azure.com/.default",
                )
            client = await stack.enter_async_context(
                AsyncAzureOpenAI(
                    azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
                    api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2025-04-01-preview"),
                    api_key=key,
                    azure_ad_token_provider=token_provider,
                    max_retries=0,
                )
            )
            tools = builtin_tools(context)
            for config in context.request.mcp_servers:
                headers = {header: os.environ[env] for header, env in config.headers_env.items()}
                server = await stack.enter_async_context(
                    MCPServerStreamableHttp(
                        name=config.name,
                        params={"url": config.url, "headers": headers},
                        cache_tools_list=True,
                    )
                )
                for remote in await server.list_tools():

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

                    tools.append(
                        make_remote(
                            server,
                            remote.name,
                            remote.description or "MCP tool",
                            remote.inputSchema,
                        )
                    )
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
                model_settings=ModelSettings(parallel_tool_calls=False, store=False),
            )
            result = await Runner.run(
                agent,
                input=items,
                context=context,
                max_turns=context.request.limits.max_tool_calls + 1,
                run_config=RunConfig(tracing_disabled=True),
            )
            return cast(list[JsonValue], JSON.validate_python(result.to_input_list()))
