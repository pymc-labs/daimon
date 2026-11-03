# Context providers

A context provider is an MCP server Daimon asks for short context cards
before each turn. The cards go in front of the person's message, and a
once-per-session part joins the system message. Read this to bind one.

This is separate from per-turn MCP context
([`DAIMON_MCP_CONTEXT__ENABLED`](configuration.md#daimon_mcp_context__enabled)),
which asks the agent's own MCP servers. Each has its own switch.

## Bind a provider

Bindings are set by the deployment operator. Shell access to the host is the
gate.

```bash
daimon context-provider set provider.json --tenant TENANT_UUID --channel CHANNEL_ID --agent MA_AGENT_ID
```

Leave out `--channel` or `--agent` for a broader binding. A turn uses the most
specific match: channel and agent, then channel, then agent, then the whole
tenant. With no binding the turn is unchanged.

```json
{
  "transport": "mcp",
  "endpoint": "https://provider.example/mcp",
  "bearer_env": "DAIMON_CONTEXT_00000000000000000000000000000001_BRIDGE",
  "enabled": true
}
```

The bearer stays in the host environment and is never stored. Its variable
name starts with `DAIMON_CONTEXT_`, then the tenant UUID in upper case without
hyphens, then `_`.

To turn a binding off, set `"enabled": false` and run the same command with
the same selectors. There is no delete. A disabled binding still stops a
broader one from applying.

## What the provider receives

Daimon calls the tool `context_cards_for_turn` over MCP, with no initialize
step. The reply must hold one JSON-RPC result with a version 1
`ContextResponse` in `structuredContent`, as JSON or SSE.

The request names the tenant, channel, thread, agent and requester, plus
`deadline_ms`. Discord also sends the raw message text and its mentions. In a
DM, the channel and thread carry the private DM id. The provider must verify
the caller and requester itself: the request grants no access.

Providers run on Managed Agents sessions, including replacement sessions.

## Limits

| Limit | Value |
| --- | --- |
| Time, first call in a session | 400 ms, lowered by `first_call_timeout_ms` |
| Time, later calls | 200 ms, lowered by `timeout_ms` |
| Response size | 64 KiB |
| Turn cards | 2,500 tokens, lowered by `max_tokens` |
| System part | 1,000 tokens, up to 2,000 with `system_max_tokens` |

Tokens are counted as characters divided by 3.5. There are no retries. A
timeout, refusal, oversized or invalid reply runs the turn without context.
Logs record only the tenant and the error type, never provider text or
credentials.

The endpoint must be HTTPS. Redirects, proxies and cookies are off. Discord,
Slack, Teams and the scheduler open connections to enabled providers at start
and keep them warm with an unauthenticated `GET /health` about every 200
seconds.

Daimon remembers which cards a session already received, in memory only. A
restart can repeat a card, and content already sent cannot be taken back.
