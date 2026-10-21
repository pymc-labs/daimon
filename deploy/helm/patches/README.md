Apply `per-conversation.patch` to PR #1273 at
`ca82432b60fcb5e2587d770ac2f0da7e59df51d0`:

```sh
git apply deploy/helm/patches/per-conversation.patch
```

The PR deliberately carries only this patch, not the pending chart. Set
`harness.mode=perConversation`, explicitly accept F76 with
`harness.perConversation.allowInsecureHTTP=true`, and point
`harness.perConversation.mcpCASecret` at the chart-generated MCP CA Secret.
The bundled MCP path requires PR #1276's C3.9a client support and a matching
HTTPS Service URL/certificate. See the harness README for RBAC and durability.

The offline kind smoke runner extracts the pinned chart and applies the patch
in an isolated scratch directory. It does not check out or merge the chart branch.
