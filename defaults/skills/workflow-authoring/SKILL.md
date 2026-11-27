---
name: workflow-authoring
description: Read, change and test a workflow's draft with the workflow tools. Use when someone asks you to build, change, fix or explain a workflow, or has one open.
---

# Workflow authoring

A workflow is a graph of steps that runs on its own. You change its draft; nothing goes
live until someone publishes it.

## Edit a draft

1. Read it with `get_workflow`. Note `revision`.
2. Change it with `edit_workflow_draft`, passing that revision as `base_revision`.
   Edits apply in order: `add_node`, `update_node`, `remove_node`, `connect`,
   `set_trigger`, `set_inputs`, `set_start`.
3. If the draft moved on, you are told its new revision. Read it again and redo the edit.
4. Fix the problems that come back. `validate_workflow` lists errors, warnings and grants.
5. Try it with `run_workflow` and `test: true`. A test run uses the draft and cannot write
   to real systems.

Publish only when the person asks. An agent key cannot publish.

In Discord, Slack and Teams, link the workflow page for the full canvas and do not paste
the spec.

## Steps

| Kind | Use it to |
| --- | --- |
| `agent` | Have an agent do work and return `output` fields |
| `branch` | Pick the next step by a condition |
| `transform` | Compute values from earlier results |
| `code` | Run a Python script |
| `action` | Call one tool with fixed arguments |
| `approval` | Ask a person to approve, choose, or answer |
| `wait` | Wait for a time, a date or an event |
| `for_each` | Run one step for each item of a list |
| `workflow` | Run another workflow and take its output |
| `post` | Post a message and carry on |
| `finish` | End the run, with an `output` and an optional `deliver` or `state` |

Every path ends at a `finish`. A loop needs `max_visits`.

An agent step may work `at` `home`, `origin` or a channel or thread id. With
`memory: personal` it reads the run-as person's memory, works nowhere, and only a post
`to: run_as` may use its result. `steps.<id>.posted` lists where it posted itself.

An agent step lists `skills` by name from the workspace library; an unknown name is an
error. An `approval` with `otherwise` sends a reply that names no option to that step.

## Expressions

Values use `${{ }}` expressions: `${{ inputs.topic }}`, `${{ steps.draft.output.text }}`.
A step reads only inputs and steps before it. A step that may have been skipped needs a
`has()` guard, as below. A branch condition is one whole expression. Agent
`instructions` take no expressions: pass data in `inputs`.

```yaml
output: ${{ has(steps.t) ? steps.t.output : {} }}
```

## Asks, writes and grants

An `approval` step asks people in chat or in the inbox. With `binds`, the write after
it sends exactly what was approved.

Outside input is untrusted: a webhook body, a stranger's answer, a page from the web. A
step that sees it and writes is refused unless an approval binds the write. With
`writes: propose`, an agent proposes its writes and a person approves each one.

Some settings need an admin's approval before the version goes live: writes without
asking, built-in tools, code with network access, an unsigned webhook, a budget over the
default, or a step that trusts outside input.

## Posts, triggers and time

A `post` step and `finish.deliver` take `to`, `thread`, `also`, `mention` and `message`.
`to` is `home`, `owner`, `run_as`, `origin`, an id or a template. A templated `to` must
be one of the spec's `destinations`, and `destinations` hold fixed ids. `thread` finds or
creates the thread with that exact title.

Triggers also include `mention` (`channels` or `match`) and `poll` (`agent`, a read
`tool`, `every`, `key`, and `items`). A wait can `poll` too. A schedule takes a cron with
`every_weeks` and an `anchor`, or one `at` time. A duration like `2bd` counts business
days in waits and approvals only.

## State, scopes and boards

`finish.state` saves keys for later runs. Read them as `workflow.state` and the last
success as `workflow.last_run`; `run.date` is the day the run started. Saved state is
untrusted input.

A step that must write with nobody watching lists `writes_to`: a `tool`, a `where`
rule per argument (`in`, `prefix`, `domains` or `created_by`) and `free` arguments.
Prefer it to `writes: allow`. `writes: policy` writes only as the server allows every
unattended run.

`stage` on a step and a `card` on the spec put runs on a board. `card.date` names the
field a calendar uses.
