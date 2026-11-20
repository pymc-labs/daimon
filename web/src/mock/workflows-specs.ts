// Seeded workflow specs, as authors would write them. Expressions inside flow mappings are quoted so they parse.
import { fromYaml } from "@/modules/workflows/yaml"
import type { Spec } from "@/modules/workflows/types"

const parse = (text: string): Spec => {
  const r = fromYaml(text)
  if (r.error != null) throw new Error(`seed spec line ${r.line}: ${r.error}`)
  return r.spec
}

export const INBOUND_LEAD = `schema: daimon.workflow/v1
name: Inbound lead
goal: Qualify website enquiries and draft a reply for sales.
home: {channel: "C0SALES"}              # budget, readers rules, default delivery
visibility: shared                      # private | shared
label: \${{ inputs.email }}              # shown and searchable in run lists
inputs:                                 # JSON Schema subset; drives the run form
  type: object
  required: [email, message]
  properties:
    name: {type: string, title: Name}
    email: {type: string, format: email, title: Email}
    message: {type: string, maxLength: 5000, title: Message}
triggers:
  - id: site_form
    kind: webhook
    verify: standard-webhooks
    inputs: \${{ payload.data }}         # payload to inputs; validated against inputs
    respond: accepted                   # 202 with the delivery id
concurrency: {key: "\${{ inputs.email }}", limit: 1, on_conflict: skip}
defaults: {agent: sales, budget_usd: 2}
limits: {deadline: 3d}
start: research
nodes:
  research:
    kind: agent
    title: Research the lead
    instructions: |
      Find the sender's company, its size and what it likely needs.
      Use web search and HubSpot reads. Contact nobody.
    inputs: {lead: "\${{ inputs }}"}
    output:
      company: {type: string}
      fit: {enum: [strong, possible, poor]}
      notes: {type: string}
    routes:
      qualified: {to: draft_reply, when: The lead fits our services.}
      unqualified: {to: done, when: "Spam, students, job seekers or no fit."}
  draft_reply:
    kind: agent
    instructions: Draft a short, warm reply in our voice that proposes a call.
    inputs: {lead: "\${{ inputs }}", research: "\${{ steps.research.output }}"}
    output: {subject: {type: string}, body: {type: string}}
    next: review
  review:
    kind: approval
    approvers: [owner]
    show: |
      **\${{ steps.research.output.company }}** (\${{ steps.research.output.fit }})
      \${{ steps.draft_reply.output.body }}
    form: {body: {type: string, default: "\${{ steps.draft_reply.output.body }}"}}
    binds: send                         # the approver sees the exact email send will send
    routes: {approve: send, reject: done}
    timeout: {after: 2d, route: reject}
  send:
    kind: action
    tool: gmail/send_message
    args:
      to: \${{ inputs.email }}
      subject: \${{ steps.draft_reply.output.subject }}
      body: \${{ steps.review.output.body }}
    next: done
  done:
    kind: finish
    output: \${{ steps.research.output }}
    deliver: {to: home, message: "\${{ steps.research.output.notes }}"}
`

const DIGEST = `schema: daimon.workflow/v1
name: Weekly pipeline digest
goal: Summarise open deals every Monday and flag the risky ones.
home: {channel: C0SALES}
visibility: shared
triggers:
  - {id: monday, kind: schedule, cron: "0 9 * * 1", timezone: Europe/Stockholm, catch_up: run-once}
defaults: {agent: sales, budget_usd: 1}
start: collect
nodes:
  collect:
    kind: agent
    title: Collect open deals
    instructions: List open HubSpot deals with stage, amount, close date and last activity. Mark deals with no activity in 14 days or a slipped close date as risky.
    tools: [hubspot/search_crm, hubspot/get_object]
    output:
      deals: {type: array, items: {type: object}}
      any_risky: {type: boolean}
      total: {type: number}
    next: risky
  risky:
    kind: branch
    title: Any risky deals
    cases:
      - {when: "\${{ steps.collect.output.any_risky }}", to: write}
    else: done_quiet
  write:
    kind: agent
    title: Write the digest
    instructions: Write a short digest. Risky deals first, one line each with the reason and a next step.
    inputs: {deals: "\${{ steps.collect.output.deals }}"}
    output: {digest: {type: string}}
    next: done
  done:
    kind: finish
    title: Post to sales
    deliver: {to: home, message: "\${{ steps.write.output.digest }}"}
  done_quiet:
    kind: finish
    title: Nothing risky
`

const MEETING = `schema: daimon.workflow/v1
name: Meeting follow-up
goal: Turn client meeting notes into a summary and Linear issues.
home: {channel: C0DELIVERY}
visibility: shared
triggers:
  - {id: meeting_ended, kind: webhook, verify: standard-webhooks, inputs: {meeting_id: "\${{ payload.meeting.external }}"}, respond: accepted}
inputs:
  type: object
  required: [meeting_id]
  properties:
    meeting_id: {type: string, title: Meeting}
defaults: {agent: daimon}
start: summarise
nodes:
  summarise:
    kind: agent
    title: Summarise the meeting
    instructions: Read the transcript. List decisions, open questions and action items with owners.
    inputs: {meeting: "\${{ inputs.meeting_id }}"}
    tools: [bluedot/get_transcript]
    output:
      summary: {type: string}
      actions: {type: array, items: {type: object}}
    next: file
  file:
    kind: agent
    title: File action items
    instructions: Create one Linear issue per action item in the client's project. Skip items already filed.
    inputs: {actions: "\${{ steps.summarise.output.actions }}"}
    tools: [linear/create_issue, linear/query_issues]
    writes: propose
    next: done
  done:
    kind: finish
    deliver: {to: home, message: "\${{ steps.summarise.output.summary }}"}
`

const PROFIT = `schema: daimon.workflow/v1
name: Monthly profitability
goal: Report project margins on the first of each month.
home: {channel: C0LEADS}
visibility: shared
triggers:
  - {id: monthly, kind: schedule, cron: "0 7 1 * *", timezone: Europe/London, catch_up: run-once}
defaults: {agent: daimon, budget_usd: 2}
start: fetch
nodes:
  fetch:
    kind: action
    title: Fetch hours and rates
    tool: toggl/project_profitability
    args: {period: last_month}
    next: compute
  compute:
    kind: code
    title: Compute margins
    language: python
    network: "off"
    source: |
      import json
      rows = json.load(open("input.json"))["projects"]
      out = [{**r, "margin": (r["revenue"] - r["cost"]) / r["revenue"]} for r in rows if r["revenue"]]
      json.dump({"projects": out}, open("output.json", "w"))
    inputs: {data: "\${{ steps.fetch.output }}"}
    output: {projects: {type: array, items: {type: object}}}
    next: explain
  explain:
    kind: agent
    title: Explain the month
    instructions: Explain which projects moved margin and why, in five bullets at most.
    inputs: {projects: "\${{ steps.compute.output.projects }}"}
    output: {report: {type: string}}
    next: done
  done:
    kind: finish
    title: Post to leads
    deliver: {to: home, message: "\${{ steps.explain.output.report }}"}
`

const TRIAGE = `schema: daimon.workflow/v1
name: Issue triage
goal: Classify new GitHub issues, reproduce bugs and draft a response.
home: {channel: C0TRIAGE}
visibility: shared
label: \${{ inputs.title }}
inputs:
  type: object
  required: [number, title, body]
  properties:
    number: {type: integer, title: Issue}
    title: {type: string, title: Title}
    body: {type: string, title: Body}
triggers:
  - {id: github_issues, kind: webhook, verify: github, inputs: "\${{ payload.issue }}", respond: accepted}
concurrency: {key: "\${{ inputs.number }}", limit: 1, on_conflict: replace}
defaults: {agent: reviewer, budget_usd: 3}
start: classify
nodes:
  classify:
    kind: agent
    title: Classify the issue
    instructions: Read the issue and decide what it is.
    inputs: {issue: "\${{ inputs }}"}
    output: {area: {type: string}, severity: {enum: [low, medium, high]}}
    routes:
      bug: {to: reproduce, when: Something is broken.}
      question: {to: respond, when: A usage question.}
      feature: {to: respond, when: A feature request.}
      spam: {to: ignore, when: Spam or off topic.}
  reproduce:
    kind: agent
    title: Reproduce the bug
    instructions: Check out the repository and try to reproduce the bug. Report the minimal steps.
    inputs: {issue: "\${{ inputs }}"}
    session: fresh
    builtins: all
    untrusted_waiver: Sandboxed checkout, no secrets
    output: {reproduced: {type: boolean}, steps: {type: string}}
    next: respond
  respond:
    kind: agent
    title: Draft a response
    instructions: Draft a reply on the issue. Thank the reporter and say what happens next.
    inputs: {issue: "\${{ inputs }}", area: "\${{ steps.classify.output.area }}"}
    tools: [github/create_comment]
    writes: propose
    join: first
    next: done
  done:
    kind: finish
    deliver: {to: home, message: "\${{ steps.classify.output.area }}"}
  ignore:
    kind: finish
    title: Spam
`

const RESEARCH = `schema: daimon.workflow/v1
name: Competitor research
goal: Profile each competitor and synthesise a comparison.
visibility: private
inputs:
  type: object
  required: [competitors]
  properties:
    competitors: {type: array, items: {type: string}, title: Competitors}
triggers:
  - {id: manual, kind: manual}
defaults: {agent: daimon, budget_usd: 4}
start: each
nodes:
  each:
    kind: for_each
    title: Profile each competitor
    items: \${{ inputs.competitors }}
    as: competitor
    concurrency: 5
    tolerate_failures: 1
    do:
      kind: agent
      instructions: Profile the competitor from its website and recent news. Pricing, positioning, customers.
      inputs: {name: "\${{ item }}"}
      builtins: read
    next: synthesise
  synthesise:
    kind: agent
    title: Synthesise
    instructions: Compare the profiles in a table and list three openings for us.
    inputs: {profiles: "\${{ steps.each.output }}"}
    output: {report: {type: string}}
    next: done
  done:
    kind: finish
    output: \${{ steps.synthesise.output }}
`

const CONTENT = `schema: daimon.workflow/v1
name: Content review loop
goal: Draft a blog post, critique it until it is good, then publish to Notion.
visibility: shared
inputs:
  type: object
  required: [topic]
  properties:
    topic: {type: string, title: Topic}
    audience: {type: string, title: Audience}
triggers:
  - {id: manual, kind: manual}
defaults: {agent: daimon, budget_usd: 3}
start: write
nodes:
  write:
    kind: agent
    title: Write the draft
    instructions: Write or revise the post. Apply every point of the latest critique.
    inputs: {topic: "\${{ inputs.topic }}", critique: "\${{ steps.critique.output.points }}"}
    output: {title: {type: string}, body: {type: string}}
    max_visits: 3
    on_max_visits: approve
    next: critique
  critique:
    kind: agent
    title: Critique
    instructions: Critique the draft against our style guide. Accept only if nothing important is left.
    inputs: {draft: "\${{ steps.write.output }}"}
    output: {points: {type: array, items: {type: string}}}
    routes:
      revise: {to: write, when: Important points remain.}
      accept: {to: approve, when: Ready for an editor.}
  approve:
    kind: approval
    title: Editor approval
    approvers: [editors]
    show: \${{ steps.write.output.body }}
    binds: create_page
    routes: {approve: create_page, reject: rejected}
    timeout: {after: 3d, route: reject}
  create_page:
    kind: action
    title: Create Notion page
    tool: notion/create_page
    args:
      title: \${{ steps.write.output.title }}
      body: \${{ steps.write.output.body }}
    next: done
  done:
    kind: finish
    title: Published
  rejected:
    kind: finish
    title: Rejected
    outcome: failed
`

const ONBOARD = `schema: daimon.workflow/v1
name: Client onboarding
goal: Set up a new client's workspace and send the welcome pack before kickoff.
home: {channel: C0CLIENTS}
visibility: shared
label: \${{ inputs.client }}
inputs:
  type: object
  required: [client, contact_email, kickoff_date]
  properties:
    client: {type: string, title: Client}
    contact_email: {type: string, format: email, title: Contact}
    kickoff_date: {type: string, format: date, title: Kickoff}
triggers:
  - {id: deal_won, kind: webhook, verify: standard-webhooks, inputs: "\${{ payload.deal }}", respond: accepted}
  - {id: manual, kind: manual}
defaults: {agent: daimon, budget_usd: 3}
limits: {deadline: 60d}
start: confirm
nodes:
  confirm:
    kind: approval
    title: Confirm setup
    approvers: [owner]
    show: Set up \${{ inputs.client }}, kickoff \${{ inputs.kickoff_date }}
    binds: [folder, page, project]
    routes: {approve: setup, reject: cancelled}
  setup:
    kind: transform
    title: Names
    set:
      slug: \${{ inputs.client.lowerAscii() }}
    next: [folder, page, project]
  folder:
    kind: action
    title: Drive folder
    tool: gdrive/create_folder
    args: {name: "\${{ inputs.client }}"}
    next: welcome
  page:
    kind: action
    title: Notion page
    tool: notion/create_page
    args: {title: "\${{ inputs.client }}"}
    next: welcome
  project:
    kind: action
    title: Linear project
    tool: linear/create_project
    args: {name: "\${{ inputs.client }}"}
    next: welcome
  welcome:
    kind: agent
    title: Draft the welcome email
    join: all
    instructions: Draft a welcome email with the folder, page and project links and the kickoff date.
    inputs: {client: "\${{ inputs.client }}", kickoff: "\${{ inputs.kickoff_date }}"}
    output: {subject: {type: string}, body: {type: string}}
    next: review
  review:
    kind: approval
    title: Review the email
    approvers: [owner]
    show: \${{ steps.welcome.output.body }}
    form: {body: {type: string, default: "\${{ steps.welcome.output.body }}"}}
    binds: send
    routes: {approve: send, reject: before_kickoff}
  send:
    kind: action
    title: Send welcome
    tool: gmail/send_message
    args:
      to: \${{ inputs.contact_email }}
      subject: \${{ steps.welcome.output.subject }}
      body: \${{ steps.review.output.body }}
    next: before_kickoff
  before_kickoff:
    kind: wait
    title: Day before kickoff
    until: \${{ timestamp(inputs.kickoff_date) - duration("24h") }}
    next: brief
  brief:
    kind: agent
    title: Brief the team
    instructions: Write a one-page kickoff brief for the team.
    inputs: {client: "\${{ inputs.client }}"}
    output: {brief: {type: string}}
    next: done
  done:
    kind: finish
    deliver: {to: home, message: "\${{ steps.brief.output.brief }}"}
  cancelled:
    kind: finish
    title: Cancelled
    outcome: failed
`

const ALERT = `schema: daimon.workflow/v1
name: Alert triage
goal: Investigate production alerts and page on-call only when it matters.
home: {channel: C0ONCALL}
visibility: shared
label: \${{ inputs.alert_key }}
inputs:
  type: object
  required: [alert_key, summary]
  properties:
    alert_key: {type: string, title: Alert}
    summary: {type: string, title: Summary}
triggers:
  - {id: grafana, kind: webhook, verify: standard-webhooks, start_when: "\${{ payload.status == 'firing' }}", inputs: "\${{ payload.alert }}", respond: accepted}
concurrency: {key: "\${{ inputs.alert_key }}", on_conflict: debounce, debounce: 5m}
defaults: {agent: reviewer, budget_usd: 2}
start: investigate
nodes:
  investigate:
    kind: agent
    title: Investigate
    instructions: Check logs, recent deploys and dashboards. Estimate severity and the likely cause.
    inputs: {alert: "\${{ inputs }}"}
    builtins: read
    output: {severity: {enum: [low, medium, high]}, cause: {type: string}}
    next: severity
  severity:
    kind: branch
    title: Severity
    cases:
      - {when: "\${{ steps.investigate.output.severity == 'high' }}", to: page}
    else: watch
  page:
    kind: finish
    title: Page on-call
    deliver: {to: "#oncall", message: "\${{ steps.investigate.output.cause }}"}
  watch:
    kind: wait
    title: Wait for resolve
    event: {source: "trigger:grafana", match: "\${{ event.payload.status == 'resolved' }}", key: "\${{ inputs.alert_key }}"}
    timeout: 2h
    routes: {arrived: quiet, timed_out: escalate}
  quiet:
    kind: finish
    title: Resolved
  escalate:
    kind: finish
    title: Escalate
    deliver: {to: "#oncall", message: "\${{ steps.investigate.output.cause }}"}
`

const oneStep = (name: string, agent: string, cron: string, tz: string, text: string, channel: string) => `schema: daimon.workflow/v1
name: ${name}
visibility: private
home: {channel: ${channel}}
triggers:
  - {id: schedule, kind: schedule, cron: "${cron}", timezone: ${tz}, catch_up: run-once}
defaults: {agent: ${agent}}
start: run
nodes:
  run:
    kind: agent
    title: ${name}
    instructions: ${text}
    session: main
    output: {summary: {type: string}}
    next: done
  done:
    kind: finish
    deliver: {to: home, message: "\${{ steps.run.output.summary }}"}
`

const BOARD = `schema: daimon.workflow/v1
name: Quarterly board pack
goal: Assemble the quarterly board pack from finance and delivery data.
visibility: private
triggers:
  - {id: manual, kind: manual}
defaults: {agent: daimon}
start: gather
nodes:
  gather:
    kind: agent
    title: Gather numbers
    instructions: Collect revenue, margin and headcount for the quarter.
    output: {numbers: {type: object}}
    next: draft
  draft:
    kind: agent
    title: Draft the pack
    instructions: Draft the board pack in our template.
    inputs: {numbers: "\${{ steps.gather.output.numbers }}"}
    output: {doc: {type: string}}
    next: done
  done:
    kind: finish
`

const RENEWAL = `schema: daimon.workflow/v1
name: Renewal reminders
goal: Remind account owners 30 days before a contract renews.
home: {channel: C0SALES}
visibility: shared
triggers:
  - {id: daily, kind: schedule, cron: "0 8 * * 1-5", timezone: Europe/London, catch_up: skip}
defaults: {agent: sales}
start: find
nodes:
  find:
    kind: agent
    title: Find renewals
    instructions: List contracts renewing in the next 30 days with their account owner.
    output: {renewals: {type: array, items: {type: object}}}
    next: done
  done:
    kind: finish
    deliver: {to: home, message: "\${{ steps.find.output.renewals }}"}
`

export const SPECS = {
  lead: parse(INBOUND_LEAD), digest: parse(DIGEST), meeting: parse(MEETING), profit: parse(PROFIT), triage: parse(TRIAGE),
  research: parse(RESEARCH), content: parse(CONTENT), onboard: parse(ONBOARD), alert: parse(ALERT), board: parse(BOARD), renewal: parse(RENEWAL),
  readout: parse(oneStep("Weekly channel readout", "mmm", "0 9 * * 1", "Europe/Stockholm", "Summarize last week's channel contribution for Larkspur and flag any interval wider than 20%.", "C0MMM")),
  price: parse(oneStep("Overnight price test", "pricing", "30 8 * * 1-5", "America/New_York", "Post the overnight price test readout.", "C0PRICING")),
  prs: parse(oneStep("Stale pull requests", "reviewer", "0 17 * * 5", "Europe/London", "List pull requests open for more than a week.", "C0ENG")),
}

// ---------- Templates ----------

const TEMPLATE_YAML: Record<string, string> = {
  daily_digest: `schema: daimon.workflow/v1
name: Daily digest
visibility: private
triggers:
  - {id: daily, kind: schedule, cron: "0 8 * * 1-5", timezone: Europe/London, catch_up: skip}
start: collect
nodes:
  collect:
    kind: agent
    title: Collect updates
    instructions: Collect what changed since yesterday.
    output: {items: {type: array, items: {type: object}}, any: {type: boolean}}
    next: anything
  anything:
    kind: branch
    title: Anything new
    cases:
      - {when: "\${{ steps.collect.output.any }}", to: write}
    else: quiet
  write:
    kind: agent
    title: Write the digest
    instructions: Write a short digest.
    inputs: {items: "\${{ steps.collect.output.items }}"}
    output: {digest: {type: string}}
    next: done
  done:
    kind: finish
    deliver: {to: home, message: "\${{ steps.write.output.digest }}"}
  quiet:
    kind: finish
    title: Nothing new
`,
  webhook_triage: `schema: daimon.workflow/v1
name: Webhook triage
visibility: private
inputs: {type: object, properties: {body: {type: object}}}
triggers:
  - {id: hook, kind: webhook, verify: standard-webhooks, inputs: "\${{ payload }}", respond: accepted}
start: classify
nodes:
  classify:
    kind: agent
    title: Classify
    instructions: Classify the delivery.
    inputs: {body: "\${{ inputs.body }}"}
    routes:
      act: {to: act, when: Needs a person.}
      ignore: {to: ignored, when: Noise.}
  act:
    kind: finish
    title: Tell the channel
    deliver: {to: home, message: "\${{ steps.classify.summary }}"}
  ignored:
    kind: finish
    title: Ignored
`,
  approve_send: `schema: daimon.workflow/v1
name: Approve then send
visibility: private
inputs: {type: object, required: [to], properties: {to: {type: string, format: email, title: To}}}
triggers:
  - {id: manual, kind: manual}
start: draft
nodes:
  draft:
    kind: agent
    title: Draft the email
    instructions: Draft the email.
    output: {subject: {type: string}, body: {type: string}}
    next: approve
  approve:
    kind: approval
    approvers: [owner]
    show: \${{ steps.draft.output.body }}
    binds: send
    routes: {approve: send, reject: done}
  send:
    kind: action
    tool: gmail/send_message
    args: {to: "\${{ inputs.to }}", subject: "\${{ steps.draft.output.subject }}", body: "\${{ steps.draft.output.body }}"}
    next: done
  done:
    kind: finish
`,
  research_each: `schema: daimon.workflow/v1
name: Research each
visibility: private
inputs: {type: object, required: [items], properties: {items: {type: array, items: {type: string}, title: Items}}}
triggers:
  - {id: manual, kind: manual}
start: each
nodes:
  each:
    kind: for_each
    title: Research each
    items: \${{ inputs.items }}
    concurrency: 3
    do: {kind: agent, instructions: Research the item., inputs: {item: "\${{ item }}"}}
    next: combine
  combine:
    kind: agent
    title: Combine
    instructions: Combine the findings.
    inputs: {found: "\${{ steps.each.output }}"}
    next: done
  done:
    kind: finish
`,
  review_loop: `schema: daimon.workflow/v1
name: Review loop
visibility: private
triggers:
  - {id: manual, kind: manual}
start: write
nodes:
  write:
    kind: agent
    title: Write
    instructions: Write or revise.
    output: {text: {type: string}}
    max_visits: 3
    on_max_visits: done
    next: critique
  critique:
    kind: agent
    title: Critique
    instructions: Critique the text.
    inputs: {text: "\${{ steps.write.output.text }}"}
    routes:
      revise: {to: write, when: Needs work.}
      accept: {to: done, when: Good enough.}
  done:
    kind: finish
`,
  onboarding: `schema: daimon.workflow/v1
name: Onboarding
visibility: private
inputs: {type: object, required: [name], properties: {name: {type: string, title: Name}}}
triggers:
  - {id: manual, kind: manual}
start: setup
nodes:
  setup:
    kind: transform
    title: Names
    set: {slug: "\${{ inputs.name.lowerAscii() }}"}
    next: [folder, page]
  folder:
    kind: action
    tool: gdrive/create_folder
    args: {name: "\${{ inputs.name }}"}
    next: welcome
  page:
    kind: action
    tool: notion/create_page
    args: {title: "\${{ inputs.name }}"}
    next: welcome
  welcome:
    kind: agent
    title: Welcome
    instructions: Draft a welcome note.
    next: kickoff
  kickoff:
    kind: wait
    title: Wait a day
    for: 1d
    next: done
  done:
    kind: finish
`,
}
export const TEMPLATE_SPECS: Record<string, Spec> = Object.fromEntries(Object.entries(TEMPLATE_YAML).map(([k, v]) => [k, parse(v)]))
