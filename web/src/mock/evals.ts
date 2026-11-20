// Evals: suites, cases, runs, comparison and review. The future server contract for the evals module; generic invented data.
import { http, HttpResponse, type HttpHandler } from "msw"

import type {
  Arm, ArmSummary, Case, CaseSummary, ChatEvent, ChatMessage, Column, Comment, Compare, Estimate, ItemStatus, Output, Platform, Queue, ReviewTask, Run, RunItem,
  RunItemSummary, RunSummary, Score, Share, Suite, SuiteSummary,
} from "@/modules/evals/types"
import { forbidden, notFound, unauthorized, wait } from "./util"
import { me } from "./world"

const NOW = Date.now()
const iso = (offsetMin: number) => new Date(NOW + offsetMin * 60_000).toISOString()
const guard = () => { const m = me(); return !m ? unauthorized() : m.capabilities.includes("evals.use" as never) ? null : forbidden() }

// ---------- arms ----------
const ARMS: Arm[] = [
  { id: "baseline-a", label: "Baseline A", kind: "baseline", baseline: { source_system: "baseline-a", recorded_from: iso(-60 * 24 * 40), recorded_to: iso(-60 * 24 * 12) } },
  { id: "replica", label: "Replica", kind: "live", live: { backend: "harness", model: "model-large", effort: "high" } },
  { id: "replica-fast", label: "Replica, fast", kind: "live", live: { backend: "harness", model: "model-small", effort: "low" } },
]

// ---------- cases ----------
type Spec = { id: string; title: string; platform: Platform; fidelity: Case["fidelity"]["level"]; tags: string[]; flags?: string[]; ask: string; kind: "md" | "pdf" | "img" | "csv" | "slides" | "json" | "text" }
const SPECS: Spec[] = [
  { id: "c-spend", title: "Summarize last week's channel spend", platform: "discord", fidelity: "full", tags: ["reporting"], ask: "Can you summarize last week's channel spend?", kind: "md" },
  { id: "c-launch", title: "Draft a launch announcement", platform: "slack", fidelity: "tools", tags: ["writing"], ask: "Draft a short launch announcement for the new dashboard.", kind: "md" },
  { id: "c-issue", title: "Open an issue for the failing build", platform: "discord", fidelity: "full", tags: ["engineering"], flags: ["needs_human_check"], ask: "The nightly build fails again. Open an issue for it.", kind: "text" },
  { id: "c-pricing", title: "Compare two pricing options", platform: "teams", fidelity: "text", tags: ["analysis"], ask: "Compare the two pricing options and send me the table.", kind: "csv" },
  { id: "c-deck", title: "Prepare the quarterly review deck", platform: "teams", fidelity: "tools", tags: ["writing", "slides"], flags: ["file_rebuilt"], ask: "Prepare the quarterly review deck.", kind: "slides" },
  { id: "c-actions", title: "Extract action items from the thread", platform: "slack", fidelity: "full", tags: ["writing"], ask: "Pull the action items out of this thread.", kind: "json" },
  { id: "c-report", title: "Create the monthly report", platform: "web", fidelity: "authored", tags: ["reporting"], ask: "Create the monthly report as a PDF.", kind: "pdf" },
  { id: "c-chart", title: "Chart the signup trend", platform: "web", fidelity: "tools", tags: ["analysis"], ask: "Chart the signup trend for the last quarter.", kind: "img" },
]
const SUITE_CASES: Record<string, Spec[]> = {
  "weekly-reports": SPECS,
  "support-triage": SPECS.slice(0, 3).map((s) => ({ ...s, id: `s-${s.id}`, tags: ["support"] })),
  "pricing-questions": SPECS.slice(3, 6).map((s) => ({ ...s, id: `p-${s.id}` })),
}
const allSpecs = () => Object.values(SUITE_CASES).flat()
const specOf = (id: string) => allSpecs().find((s) => s.id === id)

const person = (id: string, name: string, over = {}) => ({ id, display_name: name, role: "member", ...over })
const msg = (id: string, author: string, text: string, min: number, over: Partial<ChatMessage> = {}): ChatMessage => ({ id, author, text, at: iso(min), ...over })

const edits: Record<string, Case> = {}
function buildCase(s: Spec): Case {
  if (edits[s.id]) return edits[s.id]!
  const write = s.tags.includes("engineering")
  return {
    id: s.id, revision: 2, hash: `h-${s.id}`, title: s.title, tags: s.tags, source_thread: `thread-${s.id.slice(-4)}`,
    fidelity: { level: s.fidelity, missing: s.fidelity === "full" ? undefined : ["card history"], note: s.fidelity === "authored" ? "Written by hand" : undefined },
    provenance: {
      kind: s.fidelity === "authored" ? "authored" : "recorded", source_system: s.fidelity === "authored" ? undefined : "baseline-a", recorded_at: iso(-60 * 24 * 30),
      scrub: { status: s.flags?.length ? "partial" : "scrubbed", policy: "default", entities: 14, human_checked: !s.flags?.length },
      expectations_by: [{ kind: "model", model: "model-large", at: iso(-60 * 24 * 20), action: "drafted" }, { kind: "person", at: iso(-60 * 24 * 18), action: "checked" }],
    },
    surface: { platform: s.platform, conversation: s.platform === "web" ? "dm" : "channel", channel: { name: s.platform === "web" ? "Chat" : "team-updates" }, participants: [person("u1", "Sam Reed", { is_requester: true }), person("u2", "Riley Chen")], bot: person("bot", "Assistant", { is_bot: true }) },
    clock: { now: iso(-60 * 24 * 30), timezone: "UTC" },
    history: [
      msg("h1", "u2", "Morning. Before the review: do we have the latest numbers in the shared folder?", -60 * 24 * 30 - 20),
      msg("h2", "u1", "Yes, uploaded Friday. I will ask the assistant to pull them together.", -60 * 24 * 30 - 15, { reactions: [{ emoji: "👍", by: ["u2"] }] }),
    ],
    turn: { trigger: "mention", messages: [msg("m_turn", "u1", s.ask, -60 * 24 * 30 - 5)] },
    script: write ? { steps: [{ id: "st1", on: { event: "card", card_kind: "confirmation" }, do: [{ action: "click", button: { action: "approve" } }], once: true, delay_ms: 800 }], defaults: { confirmation: "approve", credential_request: "skip", question: "end" } } : { defaults: { confirmation: "approve", credential_request: "skip", question: "end" } },
    world: {
      exchanges: [
        { id: "x1", surface: "mcp", service: "files", tool: "list_files", kind: "read", origin: "recorded", request: { args: { folder: "shared" } }, response: { result: { files: ["numbers.csv", "notes.md"] } } },
        { id: "x2", surface: "mcp", service: "files", tool: "read_file", kind: "read", origin: "recorded", request: { args: { path: "numbers.csv" } }, response: { result: { rows: 120 } } },
        ...(write ? [{ id: "x3", surface: "http", service: "tracker", tool: "issues.create", kind: "write" as const, origin: "recorded" as const, request: { http: { method: "POST", url: "https://tracker.example/api/issues" } }, response: { http: { status: 201 } }, effects: [{ verb: "tracker.issue.create" }] }] : []),
      ],
      resources: [{ service: "files", type: "folder", key: "shared" }], files: [{ path: "/work/numbers.csv", state: "present" }], identities: [{ service: "tracker", login: "assistant-bot" }],
    },
    expect: {
      tool_calls: { mode: "subset", calls: [{ match: { service: "files", tool: "read_file" }, min: 1, hard: true }] },
      forbidden_effects: ["tracker.issue.delete"],
      effects: write ? { mode: "superset", items: [{ verb: "tracker.issue.create", min: 1, max: 1, hard: true }] } : undefined,
      outputs: [{ kind: "reply", where: "final", text: { op: "contains", value: "summary" }, min: 1, hard: true }],
      rubric: [{ id: "r1", criterion: "Answers the question that was asked", kind: "binary", weight: 1, hard: true }, { id: "r2", criterion: "Numbers match the source file", kind: "binary", weight: 1, hard: false }, { id: "r3", criterion: "Tone fits the channel", kind: "scale", weight: 0.5, hard: false }],
      reference: { text: "A short summary with the three main changes and one recommendation." },
      limits: { max_tool_calls: 12, max_duration_s: 300, max_cost_usd: 0.5 },
    },
    review_flags: s.flags?.map((f) => ({ reason: f, detail: f === "needs_human_check" ? "One screenshot may show a person" : undefined })),
  }
}
const summaryOf = (s: Spec): CaseSummary => {
  const c = buildCase(s)
  return { id: s.id, revision: c.revision, hash: c.hash!, title: c.title, fidelity: c.fidelity.level, platform: s.platform, tags: c.tags, flags: c.review_flags?.filter((f) => !f.cleared_at).map((f) => f.reason), last_result: s.id === "c-issue" ? "fail" : s.id === "c-chart" ? null : "pass" }
}

// ---------- suites ----------
const JUDGES = [{ id: "rubric-1", kind: "rubric" as const, models: [{ model: "judge-a" }, { model: "judge-b" }] }, { id: "pairwise-1", kind: "pairwise" as const, models: [{ model: "judge-a" }] }]
const suites: (SuiteSummary & { description?: string; shares: Share[] })[] = [
  { id: "weekly-reports", name: "Weekly reporting", version: 3, role: "owner", scrub_status: "scrubbed", description: "Everyday reporting and writing requests", shares: [{ kind: "person", id: "riley", role: "reviewer" }, { kind: "group", id: "analysts", role: "viewer" }] },
  { id: "support-triage", name: "Support triage", version: 1, role: "reviewer", scrub_status: "partial", shares: [] },
  { id: "pricing-questions", name: "Pricing questions", version: 2, role: "owner", scrub_status: "unscrubbed", shares: [] },
]
const suiteDoc = (id: string): Suite | undefined => {
  const s = suites.find((x) => x.id === id)
  return s && { id, version: s.version, name: s.name, description: s.description, owner: { kind: "person", id: "sam", display_name: "Sam Reed" }, shares: s.role === "owner" ? s.shares : undefined,
    cases: (SUITE_CASES[id] ?? []).map((c, i) => ({ id: c.id, revision: 2, hash: `h-${c.id}`, weight: 1, split: i % 4 === 3 ? "dev" : "test" })), defaults: { repeats: 1, judges: JUDGES, review: { layout: "side_by_side", blind: true } } }
}

// ---------- blobs ----------
const wrap = (t: string, n = 70) => t.replace(new RegExp(`(.{1,${n}})(\\s|$)`, "g"), "$1\n").trim()
function pdf(pages: string[]) {
  // Objects: 1 catalog, 2 pages, 3 font, then a content stream (4+2i) and a page (5+2i) per page.
  const objs = ["<< /Type /Catalog /Pages 2 0 R >>", `<< /Type /Pages /Kids [${pages.map((_, i) => `${5 + i * 2} 0 R`).join(" ")}] /Count ${pages.length} >>`, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
  pages.forEach((t, i) => {
    const body = `BT /F1 14 Tf 56 740 Td 18 TL ${wrap(t, 60).split("\n").map((l) => `(${l.replace(/[()\\]/g, "")}) Tj T*`).join(" ")} ET`
    objs.push(`<< /Length ${body.length} >>\nstream\n${body}\nendstream`, `<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents ${4 + i * 2} 0 R /Resources << /Font << /F1 3 0 R >> >> >>`)
  })
  let out = "%PDF-1.4\n"
  const at: number[] = []
  objs.forEach((o, i) => { at.push(out.length); out += `${i + 1} 0 obj\n${o}\nendobj\n` })
  const x = out.length
  out += `xref\n0 ${objs.length + 1}\n0000000000 65535 f \n${at.map((n) => `${String(n).padStart(10, "0")} 00000 n \n`).join("")}trailer\n<< /Size ${objs.length + 1} /Root 1 0 R >>\nstartxref\n${x}\n%%EOF`
  return out
}
const svg = (variant: string, title: string, n = 6) => {
  const hue = variant === "replica" ? ["#0c9e82", "#2f6db5"] : variant === "replica-fast" ? ["#c2456b", "#2f6db5"] : ["#2f6db5", "#d0731d"]
  const bars = Array.from({ length: n }, (_, i) => { const h = 60 + ((i * 53 + (variant.length * 17)) % 150); return `<rect x="${60 + i * 80}" y="${300 - h}" width="50" height="${h}" fill="${hue[i % 2]}"/><text x="${85 + i * 80}" y="320" font-size="12" text-anchor="middle" fill="#55627a">W${i + 1}</text>` }).join("")
  return `<svg xmlns="http://www.w3.org/2000/svg" width="640" height="360" viewBox="0 0 640 360"><rect width="640" height="360" fill="#ffffff"/><text x="60" y="40" font-size="18" font-family="sans-serif" fill="#0c1f40">${title}</text><line x1="50" y1="300" x2="600" y2="300" stroke="#ccd3dd"/>${bars}</svg>`
}
const REPLY = (variant: string, s: Spec) => variant === "baseline-a"
  ? `Here is the summary for **${s.title.toLowerCase()}**.\n\n- Search spend rose 4% week over week\n- Social held flat\n- Video dropped 9%\n\nRecommendation: shift a small share of video budget to search.`
  : `Here is the summary for **${s.title.toLowerCase()}**.\n\n- Search spend rose 4% week over week\n- Social held flat\n- Video dropped 11%, the largest move\n\nRecommendation: move part of the video budget to search and review again on Friday.`
const SHEET = (variant: string) => [["Option", "Monthly price", "Seats", "Support"], ["Starter", "29", "5", "Email"], ["Team", variant === "baseline-a" ? "79" : "75", "25", "Chat"], ["Business", "199", "100", variant === "replica-fast" ? "Chat" : "Phone"]]
const toCsv = (rows: string[][]) => rows.map((r) => r.join(",")).join("\n")

function blob(sha: string) {
  const [, kind, variant, c] = sha.split(":") as [string, string, string, string]
  const s = specOf(c) ?? SPECS[0]!
  switch (kind) {
    case "md": return { type: "text/markdown", body: REPLY(variant, s) }
    case "csv": return { type: "text/csv", body: toCsv(SHEET(variant)) }
    case "json": return { type: "application/json", body: JSON.stringify({ items: [{ owner: "Sam", task: "Send the numbers", due: "Friday" }, { owner: "Riley", task: variant === "baseline-a" ? "Book the review room" : "Book the room for Monday", due: "Thursday" }], count: 2 }, null, 2) }
    case "pdf": return { type: "application/pdf", body: pdf([REPLY(variant, s).replace(/\*\*/g, ""), `Appendix. ${variant === "baseline-a" ? "Figures are rounded." : "Figures are rounded to one decimal."} Source: shared folder, numbers file.`]) }
    case "pdftext": return { type: "text/plain", body: `${REPLY(variant, s).replace(/\*\*/g, "")}\nAppendix. ${variant === "baseline-a" ? "Figures are rounded." : "Figures are rounded to one decimal."}` }
    case "img": return { type: "image/svg+xml", body: svg(variant, s.title) }
    case "slide": return { type: "image/svg+xml", body: svg(variant, `${s.title}, slide ${variant.split("-").pop()}`, 4) }
    case "patch": return { type: "text/x-diff", body: "diff --git a/build.sh b/build.sh\n--- a/build.sh\n+++ b/build.sh\n@@ -1,4 +1,5 @@\n #!/bin/sh\n-make all\n+make clean\n+make all\n echo done\n" }
    default: return { type: "text/plain", body: `Issue opened: build fails on the nightly run.\nOwner: unassigned\nLabels: bug, build\n${variant === "baseline-a" ? "" : "Linked to the last failing commit.\n"}` }
  }
}
const ref = (kind: string, variant: string, c: string, name: string, media: string) => ({ sha256: `b:${kind}:${variant}:${c}`, size: 2048, media_type: media, name })

// ---------- runs and items ----------
const MS_PER_ITEM = 1800
type Sim = { id: string; created: number; cancelled?: boolean; fixed?: Run["status"]; cap?: number }
const runs: (Run & { sim: Sim })[] = []
const mkRun = (id: string, name: string, suite: string, arms: string[], over: Partial<Run> & { sim: Sim }): Run & { sim: Sim } => ({
  id, name, suite: { id: suite, version: suiteDoc(suite)!.version }, arms: arms.map((a) => ARMS.find((x) => x.id === a)!), repeats: 1, judges: JUDGES, origin: { kind: "local" }, status: "done", ...over,
})
const est = (arms: number, cases: number, repeats: number): Estimate => {
  const n = arms * cases * repeats, a = n * 0.11, j = n * 0.035, sim = n * 0.02
  return { low_usd: +(0.6 * (a + j + sim)).toFixed(2), expected_usd: +(a + j + sim).toFixed(2), high_usd: +(1.9 * (a + j + sim)).toFixed(2), at: iso(0),
    lines: [{ category: "agent", items: n, expected_usd: +a.toFixed(2), basis: "prior_runs" }, { category: "simulator", items: n, expected_usd: +sim.toFixed(2), basis: "suite_median" }, { category: "judge", items: n, expected_usd: +j.toFixed(2), basis: "default" }, { category: "sandbox", items: n, expected_usd: +(n * 0.01).toFixed(2) }] }
}
runs.push(
  mkRun("run-1", "Replica against baseline", "weekly-reports", ["baseline-a", "replica"], { started_at: iso(-300), finished_at: iso(-270), spend: { total_usd: 2.31, by_category: { agent: 1.4, judge: 0.7, simulator: 0.21 } }, approval: { by: { display_name: "Sam Reed" }, at: iso(-305), cap_usd: 5 }, estimate: est(2, 8, 1), sim: { id: "run-1", created: NOW - 300 * 60_000, fixed: "done" } }),
  mkRun("run-2", "Fast variant check", "weekly-reports", ["replica", "replica-fast"], { started_at: iso(-1), spend: { total_usd: 0.44 }, approval: { by: { display_name: "Sam Reed" }, at: iso(-2), cap_usd: 3 }, estimate: est(2, 8, 1), status: "running", sim: { id: "run-2", created: NOW - 40_000, cap: 3 } }),
  mkRun("run-3", "Pricing replay", "pricing-questions", ["baseline-a", "replica"], { status: "estimated", estimate: est(2, 3, 1), sim: { id: "run-3", created: NOW, fixed: "estimated" } }),
)
const runOf = (id: string) => runs.find((r) => r.id === id)
const caseIds = (r: Run) => r.case_ids ?? (SUITE_CASES[r.suite.id] ?? []).map((c) => c.id)
const itemList = (r: Run & { sim: Sim }): RunItemSummary[] => {
  const ids = caseIds(r)
  const done = r.sim.fixed ? Infinity : Math.floor((Date.now() - r.sim.created) / MS_PER_ITEM)
  const list: RunItemSummary[] = []
  ids.forEach((c) => r.arms.forEach((a) => { for (let k = 0; k < r.repeats; k++) list.push({ id: `${r.id}:${c}:${a.id}:${k}`, key: `${c}:${a.id}:${k}`, case_id: c, case_revision: 2, arm_id: a.id, repeat: k, status: "pending" }) }))
  return list.map((it, i): RunItemSummary => {
    const status: ItemStatus = r.status === "estimated" ? "pending" : r.sim.cancelled && i >= done ? "cancelled" : i < done ? (it.case_id === "c-chart" && it.arm_id === "replica-fast" ? "error" : "done") : i === done ? "running" : "pending"
    return { ...it, status }
  })
}
const statusOf = (r: Run & { sim: Sim }): Run["status"] => {
  if (r.sim.fixed) return r.sim.fixed
  if (r.sim.cancelled) return "cancelled"
  const items = itemList(r)
  return items.every((i) => i.status === "done" || i.status === "error") ? "done" : "running"
}
const view = (r: Run & { sim: Sim }): Run => {
  const { sim, ...rest } = r
  const status = statusOf(r)
  const items = itemList(r)
  const finished = items.filter((i) => i.status === "done" || i.status === "error").length
  return { ...rest, status, spend: sim.fixed ? r.spend : { total_usd: +(finished * 0.09).toFixed(2) }, finished_at: status === "done" ? r.finished_at ?? iso(0) : undefined }
}

function buildItem(r: Run, caseId: string, armId: string, repeat: number): RunItem {
  const s = specOf(caseId) ?? SPECS[0]!
  const slug = armId
  const out: Output[] = []
  const add = (kind: string, name: string, media: string, extra: Partial<Output> = {}) => out.push({ id: `o-${kind}`, name, media_type: media, blob: ref(kind, slug, caseId, name, media), ...extra })
  if (s.kind === "md") add("md", "summary.md", "text/markdown")
  if (s.kind === "csv") add("csv", "options.csv", "text/csv")
  if (s.kind === "json") add("json", "actions.json", "application/json")
  if (s.kind === "pdf") add("pdf", "report.pdf", "application/pdf", { text: ref("pdftext", slug, caseId, "report.txt", "text/plain") })
  if (s.kind === "img") add("img", "trend.svg", "image/svg+xml")
  if (s.kind === "slides") add("slide", "review.pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation", { renders: [1, 2, 3].map((n) => ref("slide", `${slug}-${n}`, caseId, `slide-${n}.svg`, "image/svg+xml")) })
  if (s.kind === "text") add("text", "issue.txt", "text/plain")
  if (s.tags.includes("engineering")) add("patch", "fix.patch", "text/x-diff")
  const write = s.tags.includes("engineering")
  const baseline = armId === "baseline-a"
  const events: ChatEvent[] = [
    { seq: 1, kind: "message", actor: "u1", message_id: "m_turn", text: s.ask },
    { seq: 2, kind: "reaction_add", actor: "bot", message_id: "m_turn", emoji: "👀" },
    ...(write ? [{ seq: 3, kind: "card" as const, actor: "bot", message_id: "m_card", card: { id: "k1", kind: "confirmation" as const, headline: "Create an issue?", body: "The assistant wants to open an issue in the tracker.", facts: [{ label: "Title", value: "Nightly build fails" }], buttons: [{ id: "approve", label: "Approve", style: "primary" as const, action: "approve" as const }, { id: "deny", label: "Deny", style: "secondary" as const, action: "deny" as const }], state: "open" as const } },
      { seq: 4, kind: "card_update" as const, actor: "u1", scripted: "st1", card: { id: "k1", kind: "confirmation" as const, headline: "Create an issue?", body: "The assistant wants to open an issue in the tracker.", facts: [{ label: "Title", value: "Nightly build fails" }], buttons: [{ id: "approve", label: "Approve", style: "primary" as const, action: "approve" as const }, { id: "deny", label: "Deny", style: "secondary" as const, action: "deny" as const }], state: "answered" as const, answer: { by: "u1", button: "approve" } } }] : []),
    { seq: 5, kind: "reaction_remove", actor: "bot", message_id: "m_turn", emoji: "👀" },
    { seq: 6, kind: "message", actor: "bot", message_id: "m_reply", text: write ? (baseline ? "I opened an issue for the failing nightly build." : "I opened an issue for the failing nightly build and linked the last failing commit.") : REPLY(slug, s) },
    ...out.map((o, i) => ({ seq: 7 + i, kind: "file" as const, actor: "bot", message_id: "m_reply", files: [o.id] })),
  ]
  const calls = [
    { seq: 1, surface: "mcp", service: "files", tool: "list_files", origin: "recorded" as const, request: { folder: "shared" }, response: { files: ["numbers.csv", "notes.md"] } },
    { seq: 2, surface: "mcp", service: "files", tool: "read_file", origin: "recorded" as const, request: { path: "numbers.csv" }, response: { rows: 120 } },
    ...(baseline ? [] : [{ seq: 3, surface: "http", service: "web", tool: "get", origin: "simulated" as const, request: { url: "https://example.com/rates" }, response: { status: 200 }, effects: [{ verb: "web.page.read" }] }]),
    ...(write ? [{ seq: 4, surface: "http", service: "tracker", tool: "issues.create", origin: "overlay" as const, request: { title: "Nightly build fails" }, response: { number: 95 }, effects: [{ verb: "tracker.issue.create" }] }] : []),
    ...(armId === "replica-fast" ? [{ seq: 9, surface: "http", service: "mail", tool: "send", origin: "denied" as const, reason: "Not in the recorded world" }] : []),
  ]
  const bad = s.id === "c-chart" && armId === "replica-fast"
  return {
    id: `${r.id}:${caseId}:${armId}:${repeat}`, run_id: r.id, case: { id: caseId, revision: 2 }, arm_id: armId, repeat, status: bad ? "error" : "done",
    error: bad ? { kind: "timeout", message: "The item ran out of time" } : undefined,
    chat: events, outputs: out, world_calls: calls, usage: { cost_usd: baseline ? 0.08 : armId === "replica" ? 0.16 : 0.05, duration_ms: 12_000 + armId.length * 3000, tool_calls: calls.length },
    enclosure: { mode: baseline ? "recorded" : "enclosed", simulated_calls: calls.filter((c) => c.origin === "simulated").length, denied_calls: calls.filter((c) => c.origin === "denied").length },
  }
}
const scoresOf = (item: RunItem, s: Spec): Score[] => {
  const weak = item.arm_id === "replica-fast" || (item.arm_id === "replica" && s.id === "c-issue")
  return [
    { id: `${item.id}:s1`, item_id: item.id, criterion: "Calls the expected tools", grader: { kind: "code" }, value: { pass: true }, at: iso(-30) },
    { id: `${item.id}:s2`, item_id: item.id, criterion: "Answers the question that was asked", grader: { kind: "llm", judge_id: "rubric-1", model: "judge-a" }, value: { pass: !weak }, rationale: weak ? "The reply leaves out the recommendation." : "Covers the three changes and gives a recommendation.", at: iso(-29) },
    { id: `${item.id}:s3`, item_id: item.id, criterion: "Tone fits the channel", grader: { kind: "llm", judge_id: "rubric-1", model: "judge-b" }, value: { score: weak ? 3 : 4 }, at: iso(-29) },
  ]
}
const LABELS = ["A", "B", "C"]
function compare(r: Run, caseId: string, blind: boolean): Compare {
  const s = specOf(caseId) ?? SPECS[0]!
  const order = blind ? [...r.arms].sort((a, b) => (caseId.length + a.id.length) % 2 - (caseId.length + b.id.length) % 2 || a.id.localeCompare(b.id)) : r.arms
  const columns: Column[] = order.map((a, i) => {
    const label = blind ? LABELS[i]! : a.id === "baseline-a" ? "B" : a.label[0]!
    const item = buildItem(r, caseId, a.id, 0)
    return blind
      ? { label, item: { ...item, arm_id: label, id: `${r.id}:${caseId}:${label}`, outputs: item.outputs?.map((o) => ({ ...o, blob: { ...o.blob, sha256: o.blob.sha256 } })) }, scores: scoresOf(item, s).filter((x) => x.grader.kind !== "llm" || true).map((x) => ({ ...x, id: `${x.id}b`, item_id: `${r.id}:${caseId}:${label}`, grader: { ...x.grader, model: undefined, judge_id: undefined } })) }
      : { label, arm: a, item, scores: scoresOf(item, s) }
  })
  return { run_id: r.id, case_id: caseId, repeat: 0, blind, columns }
}

function summary(r: Run): RunSummary {
  const arms: ArmSummary[] = r.arms.map((a) => {
    const rate = [0.75, 0.88, 0.62][ARMS.findIndex((x) => x.id === a.id)] ?? 0.7
    const bd = (items: number) => ({ items, passed: Math.round(items * rate), pass_rate: Math.round(items * rate) / items })
    return { arm_id: a.id, label: a.label, items: 8, scored: 8, errors: a.id === "replica-fast" ? 1 : 0, passed: Math.round(8 * rate), pass_rate: rate, rubric_score: +(0.6 + rate * 0.3).toFixed(2), cost_usd: [0.64, 1.28, 0.4][ARMS.findIndex((x) => x.id === a.id)] ?? 0.5,
      pass_at_k: { 1: rate }, pass_pow_k: { 1: rate }, breakdown: { fidelity: { full: bd(3), tools: bd(3), text: bd(1), authored: bd(1) }, platform: { discord: bd(2), slack: bd(2), teams: bd(2), web: bd(2) } } }
  })
  return {
    run_id: r.id, arms,
    comparisons: arms.length > 1 ? [{ candidate: arms[1]!.arm_id, reference: arms[0]!.arm_id, metric: "pass_rate", cases: 8, difference: 0.125, low: -0.0625, high: 0.3125, margin: 0.05, non_inferior: arms[1]!.arm_id === "replica", only_candidate: 2, only_reference: 1, mcnemar_p: 0.62 }] : [],
    preferences: arms.length > 1 ? [{ arm_a: arms[0]!.arm_id, arm_b: arms[1]!.arm_id, a_wins: 2, b_wins: 4, ties: 2, sign_test_p: 0.41 }] : [],
    calibration: [{ judge: "judge-a", n: 24, agreement: 0.83, kappa: 0.66, flagged: false }, { judge: "judge-b", n: 24, agreement: 0.67, kappa: 0.31, flagged: true }],
    judge_cost_usd: 0.7, notes: ["Baselines have one recorded answer per case, so their variance is unknown."],
  }
}

// ---------- review ----------
const queues: (Queue & { suite_id: string; order: string[] })[] = [
  { id: "q-1", name: "Blind review of the replica", run_id: "run-1", suite_id: "weekly-reports", layout: "side_by_side", blind: true, reviewers_per_item: 1, status: "open", progress: { done: 2, total: 8 }, order: SPECS.map((s) => s.id) },
  { id: "q-2", name: "Spot check, replica only", run_id: "run-1", suite_id: "weekly-reports", layout: "single", blind: false, status: "open", progress: { done: 0, total: 8 }, order: SPECS.map((s) => s.id) },
]
function task(q: (typeof queues)[number]): ReviewTask | null {
  const r = runOf(q.run_id)!
  const id = q.order[q.progress.done]
  if (!id) return null
  const cols = compare(r, id, q.blind).columns
  const columns = q.layout === "single" ? cols.slice(0, 1) : cols
  const multi = columns.length > 1
  return {
    id: `${q.id}:${id}`, queue_id: q.id, suite_id: q.suite_id, case_id: id, blind: q.blind, layout: q.layout, columns, progress: q.progress,
    criteria: [{ id: "answers", text: "Answers the question that was asked", kind: "pass" }, { id: "tone", text: "Tone fits the channel", kind: "scale", min: 1, max: 5 }, ...(multi ? [{ id: "pref", text: "Which answer would you rather send?", kind: "preference" as const }] : [])],
  }
}
const comments: Comment[] = [{ id: "cm1", case_id: "c-spend", item_id: "run-1:c-spend:replica:0", author: { display_name: "Riley Chen" }, text: "The second bullet is clearer here.", anchors: [{ target: "message", item_id: "run-1:c-spend:replica:0", ref: "m_reply", quote: "Search spend rose 4%" }], resolved: false, at: iso(-50) }]
const bundles: string[] = []

const json = (b: unknown, status = 200) => HttpResponse.json(b as never, { status })
export const evalsHandlers: HttpHandler[] = [
  http.get("/api/evals/suites", async () => { await wait(); return guard() ?? json(suites.map(({ shares: _s, description: _d, ...s }) => s)) }),
  http.post("/api/evals/suites", async ({ request }) => {
    await wait(); const b = (await request.json()) as { id: string; name: string }
    if (suites.some((s) => s.id === b.id)) return json({ detail: "A suite with this id exists." }, 400)
    suites.push({ id: b.id, name: b.name, version: 1, role: "owner", scrub_status: "scrubbed", shares: [] }); SUITE_CASES[b.id] = []
    return guard() ?? json(suiteDoc(b.id))
  }),
  http.get("/api/evals/suites/:id", async ({ params, request }) => {
    await wait(120); const d = suiteDoc(String(params.id)); const v = new URL(request.url).searchParams.get("version")
    return guard() ?? (d ? json(v ? { ...d, version: Number(v) } : d) : notFound())
  }),
  http.put("/api/evals/suites/:id/shares", async ({ params, request }) => { await wait(); const s = suites.find((x) => x.id === params.id); if (!s) return notFound(); s.shares = (await request.json()) as Share[]; return new HttpResponse(null, { status: 204 }) }),
  http.get("/api/evals/suites/:id/cases", async ({ params }) => { await wait(); return guard() ?? json((SUITE_CASES[String(params.id)] ?? []).map(summaryOf)) }),
  http.get("/api/evals/suites/:id/cases/:case", async ({ params }) => { await wait(120); const s = specOf(String(params.case)); return guard() ?? (s ? json(buildCase(s)) : notFound()) }),
  http.put("/api/evals/suites/:id/cases/:case", async ({ params, request }) => {
    await wait(250); const c = (await request.json()) as Case; edits[String(params.case)] = { ...c, revision: c.revision + 1, hash: `h-${Date.now()}` }
    const suite = String(params.id)
    if (!specOf(c.id)) SUITE_CASES[suite] = [...(SUITE_CASES[suite] ?? []), { id: c.id, title: c.title, platform: c.surface.platform, fidelity: c.fidelity.level, tags: c.tags ?? [], ask: "", kind: "text" }]
    return json({ id: c.id, revision: c.revision + 1, hash: edits[c.id]!.hash })
  }),
  http.post("/api/evals/cases/validate", async ({ request }) => {
    await wait(150); const c = (await request.json()) as Partial<Case>
    const problems = [...(!c.title?.trim() ? ["title: a case needs a title"] : []), ...(!c.turn?.messages?.length ? ["turn.messages: at least one message"] : [])]
    return json({ problems, warnings: c.fidelity?.level === "authored" ? ["fidelity: authored cases carry no recorded world"] : [] })
  }),
  http.get("/api/evals/arms", async () => { await wait(); return guard() ?? json(ARMS) }),
  http.post("/api/evals/arms", async ({ request }) => { const a = (await request.json()) as Arm; ARMS.push(a); return json(a) }),
  http.get("/api/evals/runs", async ({ request }) => {
    await wait(); const suite = new URL(request.url).searchParams.get("suite")
    return guard() ?? json(runs.filter((r) => !suite || r.suite.id === suite).map(view).sort((a, b) => (b.started_at ?? "").localeCompare(a.started_at ?? "")))
  }),
  http.post("/api/evals/runs/estimate", async ({ request }) => {
    await wait(350); const d = (await request.json()) as { suite: { id: string }; case_ids?: string[]; arm_ids: string[]; repeats: number }
    return guard() ?? json(est(d.arm_ids.length, d.case_ids?.length ?? SUITE_CASES[d.suite.id]?.length ?? 0, d.repeats))
  }),
  http.post("/api/evals/runs", async ({ request }) => {
    await wait(300); const d = (await request.json()) as { suite: { id: string; version: number }; case_ids?: string[]; arm_ids: string[]; repeats: number; cap_usd: number }
    if (!(d.cap_usd > 0)) return json({ detail: "Set a spend cap first." }, 400)
    const id = `run-${runs.length + 1}`
    const r = mkRun(id, `Run ${runs.length + 1}`, d.suite.id, d.arm_ids, { suite: d.suite, case_ids: d.case_ids, repeats: d.repeats, status: "running", started_at: iso(0), estimate: est(d.arm_ids.length, d.case_ids?.length ?? SUITE_CASES[d.suite.id]?.length ?? 0, d.repeats), approval: { by: { display_name: "You" }, at: iso(0), cap_usd: d.cap_usd }, sim: { id, created: Date.now(), cap: d.cap_usd } })
    runs.unshift(r); return json(view(r))
  }),
  http.get("/api/evals/runs/:id", async ({ params }) => { await wait(100); const r = runOf(String(params.id)); return guard() ?? (r ? json(view(r)) : notFound()) }),
  http.post("/api/evals/runs/:id/cancel", async ({ params }) => { const r = runOf(String(params.id)); if (!r) return notFound(); r.sim.cancelled = true; r.sim.fixed = undefined; return json(view(r)) }),
  http.post("/api/evals/approvals/:id", async ({ params, request }) => {
    const r = runOf(String(params.id)); const b = (await request.json()) as { cap_usd: number }
    if (!r || !(b.cap_usd > 0)) return notFound()
    r.sim = { id: r.id, created: Date.now(), cap: b.cap_usd }; r.status = "running"; r.started_at = iso(0); r.approval = { by: { display_name: "You" }, at: iso(0), cap_usd: b.cap_usd }
    return json(view(r))
  }),
  http.get("/api/evals/runs/:id/items", async ({ params }) => { await wait(100); const r = runOf(String(params.id)); return guard() ?? (r ? json(itemList(r)) : notFound()) }),
  http.get("/api/evals/runs/:id/summary", async ({ params }) => { await wait(200); const r = runOf(String(params.id)); return r ? json(summary(view(r))) : notFound() }),
  http.get("/api/evals/runs/:id/compare", async ({ params, request }) => {
    await wait(200); const r = runOf(String(params.id)); const q = new URL(request.url).searchParams
    return r ? json(compare(view(r), q.get("case") ?? caseIds(r)[0]!, q.get("blind") === "1")) : notFound()
  }),
  http.post("/api/evals/runs/:id/judge", async ({ params, request }) => {
    await wait(300); const r = runOf(String(params.id)); const b = (await request.json()) as { cap_usd?: number }
    return !r ? notFound() : b.cap_usd ? json({ ok: true }, 202) : json(est(r.arms.length, caseIds(r).length, 1).lines.filter((l) => l.category === "judge").length ? { ...est(r.arms.length, caseIds(r).length, 1), lines: est(r.arms.length, caseIds(r).length, 1).lines.filter((l) => l.category === "judge") } : est(1, 1, 1))
  }),
  http.get("/api/evals/items/:id", async ({ params }) => { const [run, c, arm, rep] = String(params.id).split(":"); const r = runOf(run!); return r ? json(buildItem(r, c!, arm!, Number(rep))) : notFound() }),
  http.get("/api/evals/queues", async () => { await wait(); return guard() ?? json(queues.map(({ order: _o, suite_id: _s, ...q }) => q)) }),
  http.post("/api/evals/queues", async ({ request }) => {
    const b = (await request.json()) as { run_id: string; name: string; layout: Queue["layout"]; blind: boolean }
    const r = runOf(b.run_id); if (!r) return notFound()
    const q = { id: `q-${queues.length + 1}`, name: b.name, run_id: b.run_id, suite_id: r.suite.id, layout: b.layout, blind: b.blind, status: "open" as const, progress: { done: 0, total: caseIds(r).length }, order: caseIds(r) }
    queues.push(q); return json(q)
  }),
  http.get("/api/evals/queues/:id/next", async ({ params }) => { await wait(200); const q = queues.find((x) => x.id === params.id); return guard() ?? (q ? json(task(q)) : notFound()) }),
  http.post("/api/evals/scores", async ({ request }) => {
    await wait(200); const b = (await request.json()) as { task_id: string }
    const q = queues.find((x) => x.id === b.task_id.split(":")[0]); if (!q) return notFound()
    q.progress = { ...q.progress, done: q.progress.done + 1 }; if (q.progress.done >= q.progress.total) q.status = "closed"
    return new HttpResponse(null, { status: 204 })
  }),
  http.get("/api/evals/comments", async ({ request }) => { await wait(100); const c = new URL(request.url).searchParams.get("case"); return json(comments.filter((x) => !c || x.case_id === c)) }),
  http.post("/api/evals/comments", async ({ request }) => {
    const b = (await request.json()) as Partial<Comment>; const c: Comment = { id: `cm${comments.length + 1}`, case_id: b.case_id, item_id: b.item_id, author: { display_name: me()?.name ?? "You" }, text: b.text ?? "", anchors: b.anchors, resolved: false, at: iso(0) }
    comments.push(c); return json(c)
  }),
  http.post("/api/evals/bundles/export", async () => { await wait(400); bundles.push("x"); return json({ sha256: "b:bundle:export:0", size: 48_000, media_type: "application/zip", name: "weekly-reports.zip" }) }),
  http.post("/api/evals/bundles/import", async () => { await wait(500); return json({ bundle_id: "bundle-1", suites: [{ id: "imported-suite", version: 1, created: true }], runs: ["run-9"], skipped_runs: [], blobs: 34, signature: "unsigned", warnings: ["The bundle is not signed."] }) }),
  http.get("/api/evals/blobs/:sha/table", async ({ params }) => {
    const [, , variant, c] = String(params.sha).split(":"); void c
    return json({ sheets: [{ name: "Options", rows: SHEET(variant ?? "replica") }, { name: "Notes", rows: [["Note"], ["Prices are monthly"]] }] })
  }),
  http.get("/api/evals/blobs/:sha", async ({ params }) => { const b = blob(String(params.sha)); return new HttpResponse(b.body, { headers: { "Content-Type": b.type } }) }),
]
