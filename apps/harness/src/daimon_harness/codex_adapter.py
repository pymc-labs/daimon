"""Codex app-server loop; the harness retains tools, auth, state and metering.

Pinned protocol: codex-cli 0.162.1. No native execution approval is ever granted.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import tempfile
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack, suppress
from pathlib import Path
from typing import cast

import httpx
from agents.tool import FunctionTool
from agents.tool_context import ToolContext
from azure.identity.aio import get_bearer_token_provider
from pydantic import JsonValue

from .codex_relay import ResponsesRelay
from .credentials import credential_log_redaction, remember_log_secret
from .errors import HarnessError
from .model_adapter import AzureModelAdapter, context_tools
from .runtime import TurnContext

CODEX_VERSION = "0.162.1"


def private_config(directory: Path, url: str, capability: str, model: str) -> None:
    """The only capability on disk is ephemeral and inaccessible to tool UIDs."""
    # Bundled model catalogs may force code_mode_only / Responses Lite even
    # when those features are disabled. The wrapper exposes ordinary functions.
    catalog = {
        "models": [
            {
                "slug": model,
                "base_instructions": "Use only the provided harness function tools.",
                "display_name": "Harness deployment",
                "description": None,
                "default_reasoning_level": "low",
                "supported_reasoning_levels": [{"effort": "low", "description": "Probe/default"}],
                "shell_type": "disabled",
                "visibility": "list",
                "supported_in_api": True,
                "priority": 0,
                "availability_nux": None,
                "upgrade": None,
                "support_verbosity": False,
                "default_verbosity": None,
                "apply_patch_tool_type": None,
                "truncation_policy": {"mode": "tokens", "limit": 10000},
                "context_window": 128000,
                "experimental_supported_tools": [],
                "tool_mode": "direct",
                "use_responses_lite": False,
                "include_skills_usage_instructions": False,
                "include_plugin_usage_instructions": False,
                "include_apps_usage_instructions": False,
            }
        ]
    }
    (directory / "models.json").write_text(json.dumps(catalog))
    config = "\n".join(
        [
            "model = " + json.dumps(model),
            'model_provider = "harness"',
            "model_catalog_json = " + json.dumps(str(directory / "models.json")),
            'approval_policy = "on-request"',
            'sandbox_mode = "read-only"',
            'web_search = "disabled"',
            "check_for_update_on_startup = false",
            "project_doc_max_bytes = 0",
            "[features]",
            "shell_tool = false",
            "multi_agent = false",
            "apps = false",
            "plugins = false",
            "recommended_plugins = false",
            "tool_suggest = false",
            "skip_host_skill_discovery = true",
            "code_mode = false",
            "code_mode_only = false",
            "code_mode_host = false",
            "shell_snapshot = false",
            "view_image = false",
            "[skills]",
            "include_instructions = false",
            "[skills.bundled]",
            "enabled = false",
            "[analytics]",
            "enabled = false",
            "[feedback]",
            "enabled = false",
            "[model_providers.harness]",
            'name = "Harness Azure Responses relay"',
            "base_url = " + json.dumps(url),
            "experimental_bearer_token = " + json.dumps(capability),
            'wire_api = "responses"',
            "requires_openai_auth = false",
            "supports_websockets = false",
            "request_max_retries = 0",
            "stream_max_retries = 0",
            "",
        ]
    )
    path = directory / "config.toml"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as file:
        file.write(config)


class CodexConnection:
    def __init__(
        self,
        process: asyncio.subprocess.Process,
        context: TurnContext,
        tools: dict[str, FunctionTool],
    ) -> None:
        self.process = process
        self.context = context
        self.tools = tools
        self.next_id = 0
        self.thread_id: str | None = None
        self.turn_id: str | None = None
        self.completed = False
        self.failed = False

    async def send(self, payload: dict[str, JsonValue]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write((json.dumps(payload) + "\n").encode())
        await self.process.stdin.drain()

    async def call(self, method: str, params: dict[str, JsonValue]) -> dict[str, JsonValue]:
        self.next_id += 1
        request_id = self.next_id
        await self.send({"id": request_id, "method": method, "params": params})
        while True:
            message = await self.receive()
            if message.get("id") == request_id and "method" not in message:
                if "error" in message:
                    raise HarnessError("codex_protocol", "Codex app-server request failed")
                return cast(dict[str, JsonValue], message["result"])
            await self.event(message)

    async def receive(self) -> dict[str, JsonValue]:
        assert self.process.stdout is not None
        line = await self.process.stdout.readline()
        if not line:
            raise HarnessError("codex_exit", "Codex app-server closed its stream")
        return cast(dict[str, JsonValue], json.loads(line))

    async def event(self, message: dict[str, JsonValue]) -> None:
        method = message.get("method")
        params = cast(dict[str, JsonValue], message.get("params", {}))
        if method == "item/tool/call":
            tool = self.tools.get(str(params.get("tool")))
            if tool is None or params.get("namespace") not in (None, "functions"):
                raise HarnessError("codex_native_tool", "Unregistered Codex dynamic tool")
            arguments = json.dumps(params["arguments"])
            result = await tool.on_invoke_tool(
                ToolContext(
                    context=self.context,
                    tool_name=tool.name,
                    tool_call_id=str(params["callId"]),
                    tool_arguments=arguments,
                ),
                arguments,
            )
            await self.send(
                {
                    "id": message["id"],
                    "result": {
                        "contentItems": [{"type": "inputText", "text": str(result)}],
                        "success": True,
                    },
                }
            )
        elif method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
            "execCommandApproval",
            "applyPatchApproval",
        }:
            decision = (
                "denied" if method in {"execCommandApproval", "applyPatchApproval"} else "decline"
            )
            await self.send({"id": message["id"], "result": {"decision": decision}})
            raise HarnessError("codex_native_tool", "Native Codex exec/patch approvals are refused")
        elif "id" in message and method is not None:
            await self.send(
                {
                    "id": message["id"],
                    "error": {
                        "code": -32601,
                        "message": "Unsupported harness operation",
                    },
                }
            )
            raise HarnessError("codex_protocol", "Unsupported Codex server request")
        elif method == "turn/started":
            self.turn_id = str(cast(dict[str, JsonValue], params["turn"])["id"])
        elif method == "item/agentMessage/delta":
            await self.context.emit(
                "agent_message_delta",
                {
                    "message_id": self.context.request.turn_id + ":" + str(params["itemId"]),
                    "text": params["delta"],
                },
            )
        elif method == "item/completed":
            item = cast(dict[str, JsonValue], params["item"])
            if item.get("type") == "agentMessage":
                await self.context.emit(
                    "agent_message",
                    {
                        "message_id": self.context.request.turn_id + ":" + str(item["id"]),
                        "parts": [{"type": "text", "text": item["text"]}],
                    },
                )
            elif item.get("type") in {"commandExecution", "fileChange", "mcpToolCall"}:
                raise HarnessError("codex_native_tool", "Unexpected native Codex execution")
        elif method == "turn/completed":
            self.completed = True
            turn = cast(dict[str, JsonValue], params["turn"])
            self.failed = turn.get("status") != "completed"
        # Token totals/notifications are not an authoritative request inventory.

    async def stop(self) -> None:
        if self.process.returncode is not None:
            with suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGKILL)
            return
        if self.thread_id and self.turn_id and not self.completed:
            with suppress(BrokenPipeError, ConnectionResetError):
                await self.send(
                    {
                        "id": "interrupt",
                        "method": "turn/interrupt",
                        "params": {
                            "threadId": self.thread_id,
                            "turnId": self.turn_id,
                        },
                    }
                )
                # Give app-server a bounded opportunity to acknowledge interruption.
                with suppress(TimeoutError, HarnessError):
                    async with asyncio.timeout(0.5):
                        while True:
                            message = await self.receive()
                            if message.get("method") == "turn/completed":
                                break
        if self.process.stdin is not None:
            self.process.stdin.close()
        try:
            await asyncio.wait_for(self.process.wait(), 0.5)
        except TimeoutError:
            with suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(self.process.wait(), 2)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    os.killpg(self.process.pid, signal.SIGKILL)
                await asyncio.wait_for(self.process.wait(), 2)
        # A controller can exit before a helper that inherited its process group.
        with suppress(ProcessLookupError):
            os.killpg(self.process.pid, signal.SIGKILL)


class CodexModelAdapter(AzureModelAdapter):
    """Capture Azure authentication before the service scrubs the environment."""

    def __init__(self) -> None:
        super().__init__()
        self.binary = os.environ.get("HARNESS_CODEX_BINARY", "codex")

    async def run(self, context: TurnContext, history: list[JsonValue]) -> list[JsonValue]:
        self.validate_probe_model(context)
        with credential_log_redaction(self.log_credentials | context.credentials) as redaction:
            async with AsyncExitStack() as stack:
                client = await stack.enter_async_context(
                    httpx.AsyncClient(
                        timeout=httpx.Timeout(30, read=context.request.limits.deadline_s),
                        follow_redirects=False,
                    )
                )
                token_provider = (
                    get_bearer_token_provider(
                        self.credential, "https://cognitiveservices.azure.com/.default"
                    )
                    if self.credential is not None
                    else None
                )

                async def headers() -> dict[str, str]:
                    if self.key:
                        return (
                            {"Authorization": "Bearer " + self.key}
                            if self.live_probe
                            else {"api-key": self.key}
                        )
                    assert token_provider is not None
                    token = await token_provider()
                    remember_log_secret(redaction, "azure_bearer", token)
                    return {"Authorization": "Bearer " + token}

                url = (
                    self.endpoint + "/responses"
                    if self.live_probe
                    else self.endpoint.rstrip("/")
                    + "/openai/responses?api-version="
                    + self.api_version
                )
                return await self.run_codex(context, history, client, url, headers)

    async def run_codex(
        self,
        context: TurnContext,
        history: list[JsonValue],
        client: httpx.AsyncClient,
        url: str,
        headers: Callable[[], Awaitable[dict[str, str]]],
    ) -> list[JsonValue]:
        # An explicit transport seam lets tests run the real binary without Azure.
        async with AsyncExitStack() as stack:
            policy_names: dict[str, str] = {}
            tools = {
                "harness_" + tool.name: tool
                for tool in await context_tools(
                    context, stack, include_denied=True, policy_names=policy_names
                )
                if isinstance(tool, FunctionTool)
            }
            dynamic: list[JsonValue] = [
                {
                    "name": name,
                    "description": tool.description,
                    "inputSchema": tool.params_json_schema,
                }
                for name, tool in tools.items()
            ]
            exposed: list[dict[str, JsonValue]] = [
                {
                    "type": "function",
                    "name": name,
                    "description": tool.description,
                    "parameters": tool.params_json_schema,
                    "strict": False,
                }
                for name, tool in tools.items()
                if context.permission(policy_names[tool.name]) != "deny"
            ]
            relay = ResponsesRelay(
                context,
                history,
                exposed,
                client,
                url,
                headers,
            )
            # Hidden denied tools still have local rejection handlers, never advertised.
            relay.names = set(tools)
            base = context.state_dir
            if base is not None:
                base.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tempfile.TemporaryDirectory(prefix="codex-", dir=base) as scratch:
                directory = Path(scratch)
                async with relay.serve() as endpoint:
                    relay.private_paths += (str(directory),)
                    private_config(directory, endpoint, relay.capability, context.request.model)
                    env = {
                        "PATH": os.defpath,
                        "HOME": scratch,
                        "CODEX_HOME": scratch,
                        "LANG": "C.UTF-8",
                    }
                    # Resolve npm launchers through the parent's PATH, without inheriting env.
                    binary = shutil.which(self.binary)
                    if binary is None:
                        raise HarnessError("codex_missing", "Install the pinned Codex binary")
                    env["PATH"] = str(Path(binary).parent) + os.pathsep + os.defpath
                    creation = asyncio.create_task(
                        asyncio.create_subprocess_exec(
                            binary,
                            "app-server",
                            "--listen",
                            "stdio://",
                            env=env,
                            cwd=scratch,
                            stdin=asyncio.subprocess.PIPE,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.DEVNULL,
                            start_new_session=True,
                            limit=8 * 1024 * 1024,
                        )
                    )
                    connection: CodexConnection | None = None
                    try:
                        process = await asyncio.shield(creation)
                        connection = CodexConnection(process, context, tools)
                        initialized = await connection.call(
                            "initialize",
                            {
                                "clientInfo": {"name": "daimon_harness", "version": "0.1.0"},
                                "capabilities": {"experimentalApi": True},
                            },
                        )
                        if not str(initialized.get("userAgent", "")).startswith(
                            "daimon_harness/" + CODEX_VERSION + " "
                        ):
                            raise HarnessError(
                                "codex_version", "Codex binary does not match the pin"
                            )
                        await connection.send({"method": "initialized"})
                        thread = await connection.call(
                            "thread/start",
                            {
                                "model": context.request.model,
                                "modelProvider": "harness",
                                "cwd": scratch,
                                "ephemeral": True,
                                "approvalPolicy": "on-request",
                                "sandbox": "read-only",
                                "environments": [],
                                "dynamicTools": dynamic,
                                "baseInstructions": context.request.instructions
                                + "\n\n"
                                + await context.skills(),
                            },
                        )
                        connection.thread_id = str(
                            cast(dict[str, JsonValue], thread["thread"])["id"]
                        )
                        inputs: list[JsonValue] = []
                        for part in context.request.input:
                            if part.type == "text":
                                inputs.append(
                                    {"type": "text", "text": part.text, "text_elements": []}
                                )
                            else:
                                inputs.append(
                                    {
                                        "type": "image",
                                        "url": f"data:{part.media_type};base64,{part.data_b64}",
                                    }
                                )
                        await connection.call(
                            "turn/start", {"threadId": connection.thread_id, "input": inputs}
                        )
                        while not connection.completed:
                            await connection.event(await connection.receive())
                        if relay.failure is not None:
                            raise relay.failure
                        if connection.failed:
                            raise HarnessError("codex_turn", "Codex turn failed")
                        return relay.result
                    finally:

                        async def cleanup() -> None:
                            nonlocal connection
                            if connection is None:
                                process = await creation
                                connection = CodexConnection(process, context, tools)
                            await connection.stop()

                        finalization = asyncio.create_task(cleanup())
                        while not finalization.done():
                            with suppress(asyncio.CancelledError):
                                await asyncio.shield(finalization)
                        await finalization
