// Shapes of the evals documents as the server ships them (`daimon.eval.*/1`), limited to what the web app reads.

export type Platform = "discord" | "slack" | "teams" | "web"
export type ShareRole = "viewer" | "reviewer" | "editor"
export type SuiteRole = "owner" | ShareRole | "admin"
export type ScrubStatus = "scrubbed" | "partial" | "unscrubbed"
export type Origin = "recorded" | "overlay" | "simulated" | "denied" | "passthrough" | "sandbox" | "live"
export type CardKind = "confirmation" | "posted" | "credential_request" | "host_call" | "provider_action" | "refusal" | "status" | "panel" | "form" | "question"
export type ButtonAction = "approve" | "deny" | "always" | "details" | "save" | "skip" | "submit" | "stop" | "open_url" | "custom"

export type BlobRef = { sha256: string; size: number; media_type: string; name?: string }

export type SuiteSummary = { id: string; name: string; version: number; role: SuiteRole; scrub_status: ScrubStatus }
export type ModelChoice = { backend?: string; model: string; effort?: string }
export type Judge = { id: string; kind: "rubric" | "pairwise" | "reference"; models: ModelChoice[]; aggregate?: "majority" | "mean" | "all" }
export type Suite = {
  id: string; version: number; name: string; description?: string; tags?: string[]
  owner?: { kind: string; id: string; display_name?: string }
  cases: { id: string; revision: number; hash: string; weight: number; split: "dev" | "test" | "holdout" }[]
  /** Who the suite is shared with; the server adds it for people who may share. */
  shares?: Share[]
  defaults?: { repeats?: number; judges?: Judge[]; review?: { layout: "single" | "side_by_side"; blind: boolean } }
}
/** The server lists id, revision, hash, title and fidelity; the rest fills in when it ships them. */
export type CaseSummary = {
  id: string; revision: number; hash: string; title: string; fidelity: string
  platform?: Platform; tags?: string[]; flags?: string[]; last_result?: "pass" | "fail" | "error" | null
}
export type Share = { kind: "person" | "group"; id: string; role: ShareRole }
export type CaseCheck = { problems: string[]; warnings: string[] }
export type ImportReport = { bundle_id: string; suites: { bundle_id: string; id: string; version: number; created: boolean }[]; runs: string[]; skipped_runs: string[]; blobs: number; signature: string; warnings: string[] }

export type Participant = { id: string; display_name: string; role?: string; is_requester?: boolean; is_bot?: boolean }
export type CardFact = { label: string; value: string }
export type CardField = { id: string; label: string; kind: "text" | "textarea" | "select" | "secret" | "checkbox"; options?: string[] }
export type CardButton = { id: string; label: string; style: "primary" | "secondary" | "danger" | "link"; action: ButtonAction }
export type NeutralCard = {
  id: string; kind: CardKind; headline?: string; body?: string; facts?: CardFact[]; fields?: CardField[]; buttons?: CardButton[]; footer?: string
  state: "open" | "answered" | "expired" | "cancelled"; answer?: { by?: string; button?: string; fields?: Record<string, string> }
}
export type Attachment = { id: string; name: string; media_type: string; size?: number; blob?: BlobRef; state?: string }
export type ChatMessage = {
  id: string; author: string; at: string; text?: string; format?: "markdown" | "plain"; reply_to?: string
  attachments?: Attachment[]; reactions?: { emoji: string; by: string[] }[]; cards?: NeutralCard[]; deleted?: boolean; edited_at?: string; kind?: "message" | "system" | "notice"
}
export type ReviewFlag = { reason: string; detail?: string; cleared_at?: string }
export type Check = { op: string; value?: unknown; values?: unknown[]; pattern?: string; min?: number; max?: number; criterion?: string }
export type CallPattern = { effect?: string; tool?: string; server?: string; service?: string; surface?: string }
export type Exchange = {
  id: string; surface: string; service?: string; server?: string; tool?: string; kind: "read" | "write"; origin: "recorded" | "derived" | "authored"
  request: unknown; response: unknown; effects?: { verb: string; target?: string }[]
}
export type Case = {
  id: string; revision: number; hash?: string; title: string; tags?: string[]; source_thread?: string
  fidelity: { level: "full" | "tools" | "text" | "authored"; missing?: string[]; note?: string }
  provenance: {
    kind: "recorded" | "authored" | "derived" | "imported"; source_system?: string; recorded_at?: string
    scrub?: { status: ScrubStatus; policy?: string; entities?: number; human_checked?: boolean }
    expectations_by?: { kind: string; model?: string; at: string; action?: string }[]
  }
  surface: { platform: Platform; conversation: string; participants: Participant[]; bot: Participant; channel?: { name: string } }
  clock: { now: string; timezone?: string }
  history: ChatMessage[]
  turn: { messages: ChatMessage[]; trigger: string }
  script?: { steps?: { id: string; on: { event: string; card_kind?: CardKind; button_action?: ButtonAction; text_contains?: string }; do: { action: string; text?: string; emoji?: string; button?: { action?: ButtonAction } }[]; once: boolean; delay_ms: number }[]; defaults?: Record<string, string> }
  world: {
    exchanges?: Exchange[]; resources?: { service: string; type: string; key: string }[]; files?: { path: string; state: string }[]
    repos?: { url: string; default_branch: string }[]; web?: { url: string; status: number; title?: string }[]; identities?: { service: string; login: string }[]
  }
  expect: {
    tool_calls?: { mode: string; calls: { match: CallPattern; min: number; max?: number; hard: boolean }[] }
    forbidden_calls?: CallPattern[]
    effects?: { mode: string; items: { verb: string; target?: string; min: number; max?: number; hard: boolean }[] }
    forbidden_effects?: string[]
    outputs?: { kind: string; where: string; text?: Check; card_kind?: CardKind; min: number; hard: boolean }[]
    rubric?: { id: string; criterion: string; kind: "binary" | "scale"; weight: number; hard: boolean }[]
    reference?: { text?: string; note?: string }
    limits?: { max_tool_calls?: number; max_duration_s?: number; max_cost_usd?: number; max_messages?: number }
  }
  review_flags?: ReviewFlag[]
  notes?: string
}

export type Arm = {
  id: string; label: string; kind: "live" | "baseline"
  live?: { agent?: string; backend: string; model: string; effort?: string }
  baseline?: { source_system?: string; recorded_from?: string; recorded_to?: string }
}
export type EstimateLine = { category: "agent" | "simulator" | "judge" | "compile" | "sandbox"; arm_id?: string; model?: string; items?: number; low_usd?: number; expected_usd: number; high_usd?: number; basis?: string }
export type Estimate = { low_usd: number; expected_usd: number; high_usd: number; at: string; lines: EstimateLine[] }
export type RunStatus = "draft" | "estimated" | "approved" | "running" | "done" | "partial" | "cancelled" | "failed"
export type Run = {
  id: string; name?: string; suite: { id: string; version: number }; arms: Arm[]; case_ids?: string[]; repeats: number
  judges?: Judge[]; status: RunStatus; origin: { kind: "local" | "imported" | "recorded"; deployment?: string }
  estimate?: Estimate; approval?: { by: { display_name?: string }; at: string; cap_usd: number }
  spend?: { total_usd?: number; by_category?: Record<string, number> }; started_at?: string; finished_at?: string
  /** A run awaiting an admin carries the cap its creator asked for as `requested_cap_usd`. */
  ext?: { requested_cap_usd?: number }
}
/** What a reviewer gets for a run: arms only by neutral label, no name, cost or approver. */
export type BlindRun = { id: string; suite: { id: string; version: number }; arms: string[]; case_ids: string[] | null; repeats: number; status: RunStatus; origin: string; started_at?: string; finished_at?: string }
export type RunDraft = { suite: { id: string; version: number }; case_ids?: string[]; arm_ids: string[]; repeats: number; judge_ids: string[] }
/** The estimate of a draft run, with the spend above which an admin must approve. */
export type RunEstimate = Estimate & { admin_approval_usd: number }
export type JudgeRequest = { id: string; kind: Judge["kind"]; models?: ModelChoice[]; aggregate: "majority" | "mean" | "all"; batch: boolean; approve_cap_usd?: number }
export type JudgeOutcome = { estimate: Estimate; ran: boolean; scores: number; spent_usd: number; skipped: number; failed: number }
export type ItemStatus = "pending" | "running" | "done" | "error" | "skipped" | "cancelled"
export type RunItemSummary = { id: string; key: string; case_id: string; case_revision: number; arm_id: string; repeat: number; status: ItemStatus }

export type Anchor = {
  target: "message" | "tool_call" | "world_call" | "output" | "page" | "image_region" | "table_range" | "text_range" | "card_element" | "diff_hunk"
  item_id: string; ref?: string; page?: number; region?: { x: number; y: number; w: number; h: number }; cells?: string; sheet?: string; start?: number; end?: number; quote?: string; element?: string
}
export type ScoreValue = { pass?: boolean; score?: number; preference?: "a" | "b" | "tie"; label?: string; unknown?: boolean }
export type ChatEvent = {
  seq: number; t_ms?: number
  kind: "message" | "edit" | "delete" | "reaction_add" | "reaction_remove" | "card" | "card_update" | "typing" | "status" | "title" | "notice" | "file"
  actor: string; message_id?: string; text?: string; format?: "markdown" | "plain"; reply_to?: string; emoji?: string; card?: NeutralCard; files?: string[]; scripted?: string
}
export type WorldCall = {
  seq: number; t_ms?: number; surface: string; service?: string; server?: string; tool?: string; origin: Origin
  request?: unknown; response?: unknown; effects?: { verb: string; target?: string }[]; reason?: string
}
export type Output = {
  id: string; name: string; media_type: string; blob: BlobRef; renders?: BlobRef[]; text?: BlobRef; path?: string
}
export type TranscriptEvent = { seq: number; kind: string; text?: string; tool?: { name: string; server?: string; input?: unknown }; result?: { origin?: Origin; is_error?: boolean } }
export type RunItem = {
  id: string; run_id: string; case: { id: string; revision: number }; arm_id: string; repeat: number; status: ItemStatus
  error?: { kind: string; message?: string }; chat?: ChatEvent[]; outputs?: Output[]; world_calls?: WorldCall[]
  transcript?: { events?: TranscriptEvent[] }
  usage?: { cost_usd?: number; duration_ms?: number; tool_calls?: number }
  enclosure?: { mode: "enclosed" | "recorded"; simulated_calls?: number; denied_calls?: number }
}

export type Rate = { items: number; passed: number; pass_rate: number | null }
export type ArmSummary = {
  arm_id: string; label: string; items: number; scored: number; errors: number; passed: number; pass_rate: number | null; rubric_score: number | null
  cost_usd: number; pass_at_k: Record<string, number>; pass_pow_k: Record<string, number>; breakdown: Record<string, Record<string, Rate>>
}
export type PairSummary = {
  candidate: string; reference: string; metric: string; cases: number; clusters: number; difference: number | null; low: number | null; high: number | null
  margin: number; non_inferior: boolean | null; only_candidate: number; only_reference: number; mcnemar_p: number | null
}
export type PreferenceSummary = { grader: string; arm_a: string; arm_b: string; a_wins: number; b_wins: number; ties: number; sign_test_p: number | null }
export type Calibration = { judge: string; n: number; agreement: number | null; kappa: number | null; flagged: boolean }
export type RunSummary = {
  run_id: string; arms: ArmSummary[]; comparisons: PairSummary[]; preferences: PreferenceSummary[]; calibration: Calibration[]; judge_cost_usd: number; notes: string[]
}

/** One answer's score as the comparison lists it; empty when blind. */
export type ScoreLine = { criterion: string; grader: string; passed: boolean | null; score: number | null; unknown: boolean; rationale: string | null }
/** One arm's result on a case. `label` is the arm's label, or a neutral one when blind; `item_id` opens `/items/{id}`. */
export type CompareColumn = { label: string; item_id: string; status: ItemStatus; passed: boolean | null; rubric: number | null; reply: string | null; scores: ScoreLine[] }
export type CompareRow = { case_id: string; title: string; repeat: number; columns: CompareColumn[] }
export type Compare = { run_id: string; blind: boolean; rows: CompareRow[] }

export type Layout = "single" | "side_by_side"
export type ReviewQueue = { id: string; name: string; layout: Layout; blind: boolean; arms?: string[]; criteria?: string[]; reviewers_per_item?: number; status?: "open" | "closed" }
/** A queue as its reviewers list it; `ref` is what `/queues/{ref}/next` takes. */
export type QueueView = { ref: string; run_id: string; queue: ReviewQueue; total: number; done: number }
export type QueueDraft = { id: string; name: string; layout: Layout; blind: boolean }
export type Criterion = { id: string; question: string; kind: "binary" | "scale" | "overall" | "preference"; scale?: { min: number; max: number } }
/** The part of a case a reviewer sees. */
export type CaseView = { id: string; title: string; platform: Platform; history: ChatMessage[]; turn: ChatMessage[]; reference?: { text?: string; note?: string } | null }
/** One column of a review task: `label` names it in scores and comments; `arm` is absent in a blind queue. */
export type TaskColumn = { label: string; arm?: Arm | null; item: RunItem }
export type Comment = { id: string; item_id?: string; case_id?: string; parent_id?: string; author: { display_name?: string; kind?: string }; text: string; anchors?: Anchor[]; resolved: boolean; at: string }
export type ReviewTask = {
  id: string; queue: string; layout: Layout; blind: boolean; case: CaseView; criteria: Criterion[]; columns: TaskColumn[]; comments: Comment[]
  reserved_until: string | null; done: number; total: number
}
/** `target` is the column label a per-answer score is about; omitted for a preference or a single answer. */
export type ScoreDraft = { criterion: string; value: ScoreValue; target?: string; rationale?: string }

/** Starts a comment anchored to something the reviewer pointed at; the item is filled in by the caller. */
export type Pin = (a: Omit<Anchor, "item_id">) => void
