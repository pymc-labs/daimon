"""Offline adapter; production UID isolation, real tools and checkpoint path."""

import asyncio
from contextlib import AsyncExitStack

from agents.mcp import MCPServerStreamableHttp
from daimon_harness.__main__ import main
from daimon_harness.credentials import mcp_headers
from daimon_harness.model_adapter import bound_mcp_http_client
from daimon_harness.service import create_app


class Fake:
    async def run(self, context, history):
        mode = context.request.input[0].text
        await context.emit(
            "model_request_started",
            {"request_id": context.request.turn_id, "model": context.request.model},
        )
        if mode == "oom":
            blocks = []
            while True:
                blocks.append(bytearray(16 * 1024 * 1024))
                await asyncio.sleep(0.01)
        if mode == "write":
            result = await context.bash('printf "POD_RESUME_OK\\n" > proof.txt; cat proof.txt')
        elif mode == "read":
            result = await context.bash("cat proof.txt")
        else:
            result = {"stdout": "done"}
        async with AsyncExitStack() as stack:
            for config in context.request.mcp_servers:
                connection = await stack.enter_async_context(
                    MCPServerStreamableHttp(
                        name=config.name,
                        params={
                            "url": config.url,
                            "headers": mcp_headers(
                                config, context.credentials, context.credential_bindings
                            ),
                            "httpx_client_factory": bound_mcp_http_client,
                        },
                    )
                )
                assert "whoami" in [tool.name for tool in await connection.list_tools()]

                async def execute(connection=connection):
                    response = await connection.call_tool("whoami", {})
                    return response.model_dump(mode="json")

                remote = await context.tool("mcp-smoke", "daimon-mcp/whoami", {}, execute, mcp=True)
                result["mcp"] = remote
        await context.emit(
            "usage",
            {
                "request_id": context.request.turn_id,
                "model": context.request.model,
                "input_tokens": 1,
                "cached_input_tokens": 0,
                "output_tokens": 1,
                "reasoning_tokens": 0,
            },
        )
        await context.emit(
            "agent_message",
            {
                "message_id": context.request.turn_id,
                "parts": [{"type": "text", "text": str(result)}],
            },
        )
        return history


main(factory=lambda: create_app(adapter=Fake()))
