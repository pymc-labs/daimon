# Frozen QA-53 tool/resource parity map

Audited integration: `9d4541c07701ccae38a70e0a254c980ef3f99e41`.
Frozen source: `~/cs/daimon-qa-driver-20261010/catalog/TARGET-53.txt` and its 53 scenario YAMLs.
`map.json` records the target checksum and each source YAML checksum; CI needs no other checkout.

**OK means the required tool/resource surface exists in source, not a scenario PASS.**
No scenario was run live, no inference/model-quality claim is made, and no deployment is certified.
For the ten host-only cases, OK means no provider tool manifest is needed at all. Human clicks,
HTTP probes, billing/permission assertions and rendering still need N9/N7's scenario runner/oracle.
For model turns, all eleven default skills remain attached; Skills lists below show the minimal
skills useful for the requested operation, inferred from the frozen steps/human checklist.
An empty list does not remove any baseline skill or promise the model will never read one.
MCP names in a row denote capabilities reachable through discovery as well as direct calls.

## Exposure counts (159 cells; availability, not passes)

| Backend | OK | GAP | Pending |
| --- | ---: | ---: | ---: |
| Anthropic | 53 | 0 | 0 |
| OpenAI | 10 | 2 | 41 PENDING-G1 |
| Gemini | 10 | 2 | 41 PENDING-G2 |

The two GAP resource cases are a channel fork preserving skills and the built-in Add skill flow:
the host resource helpers remain Anthropic-bound. The 41 pending turns have no registered
alternate host path at this base. Their driver gaps are listed independently below so G1/G2
registration cannot silently turn them into OK.

## Manifest evidence and limits

The actual default Anthropic spec crosses the real SDK through ScriptedTransport: the six
authored builtin names, reserved MCP toolset, server and eleven concrete skill pins are preserved.
Caller-visible schemas come from the real MCP server with injected claims, via `Client.list_tools`:
Discord/Slack/Teams user and admin chat callers see six top-level tools (`search_tools`, `call_tool`,
`add_skill`, `publish_report`, `create_attachment_upload_url`, `create_notebook_upload_url`).
Other requested MCP capabilities are discovered with real `search_tools` calls; the server pins
approval tools outside `call_tool`. Dedicated external agent tokens see a separate 19-tool
surface; they must not stand in for the default chat token (`chat_agent_id`, not `agent_id`).

Offline tests compare every rendered name and full JSON input schema through the actual
Anthropic/OpenAI/Gemini custom-tool encoders, including all nested keys. Nine caller-surface
digests independently pin drift. This proves compiler preservation; it does **not** claim that
OpenAI/Gemini have received an authenticated live default MCP manifest. At this base that is
an explicit typed gap; host factory probes refuse `host_turn_backend` before I/O. A G1/G2
registration makes the stale pending allowlist fail until actual host evidence replaces it.

Native builtin schemas are provider-owned and unavailable here. They are never fabricated.
Explicit typed allowlisted name/schema deltas: OpenAI maps read/edit/grep/glob/write to hosted
bash (implicit in the workspace environment), Gemini maps all six to code_execution. These are
capability mappings, not equal native parameter schemas or a quality/cost/latency certificate.

Default full-bundle Gemini deployment fails on the real pymc-artifact-style font/image payload;
no SKILL.md-only or text-only surrogate is counted as full parity. Database connections and
provider SDK HTTP are guarded against during MCP introspection; only the audit sink is disabled.

Run: `uv run pytest -q tests/parity/test_qa_tool_parity.py`.

## Evidence anchors

- Defaults: `defaults/agents/daimon.yaml`, `defaults/skills/*`; actual default MCP injection:
  `core/defaults/reconcile_agents.py`, `core/defaults/mcp_merge.py`.
- Current host gate: `core/channel_backend.py:RUNNABLE_PROFILES`,
  `core/mux_backend.py:turn_backend`, `core/turn/io.py:turn_io` (Anthropic registration only).
- MCP registration, permissions and discovery: `adapters/mcp/server.py`,
  `middleware/mcp_identity.py`, `search_transform.py`; chat credentials: `core/mcp_vault.py`.
- Native tools/auth: `mux/drivers/anthropic/resources/agents.py:agent_payload`,
  `mux/drivers/openai/agents.py:agent_body`, `mux/drivers/gemini/core.py:compile_agent`.
- Full Gemini bundles: `mux/drivers/gemini/bundles.py:validate_bundle`;
  40 MiB checkpoint: `mux/drivers/gemini/resources.py:parse_snapshot` (16 MiB per member).
- Private repository: `mux/drivers/gemini/core.py:compile_environment` refuses credential_ref.
- Host resource setup: `core/session_preparation.py`, `core/session_ports_compat.py`,
  `core/github_app_session.py`, `core/memory_resource.py`, MCP `tools/routines.py`/`skills.py`.

## Typed reason codes

- `HOST_TURN_UNREGISTERED`: Backend is absent from RUNNABLE_PROFILES, turn_backend and turn_codec on audited integration. No model-facing host manifest exists yet.
- `HOST_RESOURCE_ANTHROPIC`: Host setup, fork, skill, credential and routine handlers compose the Anthropic resource backend; registering a turn codec does not migrate these resource paths.
- `AUTHENTICATED_MCP_UNSUPPORTED`: Actual OpenAI/Gemini agent compilers refuse a streamable-http MCPConnection with credential_ref. Default daimon-mcp requires per-turn authenticated caller context.
- `GEMINI_DEFAULT_BUNDLE_UNSUPPORTED`: Real Gemini validate_bundle requires UTF-8 files <=2 MiB. Default pymc-artifact-style includes binary fonts/images; full eleven-skill default cannot be deployed.
- `BUILTIN_NAME_SCHEMA_DELTA`: Anthropic six native names map to OpenAI hosted bash or Gemini code_execution. Native builtin input schemas are provider-owned and not exposed by ToolSpec: semantic equivalence is not schema equality.
- `PRIVATE_REPOSITORY_UNSUPPORTED`: Gemini compile_sources refuses repository credentials; provider-native authenticated repository resources and host GitHub handoff need their own binding.
- `MEMORY_STORE_UNSUPPORTED`: OpenAI/Gemini profiles do not provide Anthropic native memory_stores; a host memory adapter must preserve the routine memory mount.
- `GEMINI_CHECKPOINT_LIMIT`: Gemini workspace snapshot parser limits each member to 16 MiB; scenario creates a 40 MiB member. This is a resource gap beyond host registration.

## All 53 scenarios

| Scenario | Skills used | Tools needed | Resources / host surface | Anthropic | OpenAI | Gemini | Driver gaps beyond G1/G2 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| QA-D11-FOOTER-COST-MATCHES-LEDGER | — | — | usage-ledger | OK | PENDING-G1 | PENDING-G2 | gemini: GEMINI_DEFAULT_BUNDLE_UNSUPPORTED |
| QA-D12-PDF-CLAIM-HAS-PDF | file-handling, pymc-artifact-style | bash, write, read | output-files, artifact-download | OK | PENDING-G1 | PENDING-G2 | openai: BUILTIN_NAME_SCHEMA_DELTA; gemini: GEMINI_DEFAULT_BUNDLE_UNSUPPORTED, BUILTIN_NAME_SCHEMA_DELTA |
| QA-D13-FILES-NOT-REPOSTED | file-handling | bash, write | persistent-workspace, output-files, artifact-download | OK | PENDING-G1 | PENDING-G2 | openai: BUILTIN_NAME_SCHEMA_DELTA; gemini: GEMINI_DEFAULT_BUNDLE_UNSUPPORTED, BUILTIN_NAME_SCHEMA_DELTA |
| QA-D14-FORK-KEEPS-SKILLS | workspace-setup | — | agent-fork, custom-skill-pins | OK | GAP (HOST_RESOURCE_ANTHROPIC) | GAP (HOST_RESOURCE_ANTHROPIC) | — |
| QA-D1-CANARY-TWO-TURN | file-handling | read, bash | input-files, persistent-workspace | OK | PENDING-G1 | PENDING-G2 | openai: BUILTIN_NAME_SCHEMA_DELTA; gemini: GEMINI_DEFAULT_BUNDLE_UNSUPPORTED, BUILTIN_NAME_SCHEMA_DELTA |
| QA-D1-REPLACE-NOTICE-HIDDEN-FOR-SKILLS | — | bash | persistent-workspace, workspace-checkpoint, custom-skill-pins | OK | PENDING-G1 | PENDING-G2 | openai: BUILTIN_NAME_SCHEMA_DELTA, HOST_RESOURCE_ANTHROPIC; gemini: GEMINI_DEFAULT_BUNDLE_UNSUPPORTED, BUILTIN_NAME_SCHEMA_DELTA, HOST_RESOURCE_ANTHROPIC |
| QA-D1-SLACK-FOLLOWUP | file-handling | read, bash | input-files, persistent-workspace, slack-files | OK | PENDING-G1 | PENDING-G2 | openai: BUILTIN_NAME_SCHEMA_DELTA; gemini: GEMINI_DEFAULT_BUNDLE_UNSUPPORTED, BUILTIN_NAME_SCHEMA_DELTA |
| QA-D22-WHO-ANSWERS-HERE | — | explain_agent_resolution, read_channel, search_tools, call_tool | routing-context, daimon-mcp | OK | PENDING-G1 | PENDING-G2 | openai: AUTHENTICATED_MCP_UNSUPPORTED; gemini: GEMINI_DEFAULT_BUNDLE_UNSUPPORTED, AUTHENTICATED_MCP_UNSUPPORTED |
| QA-D3-TEAMS-ANSWER-NOT-REPLACED | file-handling | bash, write | output-files, artifact-download, teams-files, approval-cards, turn-cancel | OK | PENDING-G1 | PENDING-G2 | openai: BUILTIN_NAME_SCHEMA_DELTA; gemini: GEMINI_DEFAULT_BUNDLE_UNSUPPORTED, BUILTIN_NAME_SCHEMA_DELTA |
| QA-D5-UNBOUND-AGENT-EDIT-REFUSED | workspace-setup | update_agent, attach_mcp_server, search_tools, call_tool | agent-edit-authorization, daimon-mcp | OK | PENDING-G1 | PENDING-G2 | openai: AUTHENTICATED_MCP_UNSUPPORTED; gemini: GEMINI_DEFAULT_BUNDLE_UNSUPPORTED, AUTHENTICATED_MCP_UNSUPPORTED |
| QA-D6-LONG-TURN-CARD-LIFECYCLE | — | bash | progress-cards | OK | PENDING-G1 | PENDING-G2 | openai: BUILTIN_NAME_SCHEMA_DELTA; gemini: GEMINI_DEFAULT_BUNDLE_UNSUPPORTED, BUILTIN_NAME_SCHEMA_DELTA |
| QA-D7-BIG-WORKSPACE-CHECKPOINT | — | bash | persistent-workspace, workspace-checkpoint-40m, custom-skill-pins | OK | PENDING-G1 | PENDING-G2 | openai: BUILTIN_NAME_SCHEMA_DELTA, HOST_RESOURCE_ANTHROPIC; gemini: GEMINI_DEFAULT_BUNDLE_UNSUPPORTED, BUILTIN_NAME_SCHEMA_DELTA, HOST_RESOURCE_ANTHROPIC, GEMINI_CHECKPOINT_LIMIT |
| QA-D8-GITHUB-CONNECT-OFFERED | workspace-setup | github_connect, search_tools, call_tool | daimon-mcp, github-app, repository-binding | OK | PENDING-G1 | PENDING-G2 | openai: AUTHENTICATED_MCP_UNSUPPORTED, HOST_RESOURCE_ANTHROPIC; gemini: GEMINI_DEFAULT_BUNDLE_UNSUPPORTED, AUTHENTICATED_MCP_UNSUPPORTED, HOST_RESOURCE_ANTHROPIC |
| QA-D8-GITHUB-SAVED-KEY-SWITCH | workspace-setup | request_repo_binding, github_connect, bash, search_tools, call_tool | daimon-mcp, repository-binding, private-repository-credentials, github-app | OK | PENDING-G1 | PENDING-G2 | openai: BUILTIN_NAME_SCHEMA_DELTA, AUTHENTICATED_MCP_UNSUPPORTED, HOST_RESOURCE_ANTHROPIC; gemini: GEMINI_DEFAULT_BUNDLE_UNSUPPORTED, BUILTIN_NAME_SCHEMA_DELTA, AUTHENTICATED_MCP_UNSUPPORTED, HOST_RESOURCE_ANTHROPIC, PRIVATE_REPOSITORY_UNSUPPORTED |
| QA-I3-TOOL-ONLY-NO-EMPTY-RESPONSE | — | bash | progress-cards | OK | PENDING-G1 | PENDING-G2 | openai: BUILTIN_NAME_SCHEMA_DELTA; gemini: GEMINI_DEFAULT_BUNDLE_UNSUPPORTED, BUILTIN_NAME_SCHEMA_DELTA |
| QA-I6-BARE-MENTION-THREAD-TITLE | — | — | routing-context, thread-title | OK | PENDING-G1 | PENDING-G2 | gemini: GEMINI_DEFAULT_BUNDLE_UNSUPPORTED |
| QA-I7-MCP-OAUTH-DISCOVERY | — | — | mcp-oauth-metadata | OK | OK | OK | — |
| QA-ISO-TEAM-CHANNEL-ISOLATION | — | read_channel, search_messages, bash, search_tools, call_tool | daimon-mcp, tenant-channel-authorization, input-files, persistent-workspace | OK | PENDING-G1 | PENDING-G2 | openai: BUILTIN_NAME_SCHEMA_DELTA, AUTHENTICATED_MCP_UNSUPPORTED; gemini: GEMINI_DEFAULT_BUNDLE_UNSUPPORTED, BUILTIN_NAME_SCHEMA_DELTA, AUTHENTICATED_MCP_UNSUPPORTED |
| QA-NEW10-SPLIT-ANSWER-AND-CHUNK-REPLY | — | — | answer-chunk-rendering, routing-context | OK | PENDING-G1 | PENDING-G2 | gemini: GEMINI_DEFAULT_BUNDLE_UNSUPPORTED |
| QA-NEW12-AGENT-SWITCH-HANDOFF-COPY | workspace-setup | — | agent-fork, session-replacement, routing-context | OK | PENDING-G1 | PENDING-G2 | openai: HOST_RESOURCE_ANTHROPIC; gemini: GEMINI_DEFAULT_BUNDLE_UNSUPPORTED, HOST_RESOURCE_ANTHROPIC |
| QA-NEW13-WRITERS-NONE-NOT-SILENT | — | — | admission-denial | OK | OK | OK | — |
| QA-NEW14-MISSING-THREAD-PERMISSION-COPY | — | — | thread-permission-denial | OK | OK | OK | — |
| QA-NEW15-CHANNEL-BUDGET-EXHAUSTED | — | — | usage-ledger, admission-denial | OK | PENDING-G1 | PENDING-G2 | gemini: GEMINI_DEFAULT_BUNDLE_UNSUPPORTED |
| QA-NEW16-TENANT-CREDIT-DEPLETED-COPY | — | — | admission-denial, usage-ledger | OK | OK | OK | — |
| QA-NEW17-MCP-USABLE-NEXT-MESSAGE | workspace-setup | attach_mcp_server, search_tools, call_tool | daimon-mcp, external-mcp-deepwiki, session-refresh | OK | PENDING-G1 | PENDING-G2 | openai: AUTHENTICATED_MCP_UNSUPPORTED, HOST_RESOURCE_ANTHROPIC; gemini: GEMINI_DEFAULT_BUNDLE_UNSUPPORTED, AUTHENTICATED_MCP_UNSUPPORTED, HOST_RESOURCE_ANTHROPIC |
| QA-NEW18-SKILL-ADD-YES-PLEASE | workspace-setup | add_skill | input-files, custom-skill-pins, chat-skill-add, session-refresh, daimon-mcp | OK | PENDING-G1 | PENDING-G2 | openai: HOST_RESOURCE_ANTHROPIC, AUTHENTICATED_MCP_UNSUPPORTED; gemini: GEMINI_DEFAULT_BUNDLE_UNSUPPORTED, HOST_RESOURCE_ANTHROPIC, AUTHENTICATED_MCP_UNSUPPORTED |
| QA-NEW19-SKILL-DELETE-WHILE-ATTACHED | — | — | custom-skill-pins, skill-delete-guard | OK | PENDING-G1 | PENDING-G2 | openai: HOST_RESOURCE_ANTHROPIC; gemini: GEMINI_DEFAULT_BUNDLE_UNSUPPORTED, HOST_RESOURCE_ANTHROPIC |
| QA-NEW1-PDF-BARE-MENTION-NO-DROP | file-handling | read | input-files, thread-title | OK | PENDING-G1 | PENDING-G2 | openai: BUILTIN_NAME_SCHEMA_DELTA; gemini: GEMINI_DEFAULT_BUNDLE_UNSUPPORTED, BUILTIN_NAME_SCHEMA_DELTA |
| QA-NEW21-FORM-EXPIRY-UPDATES-CARD | workspace-setup | request_agent_key, search_tools, call_tool | daimon-mcp, credential-forms, credential-vault | OK | PENDING-G1 | PENDING-G2 | openai: AUTHENTICATED_MCP_UNSUPPORTED, HOST_RESOURCE_ANTHROPIC; gemini: GEMINI_DEFAULT_BUNDLE_UNSUPPORTED, AUTHENTICATED_MCP_UNSUPPORTED, HOST_RESOURCE_ANTHROPIC |
| QA-NEW22-ROUTINE-OUTPUT-NOT-TRUNCATED | workspace-setup | create_routine, bash, write, search_tools, call_tool | daimon-mcp, routine-runner, memory-store, answer-chunk-rendering | OK | PENDING-G1 | PENDING-G2 | openai: BUILTIN_NAME_SCHEMA_DELTA, AUTHENTICATED_MCP_UNSUPPORTED, HOST_RESOURCE_ANTHROPIC, MEMORY_STORE_UNSUPPORTED; gemini: GEMINI_DEFAULT_BUNDLE_UNSUPPORTED, BUILTIN_NAME_SCHEMA_DELTA, AUTHENTICATED_MCP_UNSUPPORTED, HOST_RESOURCE_ANTHROPIC, MEMORY_STORE_UNSUPPORTED |
| QA-NEW23-ROUTINE-WITHOUT-DESTINATION | workspace-setup | create_routine, search_tools, call_tool | daimon-mcp, routine-runner, routing-context | OK | PENDING-G1 | PENDING-G2 | openai: AUTHENTICATED_MCP_UNSUPPORTED, HOST_RESOURCE_ANTHROPIC; gemini: GEMINI_DEFAULT_BUNDLE_UNSUPPORTED, AUTHENTICATED_MCP_UNSUPPORTED, HOST_RESOURCE_ANTHROPIC |
| QA-NEW24-EXPIRED-LINK-HTML-PAGE | — | — | notebook-report-http | OK | OK | OK | — |
| QA-NEW25-DM-NOT-SILENT | — | — | dm-pointer, slash-command-help | OK | OK | OK | — |
| QA-NEW26-WRITERS-NONE-SELECT-CONFIRM | — | — | permissions-panel, admission-denial | OK | OK | OK | — |
| QA-NEW27-MISTYPED-TOKEN-HEADLESS | — | — | credential-forms, mcp-token-validation | OK | OK | OK | — |
| QA-NEW27-MISTYPED-TOKEN-KEEPS-FORM | workspace-setup | request_mcp_token, search_tools, call_tool | daimon-mcp, credential-forms, mcp-token-validation, credential-vault | OK | PENDING-G1 | PENDING-G2 | openai: AUTHENTICATED_MCP_UNSUPPORTED, HOST_RESOURCE_ANTHROPIC; gemini: GEMINI_DEFAULT_BUNDLE_UNSUPPORTED, AUTHENTICATED_MCP_UNSUPPORTED, HOST_RESOURCE_ANTHROPIC |
| QA-NEW28-OAUTH-ABANDONED-AND-RESUME | workspace-setup | request_mcp_oauth, search_tools, call_tool | daimon-mcp, external-mcp-notion, credential-forms, credential-vault, oauth-resume | OK | PENDING-G1 | PENDING-G2 | openai: AUTHENTICATED_MCP_UNSUPPORTED, HOST_RESOURCE_ANTHROPIC; gemini: GEMINI_DEFAULT_BUNDLE_UNSUPPORTED, AUTHENTICATED_MCP_UNSUPPORTED, HOST_RESOURCE_ANTHROPIC |
| QA-NEW29-DM-CONVERSATION-FORM | workspace-setup | request_agent_key, search_tools, call_tool | daimon-mcp, credential-forms, dm-conversation | OK | PENDING-G1 | PENDING-G2 | openai: AUTHENTICATED_MCP_UNSUPPORTED; gemini: GEMINI_DEFAULT_BUNDLE_UNSUPPORTED, AUTHENTICATED_MCP_UNSUPPORTED |
| QA-NEW2-CSV-ATTACHMENT-READ | file-handling | read, bash | input-files | OK | PENDING-G1 | PENDING-G2 | openai: BUILTIN_NAME_SCHEMA_DELTA; gemini: GEMINI_DEFAULT_BUNDLE_UNSUPPORTED, BUILTIN_NAME_SCHEMA_DELTA |
| QA-NEW30-APPROVE-AFTER-RESTART | workspace-setup | publish_report | daimon-mcp, report-host, approval-cards, restart-expiry | OK | PENDING-G1 | PENDING-G2 | openai: AUTHENTICATED_MCP_UNSUPPORTED; gemini: GEMINI_DEFAULT_BUNDLE_UNSUPPORTED, AUTHENTICATED_MCP_UNSUPPORTED |
| QA-NEW31-PANELS-AFTER-RESTART | — | — | setup-billing-routine-panels, restart-expiry, coding-token-revoke, checkout | OK | OK | OK | — |
| QA-NEW32-ADD-SKILL-ON-BUILTIN | workspace-setup | — | agent-fork, custom-skill-pins, built-in-edit-guard | OK | GAP (HOST_RESOURCE_ANTHROPIC) | GAP (HOST_RESOURCE_ANTHROPIC) | — |
| QA-NEW33-HERE-AND-GITHUB-HOME-NO-HANG | — | — | slash-here, github-home, privacy-export | OK | OK | OK | — |
| QA-NEW34-SLACK-CREDENTIAL-AND-APPROVAL-PARITY | workspace-setup, file-handling | request_mcp_token, publish_report, bash, write, search_tools, call_tool | daimon-mcp, credential-forms, mcp-token-validation, approval-cards, turn-cancel, slack-files, usage-ledger | OK | PENDING-G1 | PENDING-G2 | openai: BUILTIN_NAME_SCHEMA_DELTA, AUTHENTICATED_MCP_UNSUPPORTED; gemini: GEMINI_DEFAULT_BUNDLE_UNSUPPORTED, BUILTIN_NAME_SCHEMA_DELTA, AUTHENTICATED_MCP_UNSUPPORTED |
| QA-NEW35-ROUTINE-FAILURE-IS-REPORTED | workspace-setup, file-handling | create_routine, bash, read, search_tools, call_tool | daimon-mcp, routine-runner, input-files, routine-failure-notice | OK | PENDING-G1 | PENDING-G2 | openai: BUILTIN_NAME_SCHEMA_DELTA, AUTHENTICATED_MCP_UNSUPPORTED, HOST_RESOURCE_ANTHROPIC; gemini: GEMINI_DEFAULT_BUNDLE_UNSUPPORTED, BUILTIN_NAME_SCHEMA_DELTA, AUTHENTICATED_MCP_UNSUPPORTED, HOST_RESOURCE_ANTHROPIC |
| QA-NEW36-OVERSIZE-OUTPUT-KEPT | file-handling | bash | persistent-workspace, output-files-12m, artifact-download | OK | PENDING-G1 | PENDING-G2 | openai: BUILTIN_NAME_SCHEMA_DELTA; gemini: GEMINI_DEFAULT_BUNDLE_UNSUPPORTED, BUILTIN_NAME_SCHEMA_DELTA |
| QA-NEW37-MARKDOWN-TABLE-READABLE | — | — | markdown-table-rendering | OK | PENDING-G1 | PENDING-G2 | gemini: GEMINI_DEFAULT_BUNDLE_UNSUPPORTED |
| QA-NEW3-BURST-CHANNEL-MENTIONS | — | — | turn-queue | OK | PENDING-G1 | PENDING-G2 | gemini: GEMINI_DEFAULT_BUNDLE_UNSUPPORTED |
| QA-NEW4-DOUBLE-MENTION-DEDUPE | — | — | turn-dedupe, usage-ledger | OK | PENDING-G1 | PENDING-G2 | gemini: GEMINI_DEFAULT_BUNDLE_UNSUPPORTED |
| QA-NEW5-THREAD-QUEUE-WHILE-BUSY | — | bash | turn-queue, persistent-workspace | OK | PENDING-G1 | PENDING-G2 | openai: BUILTIN_NAME_SCHEMA_DELTA; gemini: GEMINI_DEFAULT_BUNDLE_UNSUPPORTED, BUILTIN_NAME_SCHEMA_DELTA |
| QA-NEW6-TENANT-CAP-QUEUE | — | bash | turn-queue, tenant-concurrency-cap | OK | PENDING-G1 | PENDING-G2 | openai: BUILTIN_NAME_SCHEMA_DELTA; gemini: GEMINI_DEFAULT_BUNDLE_UNSUPPORTED, BUILTIN_NAME_SCHEMA_DELTA |
| QA-NEW8-LINK-PAGE-THREAD-TITLE | — | — | thread-title, url-title-filter | OK | PENDING-G1 | PENDING-G2 | gemini: GEMINI_DEFAULT_BUNDLE_UNSUPPORTED |
| QA-NEW9-NO-INTERNAL-PLUMBING | — | — | default-system-instructions, routing-context | OK | PENDING-G1 | PENDING-G2 | gemini: GEMINI_DEFAULT_BUNDLE_UNSUPPORTED |
