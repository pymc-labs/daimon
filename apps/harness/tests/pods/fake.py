"""Offline adapter; production UID isolation, real tools and checkpoint path."""

import asyncio

import uvicorn
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


uvicorn.run(create_app(adapter=Fake()), host="0.0.0.0", port=8088)
