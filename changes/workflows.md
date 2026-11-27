- **Workflows.** A workflow is a graph of steps that runs on its own: agents,
  conditions, code, tool calls, questions to people and waits. Build one on a
  canvas in the web app, from a starter or with Ask Daimon, then test and
  publish it. Runs start by hand, on a schedule, from a signed webhook or after
  another workflow. Approval steps ask people in chat by direct message or in a
  thread, or in the web app's inbox. `/workflows` on Discord, Slack and Teams
  lists them, and chat cards publish, run and follow runs. Admins can move
  routines to workflows and back, and `daimon workflows` lists, exports, imports
  and controls them. Steps can post to chat in titled threads, and runs can
  start from a chat mention or from new rows a tool returns. A workflow can save
  state for later runs, run every few weeks or once at a set time, wait in
  business days, and show its runs on a board or calendar. A step can make a
  write with nobody watching when its arguments fit a stated scope. Workflows,
  their pages, mentions (`workflow_mentions`) and polls (`workflow_polls`) are
  on; webhooks, code steps and scoped writes (`workflow_scoped_writes`) stay off
  until an admin turns them on. The routine result header and routine sandbox
  tools have a Workflow level, for a workflow's routine steps. Routines still
  work but are marked Legacy, and every routines list offers an admin "Move to a
  workflow". A moved routine becomes the workflow, with its pending post, owner
  notice, settings and intake binding; `daimon routines translate --dry-run`
  checks every routine. A workflow's routine step signs the same identity claims
  as a routine, plus its run. See [Workflows](docs/workflows.md) and
  [Advanced workflows](docs/workflows-advanced.md).
