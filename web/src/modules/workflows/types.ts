// The workflow spec (schema daimon.workflow/v1) and the shapes the workflows API returns.

export const KINDS = ["agent", "branch", "transform", "code", "action", "approval", "wait", "for_each", "workflow", "finish"] as const
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

export type Node = {
  kind: Kind; title?: string; description?: string
  next?: string | string[]; on_error?: string; retry?: { attempts: number }; timeout?: string | { after: string; route: string }
  budget_usd?: number; max_visits?: number; on_max_visits?: string; join?: "all" | "first"
  // agent
  agent?: string; model?: string; instructions?: string; inputs?: Record<string, string>
  /** Agent, code: fields. Finish: an expression. */
  output?: Record<string, Field> | string
  routes?: Record<string, string | Route>; tools?: "inherit" | string[]; writes?: "refuse" | "propose" | "allow"
  builtins?: "none" | "read" | "all"; session?: string; can_ask?: boolean; can_finish_early?: boolean; untrusted_waiver?: string
  // branch
  cases?: Case[]; else?: string
  // transform
  set?: Record<string, string>
  // code
  language?: "python"; source?: string; network?: "off" | "on"
  // action
  tool?: string; args?: Record<string, string>; idempotent?: boolean
  // approval
  approvers?: string[]; min_approvals?: number; show?: string; form?: Record<string, Field>; binds?: string | string[]; remind_every?: string
  // wait
  for?: string; until?: string; event?: { source: string; match?: string }
  // for_each
  items?: string; as?: string; do?: Node; concurrency?: number; tolerate_failures?: number
  // workflow
  workflow?: string; version?: "live" | number
  // finish
  outcome?: "succeeded" | "failed"; deliver?: { to: string; message?: string } | null
}

export type TriggerKind = "manual" | "schedule" | "webhook" | "event" | "call"
export type Trigger = {
  id: string; kind: TriggerKind
  cron?: string; timezone?: string; catch_up?: "skip" | "run-once"
  verify?: string; inputs?: string; respond?: "accepted" | "result"
  source?: string; match?: string
}

export type Spec = {
  schema: "daimon.workflow/v1"; name: string; goal?: string; home?: { channel: string }; visibility?: "private" | "shared"; label?: string
  inputs?: ObjectSchema; triggers?: Trigger[]
  concurrency?: { key?: string; limit?: number; on_conflict?: "queue" | "skip" | "replace" | "debounce"; debounce?: string }
  defaults?: { agent?: string; budget_usd?: number }; limits?: { deadline?: string; max_steps?: number; budget_usd?: number }
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
}
export type WorkflowPatch = Partial<Pick<Workflow, "name" | "visibility">> & { paused?: boolean }

export type Severity = "error" | "warning"
export type Problem = { severity: Severity; code: string; message: string; node?: string }
export type GrantKind = "unattended_write" | "builtins" | "code_network" | "unverified_webhook" | "budget_over_default" | "untrusted_waiver"
export type Grant = { kind: GrantKind; node?: string; detail: string }
/** Rule of Two: Untrusted input, Sensitive data, Writes. */
export type RuleClass = "U" | "S" | "W"
export type Validation = { errors: Problem[]; warnings: Problem[]; grants: Grant[]; classes: Record<string, RuleClass[]> }

export type Change = { node: string | null; field?: string; effect: "added" | "changed" | "removed" }
export type Draft = {
  revision: number; base_version: number | null; spec: Spec; changes: Change[]; validation: Validation
  updated_at: string; updated_by: Person
}
export type DraftPut = { base_revision: number; spec: Spec }

export type Version = { number: number; published_by: Person; published_at: string; note: string; grants: GrantKind[]; runs: number; live: boolean }

export type RunSummary = {
  id: string; number: number; status: RunStatus; label: string | null; trigger: { kind: TriggerKind; id: string }
  started_at: string; duration_ms: number | null; current_step: string | null; cost_usd: number; version: number; test: boolean
}
/** Milestone B: the run page. Stubbed by the mock. */
export type StepRun = {
  node: string; visit: number; status: StepStatus; started_at: string | null; duration_ms: number | null; cost_usd: number
  route: string | null; summary: string | null; output: unknown
}
export type RunDetail = RunSummary & { inputs: Record<string, unknown>; steps: StepRun[]; triggered_by: Person | null }
export type RunEvent = { seq: number; at: string; type: string; node?: string; data?: Record<string, unknown> }

export type Template = { id: string; name: string; trigger: TriggerKind; kinds: Kind[] }
export type NewWorkflow =
  | { from: "describe"; text: string } | { from: "template"; template: string } | { from: "blank"; name: string } | { from: "import"; yaml: string }

export type Catalog = {
  agents: { key: string; id: string; name: string; tools: number }[]
  tools: { id: string; label: string; writes: boolean }[]
  people: Person[]; channels: { id: string; name: string }[]
}

export type AskMessage = { role: "user" | "assistant"; text: string }
export type AskReply = { reply: string; draft: Draft; changed: string[] }
