# Workflows

How workflows run, how people take part in them from chat, and what an admin
turns on and limits.

A workflow is a graph of steps that runs on its own: agents, conditions, code,
tool calls, questions to people and waits. You edit a draft and publish it as a
numbered version. Every run keeps a tamper-evident record of its steps.

## Turn workflows on

Workflows are On by default, with their pages ([`workflows`](features.md#workflows)
and [`web.workflows`](features.md#web-app-workflows)), mention triggers and poll
triggers. An admin turns on webhook triggers, code steps and scoped writes
separately; they are Off by default.

```bash
DAIMON_FEATURES__WORKFLOW_WEBHOOKS=on
```

While `workflows` is Off, every path refuses: the workflow pages, the workflow
tools, `/workflows` in chat, webhooks and the `daimon workflows` commands.
Runs already started stop at their next step without spending.

| Feature | Key | Default |
| --- | --- | --- |
| Workflows | `workflows` | On |
| Web pages | `web.workflows` | On |
| Webhook triggers | `workflow_webhooks` | Off |
| Code steps | `workflow_code` | Off |
| Mention triggers | `workflow_mentions` | On |
| Poll triggers and waits | `workflow_polls` | On |
| Scoped writes | `workflow_scoped_writes` | Off |
| Run budget | `workflow_run_budget` | $5 |
| Run history | `workflow_retention_days` | 90 days |

Deployment caps on steps, runs, items and webhooks are under
[Workflows settings](configuration.md#workflows).

## Steps

| Kind | Does |
| --- | --- |
| Agent | An agent does the work and returns named fields |
| Branch | Picks the next step by a condition |
| Transform | Computes values from earlier results |
| Code | Runs a Python script |
| Action | Calls one tool with fixed arguments |
| Approval | Asks people to approve, choose, or answer |
| Wait | Waits for a time, a date or an event |
| For each | Runs one step for each item of a list |
| Workflow | Runs another workflow and takes its output |
| Post | Posts a message and carries on |
| Finish | Ends the run and can post the result |

Values come from `${{ }}` expressions, such as `${{ steps.draft.output.text }}`.
A step reads only the inputs and the steps before it. A loop needs a most-visits
limit. Validation names each problem, and a draft with errors cannot be published.

An Agent step can load skills from the workspace library by name. Each publish
pins the version it saw, so a later library edit reaches runs after a republish.

An Agent step can work in a place: home, where the run started, or a channel or
thread. The place's access rules apply, and the agent can post there itself. A
later step reads where it posted in `steps.<id>.posted`. With personal memory, the
step reads the memory of the person the run is for. Its result is private and it
works in no place, so only a Post to that person can use the result.

`writes: policy` lets a step write exactly as the server's rules for unattended
runs allow. A step with no output fields completes with its plain reply as its
summary. A step that can finish early ends the run quietly when its reply starts
or ends with `NO_POST`.

An Agent step runs on the model provider of the place it works in. Its `model`
must be one that provider offers. OpenAI always gives an agent a shell, so a step
there needs built-in tools on.

## Triggers

A workflow starts by hand, on a schedule, from a webhook, from a chat mention,
when a tool's data changes, or when another workflow finishes. Another workflow
can also call it as a step. [Advanced workflows](workflows-advanced.md) covers
mentions, polls and schedules.

A schedule fires each slot once. A missed slot is skipped, unless the trigger
catches up with one run.

A webhook trigger checks the sender's signature: Standard Webhooks, GitHub,
Stripe, Slack or a bearer token. Each delivery starts at most one run. The trigger
has a test URL that captures one delivery as a test run.

## Asks in chat

An Approval step asks people by direct message or in a thread. On Discord, the person answers by reply; on Slack, in the thread under
the message; on Teams, with the card's button. In a thread, an answer from
someone who was not asked is refused.

<!-- knowledge: web -->
The web app's inbox shows the same asks.
<!-- /knowledge -->

An answer can be approve or reject, a choice, or text. In a thread, a reply that
names no option goes to the step's `otherwise` route, with its text. Several approvers
count as distinct people, and the person who started the run cannot approve it
unless the step allows it. An unanswered ask times out to its own route.

An agent step can also ask a question. The step waits, and the answer resumes the
agent's session.

## Build and run

<!-- knowledge: web -->
In the web app, you build a workflow on a canvas from a blank draft or a
starter. Ask Daimon, beside the canvas, edits the draft with you through the
workflow tools.
<!-- /knowledge -->

A private workflow is hidden from other members until you share it.

On Discord, Slack and Teams, ask the agent to build or change a workflow, and it
edits the draft with the same tools.
<!-- knowledge: web -->
It links the workflow's page in the web app for the full canvas.
<!-- /knowledge -->

`/workflows` opens the workflows panel on Discord, Slack and Teams. In chat, the
agent does not publish or start a live run itself. It posts a card for you to
press: the draft with Publish, or the inputs with Run. A run started from a card
shows on a run card that follows it.

A Finish step can post the result to the workflow's home channel, a thread, its
owner or the person it runs as. A Post step does the same mid-run, as
[Advanced workflows](workflows-advanced.md) shows. When runs fail, the owner gets one card for each
streak of failures.

## Grants and the Rule of Two

A step that sees outside input must not also write. Outside input is a webhook
body, an answer from someone who cannot edit the workflow, or a fetched web page.
Validation refuses such a step unless an Approval before it binds the write. A write inside a
[scope](workflows-advanced.md#writes-inside-a-scope) is the one exception.

A bound write sends exactly what was approved. An agent step set to propose
its writes sends only the ones a person approves. A test run never writes to
real systems.

Some settings need an admin's approval before a version goes live: writes
without asking, built-in tools, code with network access, an unsigned webhook,
a budget over the default, or a step that trusts outside input. The admin approves from the inbox, and the grant
carries over to later versions.

## Runs

You can cancel a run, retry it from the step that failed, or rerun it from any
step with the earlier results reused. Runs past a workflow's active limit queue.
A run ends when it passes its deadline or spends its budget.

Each step's spend counts against the allowance of the person the run is for.

## Routines and workflows

An admin can move a routine to a workflow and back from the CLI.
The workflow takes over the routine's schedule, posts waiting to go out and
settings, and a paused routine moves paused. A routine that is
running, or already moved, stays. A copy made while migrating from another
system moves like any other and keeps its link to its source. A workflow on a
one-time or multi-week schedule cannot move back.

<!-- knowledge: web -->
The web app can move one too.
<!-- /knowledge -->

The routine's message becomes an Agent step with `writes: policy`. The step works
where the routine posted. If it did not post for itself, a Post step sends its
result there under the routine's agent. If it did, the Post sends only the files it made. A routine that posts to its owner's
direct messages becomes a private step with personal memory, then a Post to its
owner. The preview lists what the admin approves by moving it, which is nothing.

A Post step takes an agent in `as_agent`. With [agent identity](agent-identity.md)
on, the post goes out under that agent's name and picture. Otherwise it comes
from Daimon.

## CLI

The `daimon workflows` commands act as a workspace admin. Name the workspace with
`--tenant`.

```bash
daimon workflows list --tenant <id>
daimon workflows show <workflow> --tenant <id>
daimon workflows export <workflow> --tenant <id> --out flows/
daimon workflows import flow.yaml --tenant <id> --owner <account>
daimon workflows runs --tenant <id> --workflow <workflow>
daimon workflows cancel <run> --tenant <id>
daimon workflows redrive <run> --tenant <id>
daimon workflows verify <run> --tenant <id>
daimon workflows from-routine --tenant <id>
daimon workflows from-routine --tenant <id> --routine <routine> --apply
daimon workflows to-routine <workflow> --tenant <id>
daimon routines translate --dry-run --all-tenants
```

An import arrives as a draft owned by `--owner`, who must be a member. `verify`
checks a run's record. `from-routine` only previews until you add `--apply` and
name each routine.

## Limits

- A run form in chat holds 5 fields on Discord and 10 on Slack and Teams.
<!-- knowledge: web -->
- A longer form opens in the web app.
<!-- /knowledge -->
- A draft card does not refresh: an older card's Test run and Publish are refused.
- Publishing from chat records no note.
- On Teams, a run confirmation card cannot start a run. Teams also replaces the
  pressed card with the run card, the run form or the result.
