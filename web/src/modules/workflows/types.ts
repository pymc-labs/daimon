// The workflow spec (schema daimon.workflow/v1) and the shapes the workflows API returns.
import type { AgentRef } from "@/lib/types"

export const KINDS = ["agent", "branch", "transform", "code", "action", "approval", "wait", "for_each", "workflow", "post", "finish"] as const
export type Kind = (typeof KINDS)[number]

/** A JSON Schema subset: one field of inputs, outputs and approval forms. */
export type Field = {
  type?: "string" | "number" | "integer" | "boolean" | "object" | "array"
  enum?: string[]; title?: string; description?: string; format?: string; default?: unknown; maxLength?: number; items?: Field
}
export type ObjectSchema = { type: "object"; required?: string[]; properties: Record<string, Field> }

/** An agent route: target and when to pick it. Approval and wait routes are bare targets. */
export type Route = { to: string; when?: string }
export type Case = { when: string; to: string }
/** A `writes_to` rule on one argument: allowed values, prefixes, mail domains, or ids this run created. */
export type ArgRule = { in?: (string | number | boolean)[]; prefix?: string[]; domains?: string[]; created_by?: { tool: string; id: string } }
/** A write a step may make unattended: its tool, fixed rules per argument and the arguments left free. */
export type WriteScope = { tool: string; where?: Record<string, ArgRule>; free?: string[] }
/** Where a Finish result or a post goes: a target, a thread title, extra DMs and mentions. */
export type Deliver = { to: string; thread?: string; also?: string[]; mention?: string[]; message?: string; files?: string }

export type Node = {
  kind: Kind; title?: string; description?: string; stage?: string
  next?: string | string[]; on_error?: string; retry?: { attempts: number }; timeout?: string | { after: string; route: string }
  budget_usd?: number; max_visits?: number; on_max_visits?: string; join?: "all" | "first"
  // agent
  agent?: string; model?: string; instructions?: string; inputs?: Record<string, string>
  /** Agent, code: fields. Finish: an expression. */
  output?: Record<string, Field> | string
  routes?: Record<string, string | Route>; tools?: "inherit" | string[]; writes?: "refuse" | "propose" | "allow" | "policy"
  builtins?: "none" | "read" | "all"; session?: string; can_ask?: boolean; can_finish_early?: boolean; untrusted_waiver?: string
  /** Agent, action: writes that go out unattended. Agent: skills from the library. */
  writes_to?: WriteScope[]; skills?: string[]
  // branch
  cases?: Case[]; else?: string
  // transform
  set?: Record<string, string>
  // code
  language?: "python"; source?: string; network?: "off" | "on"
  // action
  tool?: string; args?: Record<string, string>; idempotent?: boolean
  // approval
  approvers?: string[]; min_approvals?: number; allow_self_approval?: boolean; show?: string; form?: Record<string, Field>; binds?: string | string[]; remind_every?: string
  /** Where the ask is posted (`deliver`, with the channel for a thread), what it attaches (a files reference) and the shape of the answer. */
  channel?: string; attach?: string; answer?: AnswerShape
  /** Where a reply with no option word goes. */
  otherwise?: string
  // wait
  for?: string; until?: string; event?: { source: string; match?: string }
  poll?: { agent?: string; tool: string; args?: Record<string, unknown>; every: string; until: string }
  /** Agent: where it works (`home`, `origin`, a channel or thread id) and whether the run-as person's personal memory is mounted, which keeps its result private. */
  at?: string; memory?: "personal"
  // post (also Deliver's fields); `as_agent` posts under that agent's name and picture
  to?: string; thread?: string; also?: string[]; mention?: string[]; message?: string; files?: string; as_agent?: string
  // for_each
  items?: string; as?: string; do?: Node; concurrency?: number; tolerate_failures?: number
  // workflow
  workflow?: string; version?: "live" | number
  // finish
  outcome?: "succeeded" | "failed"; state?: Record<string, string>
  /** Finish: where the result goes. Approval: where the ask is posted. */
  deliver?: Deliver | AskWhere | null
}

/** Where an approval is asked: the inbox only, a direct message, or a thread in a channel. */
export type AskWhere = "inbox" | "dm" | "thread"
export type AnswerShape = "approve" | "choice" | "text" | "form"
export type Platform = "discord" | "slack" | "teams"

/** What earlier runs saved with a Finish step (`workflow.state`). */
export type SavedState = { state: Record<string, unknown>; updated_at: string | null }

export type TriggerKind = "manual" | "schedule" | "webhook" | "event" | "call" | "mention" | "poll"
export type Trigger = {
  id: string; kind: TriggerKind
  cron?: string; timezone?: string; catch_up?: "skip" | "run-once"; every_weeks?: number; anchor?: string; at?: string
  verify?: string; start_when?: string; inputs?: string | Record<string, string>; respond?: "accepted" | "result"
  source?: string; match?: string
  // mention
  channels?: string[]
  // poll
  agent?: string; tool?: string; args?: Record<string, unknown>; every?: string; items?: string; key?: string
  rule?: "added" | "updated"; watch?: string[]; once?: boolean; spend_cap_usd?: number
}

export type Spec = {
  schema: "daimon.workflow/v1"; name: string; goal?: string; home?: { channel: string }; visibility?: "private" | "shared"; label?: string
  inputs?: ObjectSchema; triggers?: Trigger[]
  concurrency?: { key?: string; limit?: number; on_conflict?: "queue" | "skip" | "replace" | "debounce"; debounce?: string }
  defaults?: { agent?: string; budget_usd?: number }; limits?: { deadline?: string; max_steps?: number; budget_usd?: number }
  /** Channels and people templated targets may reach; the board card; business days count in `timezone`. */
  destinations?: string[]; card?: { fields: Record<string, string>; date?: string }; timezone?: string
  start: string; nodes: Record<string, Node>
}

// ---------- API ----------

export type Person = { id: string; name: string }
export type Status = "live" | "paused" | "draft" | "needs_owner" | "needs_review"
export type RunStatus = "queued" | "running" | "waiting" | "succeeded" | "failed" | "cancelled" | "timed_out"
export type StepStatus = "pending" | "ready" | "running" | "waiting" | "succeeded" | "failed" | "skipped" | "cancelled" | "unknown"
export type Tick = { number: number; status: RunStatus; at: string }

export type WorkflowSummary = {
  id: string; name: string; goal: string; status: Status; owner: Person; run_as: Person | null
  live_version: number | null; draft_revision: number | null; draft_changes: number
  triggers: Trigger[]; ticks: Tick[]; next_run: string | null; spend_usd: number
  visibility: "private" | "shared"; mine: boolean; shared_with_me: boolean; failing: boolean; steps: number
  /** Set on one-step workflows migrated from a routine. */
  source_routine: { id: string; name: string } | null
}
export type Workflow = WorkflowSummary & {
  /** The live spec, or the draft's when nothing is live. */
  spec: Spec; home_name: string; editors: Person[]; runners: Person[]; viewers: Person[]
  monthly_cap_usd: number | null; can_edit: boolean; can_run: boolean; updated_at: string
  /** Admins approve new grants. Owners are not admins by that role. */
  is_admin: boolean
  pending_request: PublishRequest | null
  /** The last publish in this session, shown on the page until the next edit. */
  published: { version: number; by: Person; at: string; note: string; approved_by: Person | null } | null
  /** One-step workflows migrated from a routine: its last result before the move. */
  migration: { routine: { id: string; name: string }; migrated_at: string; last: { at: string; status: RunStatus; text: string } | null } | null
  /** Run budget per run, from the spec. */
  run_budget_usd: number | null
}
export type WorkflowPatch = Partial<Pick<Workflow, "name" | "visibility" | "monthly_cap_usd">> & { paused?: boolean; editors?: string[]; runners?: string[] }

export type Severity = "error" | "warning"
export type Problem = { severity: Severity; code: string; message: string; node?: string }
export type GrantKind = "unattended_write" | "builtins" | "code_network" | "unverified_webhook" | "budget_over_default" | "untrusted_waiver" | "scoped_write"
export type Grant = { kind: GrantKind; node?: string; detail: string }
/** Rule of Two: Untrusted input, Sensitive data, Writes. */
export type RuleClass = "U" | "S" | "W"
export type Validation = { errors: Problem[]; warnings: Problem[]; grants: Grant[]; classes: Record<string, RuleClass[]> }

export type Change = { node: string | null; field?: string; effect: "added" | "changed" | "removed" }
export type Draft = {
  revision: number; base_version: number | null; spec: Spec; changes: Change[]; validation: Validation
  /** Grants the live version does not hold; publishing them waits on an admin. */
  new_grants: Grant[]; updated_at: string; updated_by: Person
}
export type DraftPut = { base_revision: number; spec: Spec }

export type Version = { number: number; published_by: Person; published_at: string; note: string; grants: GrantKind[]; runs: number; live: boolean }

export type WaitingReason = "approval" | "question" | "timer" | "event" | "poll" | "blocked" | "unknown"
export type RunSummary = {
  id: string; number: number; status: RunStatus; label: string | null; trigger: { kind: TriggerKind; id: string }
  started_at: string; duration_ms: number | null; current_step: string | null; cost_usd: number; version: number; test: boolean
  /** Title of the current step, or of the step a failed run ended on. */
  step_title: string | null
  workflow: { id: string; name: string }; waiting_reason: WaitingReason | null; triggered_by: Person | null
  /** Set once Retry from failure ran on this run. */
  redriven: boolean
}

/** A run on the board or the calendar: its stage now, the card fields the spec names, and its calendar day. */
export type RunCard = { run: RunSummary; stage: string; card: Record<string, string | number | boolean | null>; attention: boolean; date: string | null }
export type Board = { view: "board"; columns: { stage: string; cards: RunCard[] }[] }
export type Calendar = { view: "calendar"; month: string; cards: RunCard[] }

// ---------- Run page ----------

export type ErrorClass = "retryable" | "gate" | "contract" | "agent" | "unknown" | "fatal"
export type AttemptStatus = "running" | "succeeded" | "failed" | "abandoned"
export type Attempt = {
  n: number; status: AttemptStatus; started_at: string; duration_ms: number | null; cost_usd: number
  session: string | null; error: { class: ErrorClass; message: string } | null
}
export type WaitKind = "approval" | "question" | "timer" | "event" | "poll"
/** A file a run holds: attached to an ask, or sent back with an answer. `url` is `/api/workflows/runs/:run/files/:fid`. */
export type FileRef = { name: string; size: number; url: string }
/** Where an ask was posted: a direct message or a thread, with a link when the platform gives one. */
export type Sent = { platform: Platform; where: string; link: string | null; at: string }
export type Answer = {
  by: Person; via: "web" | Platform; text: string | null; values: Record<string, unknown>; files: FileRef[]; at: string
  /** A question's fields by name, in the order asked: their values show under these labels. */
  labels?: Record<string, string>
}
export type RunWait = {
  /** The wait's id: the inbox item to decide it by. */
  id: string
  kind: WaitKind; status: "waiting" | "completed" | "timed_out" | "cancelled"; addressees: Person[]
  opened_at: string; expires_at: string | null; closed_at: string | null; decision: string | null; decider: Person | null
  sent: Sent[]; attachments: FileRef[]; answer: Answer | null
}
/** One visit of a node, or one For each item. `key` is `node`, `node#2` for a later visit, `node[3]` for an item. */
export type StepRun = {
  key: string; node: string; visit: number; item: { index: number; label: string } | null
  status: StepStatus; started_at: string | null; ended_at: string | null; cost_usd: number
  route: string | null; attempts: Attempt[]; wait: RunWait | null; lane: string | null
  /** For each: items done and in all. */
  progress: { done: number; total: number } | null
  /** Why it failed or waits for a decision. */
  reason: string | null
  /** Edge keys (`from:slot`) that led here. */
  via: string[]
  /** Reused from an earlier run (Run from here) or a pinned output (test run). */
  reused: "fork" | "pinned" | null
}
export type Lane = { id: string; agent: string; session: string }
export type RunFailure = { reason: string; message: string; step: string | null }
/** What the ledger projects onto the run; the stream's events update it. */
export type RunState = {
  status: RunStatus; waiting_reason: WaitingReason | null; failure: RunFailure | null; outcome: "completed" | "nothing_to_do" | null
  ended_at: string | null; cost_usd: number; steps: StepRun[]; lanes: Lane[]; current_step: string | null
}
/** `pinned` are the steps whose output is reused from run `pinned_from`. */
export type RunTest = { live_writes: boolean; pinned: string[]; pinned_from: string | null }
export type RunDetail = RunSummary & RunState & {
  spec: Spec; inputs: Record<string, unknown>; untrusted_inputs: boolean; parent: { id: string; number: number } | null
  delivery: string | null; test_options: RunTest | null; budget_usd: number; deadline_at: string | null
  events: number; head_hash: string
}
export type EventType =
  | "run.queued" | "run.started" | "run.waiting" | "run.resumed" | "run.succeeded" | "run.failed" | "run.cancelled" | "run.timed_out" | "run.redriven" | "run.forked_from"
  | "step.ready" | "step.skipped" | "step.started" | "step.brief_sent" | "step.question_asked" | "step.output_recorded" | "step.route_chosen"
  | "step.contract_rejected" | "step.nudged" | "step.retry_scheduled" | "step.succeeded" | "step.failed" | "step.cancelled" | "step.unknown" | "step.resolved_by"
  | "proposal.recorded" | "proposal.decided" | "proposal.sent" | "wait.opened" | "wait.posted" | "wait.reminded" | "wait.answer_refused" | "wait.decided" | "wait.timed_out"
  | "budget.checked" | "budget.exceeded" | "delivery.queued" | "delivery.sent" | "delivery.skipped" | "delivery.refused"
/** One ledger row. `data` carries what the projection needs; `hash` chains it to the row before. */
export type RunEvent = {
  seq: number; at: string; type: EventType; step: string | null; actor: string; detail: string
  data: Record<string, unknown>; hash: string
}

export type Proposal = { id: string; tool: string; summary: string; status: "proposed" | "approved" | "rejected" | "sent" | "would_send" }
export type ActivityLine = { at: string; kind: "message" | "tool" | "note"; text: string; tool?: string }
export type StepDetail = {
  key: string; attempt: number; agent: string | null; model: string | null
  summary: string | null; route: { name: string; reason: string } | null; files: FileRef[]
  input: unknown; output: unknown
  /** JSON paths under `input` or `output` that came from an untrusted source, such as `input.lead.message`. */
  untrusted: string[]
  rejections: { attempt: number; errors: string[] }[]; proposals: Proposal[]
  activity: ActivityLine[]; plan: { text: string; status: "done" | "active" | "todo" }[]
}

export type AttentionKind = "failed" | "unknown" | "approval" | "question" | "needs_owner" | "needs_review"
export type AttentionItem = {
  kind: AttentionKind; workflow: { id: string; name: string }; run: { id: string; number: number } | null
  step: string | null; since: string; expires_at: string | null; detail: string
}

export type Template = { id: string; name: string; trigger: TriggerKind; kinds: Kind[] }
export type NewWorkflow = { from: "template"; template: string } | { from: "blank"; name: string } | { from: "import"; yaml: string }

export type Catalog = {
  agents: { key: string; id: string; name: string; tools: number }[]
  tools: { id: string; label: string; writes: boolean }[]
  /** `platforms` are where a person can be reached. */
  people: (Person & { platforms: Platform[] })[]
  /** `shared` channels include people from other organisations. */
  channels: { id: string; name: string; platform: Platform; shared: boolean }[]
}

/** The thread Ask Daimon talks in: the workflow's subject thread of the web chat. */
export type WorkflowChat = { session_id: string; agent: AgentRef }
export type Delivery = { id: string; received_at: string; inputs: Record<string, unknown> }


// ---------- Publish and inbox ----------

export type RequestStatus = "pending" | "approved" | "rejected"
export type PublishRequest = {
  id: string; workflow: { id: string; name: string }; version: number; note: string; by: Person; at: string; revision: number
  grants: Grant[]; status: RequestStatus; decided_by: Person | null; decided_at: string | null; comment: string | null
}
export type PublishBody = { note: string }

export type InboxKind = "approval" | "question" | "proposals" | "publish"
export type Decision = { route: string; label: string; by: Person; at: string; comment: string | null; selected: string[] | null }
export type InboxItem = {
  id: string; kind: InboxKind; title: string; workflow: { id: string; name: string }; run: { id: string; number: number } | null
  /** `step` is the run's step key; `step_title` its title, else the key. */
  step: string | null; step_title?: string | null; asked_by: string; since: string; expires_at: string | null; decision: Decision | null
}
export type InboxRoute = { name: string; label: string; primary: boolean; comment: boolean }
export type Bound = { tool: string; label: string; step: string; args: Record<string, unknown> }
export type InboxProposal = { id: string; tool: string; title: string; detail: string }
export type InboxDetail = InboxItem & {
  show: string | null; bound: Bound[]; proposals: InboxProposal[]; question: string | null
  form: ObjectSchema | null; values: Record<string, unknown>; routes: InboxRoute[]; publish: PublishRequest | null
  /** Takes a free reply besides its routes, sent as route `otherwise`. */
  reply?: boolean
  /** Where the ask went, what came with it, and the answer once given. */
  sent: Sent[]; attachments: FileRef[]; answer: Answer | null
}
export type InboxDecide = { route: string; comment?: string; selected?: string[]; values?: Record<string, unknown> }

// ---------- Triggers ----------

export type DeliveryOutcome = "accepted" | "skipped" | "rejected" | "duplicate"
/** A mapping evaluated against one delivery by the server. */
export type MappingPreview = { ok: true; value: unknown } | { ok: false; error: string }
export type DeliveryRow = {
  id: string; received_at: string; verified: boolean; outcome: DeliveryOutcome; status: number; run: { id: string; number: number } | null
  headers: Record<string, string>; payload: unknown
}
export type WebhookInfo = {
  live_url: string; test_url: string; secret_hint: string | null; secret_created_at: string | null; previous_expires_at: string | null
  listening_until: string | null
}
export type Secret = { secret: string; created_at: string; previous_expires_at: string | null }

export type VersionSpec = { version: Version; spec: Spec }
