# Advanced workflows

Post to chat, start runs from mentions and polls, save state between runs, write
without asking inside a scope, and follow runs on a board. Each is a field of the
workflow spec.

## Post to chat

A Post step sends a message and carries on. A Finish step posts the same way.
`to` is `home`, `owner`, `run_as`, `origin`, a channel or thread id, or a template.
`origin` is the thread a mention or a chat card started the run in.

```yaml
nodes:
  announce:
    kind: post
    to: "${{ inputs.channel }}"
    thread: "Weekly digest ${{ run.date }}"
    mention: ["person:123"]
    message: "${{ steps.draft.output.text }}"
```

A templated target must be one of the spec's `destinations`, a thread under a listed
channel, the run's `origin`, or a thread this run opened. Anything else is refused and
the step fails. A test run posts to the tester only. A post that fails before any of it
lands is sent again under the step's `retry`.

`thread` finds the thread with exactly that title under a channel, or creates it. A
post step can run inside a For each, once per item. Its output holds the `status`,
`channel`, `thread`, `message` and `link` of the post.

On Discord, a titled post also finds a thread by name. Slack and Teams threads
are found only from Daimon's own record. A `role:` mention is named but never pinged,
and a Teams post has no link.

## Mentions

A mention trigger starts a run when someone tags Daimon in one of its `channels`, or in
a message that passes its `match` expression. The run is for that person, and its
inputs are untrusted. A direct message never starts one.

A redelivered message starts no second run. A message that starts nothing, such as a
held concurrency key, stays an ordinary chat message.

## Polls

A poll trigger calls another service's read tool every `every`, from 1 minute to 1 day.
Each new row starts a run, found by its `key`. The first check only records what is
there. With `rule: updated` and `watch`, a change to a watched field starts a run for
each change, or one ever with `once`. The first poll pins the tool. A tool that changed since then pauses the
trigger until you publish again, which clears the pin.

A Wait step can also `poll`: it calls a read tool every `every` until `until` holds, or
its `timeout` route runs. A poll wait's result is untrusted input. While `workflow_polls` is
Off, nothing polls and no poll run starts.

## Schedules and durations

A schedule with `every_weeks` and an `anchor` date fires on its cron every
that many weeks. A schedule with `at` fires once, even late, then never again.

A duration like `2bd` counts business days, Monday to Friday, in the workflow's
`timezone`. Use it in waits and approvals.

## Saved state

A live run's Finish step can save `state`. The keys merge into the workflow's state
and a null value deletes a key. Later runs read it as `${{ workflow.state.key }}`.

`${{ workflow.last_run }}` is the latest succeeded live run, with its `number`,
`started_at`, `ended_at`, `output` and `posted` links. `${{ run.date }}` is the day the
run started in the workflow's timezone. `last(list, n)` takes a list's final items.

A test run or a failed run saves nothing. State over 64 KiB fails the run and
changes nothing. State is untrusted input, so a step that reads it and writes needs an
approval.

<!-- knowledge: web -->
In the web app, viewers see the State tab and editors clear it.
<!-- /knowledge -->

## Writes inside a scope

`writes_to` on an Action or an agent step lists the writes it may make with nobody
watching. Each entry names a `tool`, a `where` rule for every argument that matters,
and `free` for arguments that may be anything. A `where` rule is `in`, `prefix`,
`domains` or `created_by`.

```yaml
writes_to:
  - tool: notes/update_page
    where:
      page_id: { created_by: { tool: notes/create_page, id: ".id" } }
    free: [text]
```

A write goes out only when every argument fits. `created_by` allows only ids this run
made. A write that fits goes out even when outside input steers the step. A scope
needs `workflow_scoped_writes` and an admin's approval of its grant.

## Board and calendar

Give steps a `stage` and the spec a `card` of `fields`, with `date` naming one of them.
The Runs tab then shows a board with a column for each stage, then Done and Failed.
With a `date`, it shows a calendar of the runs by that date.

```bash
daimon workflows runs --tenant <id> --workflow <workflow> --view board
daimon workflows runs --tenant <id> --workflow <workflow> --view calendar --month 2026-11
```
