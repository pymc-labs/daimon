// What a publish changes: per-node diffs, triggers, access and the grants a version needs.
import { titleOf } from "./spec"
import { mappingText } from "./mapping"
import { approverLabel } from "./kinds"
import { ANSWER, BUILTINS, labelOf, NETWORK, OUTCOME, placeOptions, SESSION, WHERE, WRITES, type Choices } from "./options"
import type { Catalog, Kind, Node, Spec, Trigger, TriggerKind } from "./types"

export type Effect = "added" | "changed" | "removed"
/** One labelled value that differs; null on a side means it is absent there. */
export type FieldChange = { label: string; from: string | null; to: string | null }
export type Line = { op: "keep" | "add" | "del"; text: string }
export type NodeReview = { id: string; title: string; kind: Kind; effect: Effect; instructions: Line[] | null; changes: FieldChange[] }
export type TriggerReview = { id: string; kind: TriggerKind; effect: Effect; changes: FieldChange[] }
export type Review = { workflow: FieldChange[]; nodes: NodeReview[]; triggers: TriggerReview[]; access: FieldChange[] }
/** The catalog names agents, channels and people; without it ids show as they are. */
export type ReviewOpts = { catalog?: Catalog }

const text = (v: unknown): string => (v == null ? "" : typeof v === "string" ? v : typeof v === "number" || typeof v === "boolean" ? String(v) : Array.isArray(v) ? v.map(text).join(", ") : JSON.stringify(v))

/** Line diff by longest common subsequence; blank-only differences at the ends are ignored. */
export function lineDiff(a: string, b: string): Line[] {
  const x = a.replace(/\n+$/, "").split("\n"), y = b.replace(/\n+$/, "").split("\n")
  const t: number[][] = Array.from({ length: x.length + 1 }, () => new Array<number>(y.length + 1).fill(0))
  for (let i = x.length - 1; i >= 0; i--) for (let j = y.length - 1; j >= 0; j--) t[i]![j] = x[i] === y[j] ? t[i + 1]![j + 1]! + 1 : Math.max(t[i + 1]![j]!, t[i]![j + 1]!)
  const out: Line[] = []
  let i = 0, j = 0
  while (i < x.length && j < y.length) {
    if (x[i] === y[j]) { out.push({ op: "keep", text: x[i]! }); i++; j++ } else if (t[i + 1]![j]! >= t[i]![j + 1]!) out.push({ op: "del", text: x[i++]! })
    else out.push({ op: "add", text: y[j++]! })
  }
  while (i < x.length) out.push({ op: "del", text: x[i++]! })
  while (j < y.length) out.push({ op: "add", text: y[j++]! })
  return out
}

/** Keep changed lines with one line of context around them. */
export function trimContext(lines: Line[], context = 1): Line[] {
  const keep = lines.map((l, i) => l.op !== "keep" || lines.slice(Math.max(0, i - context), i + context + 1).some((n) => n.op !== "keep"))
  return lines.filter((_, i) => keep[i])
}

const target = (spec: Spec, v: unknown) => (typeof v === "string" && spec.nodes[v] ? titleOf(v, spec.nodes[v]) : text(v))

type Ctx = { spec: Spec; catalog?: Catalog }
const agentName = (c: Ctx, key: unknown) => c.catalog?.agents.find((a) => a.key === key)?.name ?? text(key)
const placeName = (c: Ctx, v: unknown, people: boolean) => placeOptions(c.catalog, text(v), people).find((o) => o.value === v)?.label ?? text(v)
const choice = (choices: Choices) => (_: Ctx, v: unknown) => labelOf(choices, text(v))
const yesNo = (_: Ctx, v: unknown) => (v ? "Yes" : "No")

/** Fields the inspector words differently from the spec: its label and the value as it shows it. */
const FIELDS: Record<string, [string, (c: Ctx, v: unknown, n: Node) => string]> = {
  agent: ["Agent", (c, v) => agentName(c, v)],
  as_agent: ["Post as", (c, v) => agentName(c, v)],
  at: ["Works in", (c, v) => placeName(c, v, false)],
  to: ["Post to", (c, v) => placeName(c, v, true)],
  memory: ["Personal memory", () => "On"],
  session: ["Session", choice(SESSION)],
  writes: ["Writes", choice(WRITES)],
  builtins: ["Built-ins", choice(BUILTINS)],
  tools: ["Tools", (_, v) => (Array.isArray(v) ? text(v) : "Inherit")],
  network: ["Network", choice(NETWORK)],
  outcome: ["Outcome", choice(OUTCOME)],
  answer: ["Answer", choice(ANSWER)],
  channel: ["Channel", (c, v) => placeName(c, v, false)],
  approvers: ["Approvers", (c, v) => [v].flat().map((a) => approverLabel(text(a), c.catalog)).join(", ")],
  min_approvals: ["Needs", (_, v, n) => `${text(v)} of ${[n.approvers ?? []].flat().length}`],
  binds: ["Covers", (c, v) => [v].flat().map((b) => target(c.spec, b)).join(", ")],
  budget_usd: ["Budget", (_, v) => `$${text(v)}`],
  retry: ["Tries", (_, v) => text((v as NonNullable<Node["retry"]>).attempts)],
  timeout: ["Timeout", (c, v) => (typeof v === "object" ? `${(v as { after: string }).after}, then ${target(c.spec, (v as { route: string }).route)}` : text(v))],
  idempotent: ["Idempotent", yesNo],
  can_ask: ["Can ask", yesNo],
  can_finish_early: ["Can finish early", yesNo],
  allow_self_approval: ["Allow self approval", yesNo],
  show: ["Shows", (_, v) => text(v)],
  for: ["Duration", (_, v) => text(v)],
  as: ["Each as", (_, v) => text(v)],
  concurrency: ["At a time", (_, v) => text(v)],
  tolerate_failures: ["Allowed failures", (_, v) => text(v)],
  source: ["Python", (_, v) => text(v)],
}

/** A node's fields as label to value, so two nodes diff field by field. */
function flat(spec: Spec, n: Node | undefined, catalog?: Catalog): Record<string, string> {
  const out: Record<string, string> = {}
  if (!n) return out
  const c: Ctx = { spec, catalog }
  for (const [k, v] of Object.entries(n)) {
    if (v == null || k === "kind" || k === "instructions") continue
    if (k === "routes") for (const [r, to] of Object.entries(v as NonNullable<Node["routes"]>)) {
      const t = typeof to === "string" ? to : to.to
      out[`Route ${r}`] = target(spec, t)
      if (typeof to !== "string" && to.when) out[`Route ${r} when`] = to.when
    } else if (k === "output" && typeof v === "object") for (const [f, d] of Object.entries(v as Record<string, { type?: string }>)) out[`Output ${f}`] = d.type ?? "any"
    else if (k === "next" || k === "else" || k === "on_error" || k === "on_max_visits" || k === "otherwise") out[k === "next" ? "Next" : k === "else" ? "Otherwise" : k === "on_error" ? "On error" : k === "otherwise" ? "Other replies" : "After max visits"] = [v].flat().map((t) => target(spec, t)).join(", ")
    else if (k === "cases") (v as NonNullable<Node["cases"]>).forEach((x, i) => { out[`Case ${i + 1}`] = `${x.when} to ${target(spec, x.to)}` })
    else if ((k === "inputs" || k === "args" || k === "set") && typeof v === "object") for (const [f, e] of Object.entries(v as Record<string, string>)) out[`${k === "inputs" ? "Input" : k === "args" ? "Argument" : "Set"} ${f}`] = e
    else if (k === "form" && typeof v === "object") for (const f of Object.keys(v)) out[`Form ${f}`] = "field"
    else if (k === "deliver" && typeof v === "object") for (const [f, d] of Object.entries(v)) out[f === "to" ? "Deliver to" : f === "message" ? "Message" : `Deliver ${f}`] = f === "to" ? placeName(c, d, true) : text(d)
    else if (k === "deliver") out.Where = labelOf(WHERE, text(v))
    else if (k === "event" && typeof v === "object") for (const [f, d] of Object.entries(v)) out[f === "source" ? "Source" : "Match"] = text(d)
    else if (k === "poll" && typeof v === "object") for (const [f, d] of Object.entries(v)) out[f === "tool" ? "Tool" : f === "every" ? "Every" : f === "until" ? "Until" : `Poll ${f}`] = text(d)
    else if (k === "do" && typeof v === "object") for (const [f, d] of Object.entries(v as Node)) { if (f !== "kind" && d != null) out[`Each item ${f.replace(/_/g, " ")}`] = f === "agent" ? agentName(c, d) : text(d) }
    else if (FIELDS[k]) out[FIELDS[k][0]] = FIELDS[k][1](c, v, n)
    else if (k === "title") out.Title = text(v)
    else out[k.charAt(0).toUpperCase() + k.slice(1).replace(/_/g, " ")] = text(v)
  }
  return out
}

export function fieldChanges(a: Record<string, string>, b: Record<string, string>): FieldChange[] {
  const out: FieldChange[] = []
  for (const k of new Set([...Object.keys(a), ...Object.keys(b)])) if (a[k] !== b[k]) out.push({ label: k, from: a[k] ?? null, to: b[k] ?? null })
  return out
}

const triggerFlat = (t: Trigger | undefined): Record<string, string> => !t ? {} : Object.fromEntries(([
  ["Cron", t.cron], ["Time zone", t.timezone], ["Missed runs", t.catch_up], ["Verification", t.verify], ["Inputs", t.inputs && mappingText(t.inputs)], ["Response", t.respond], ["Source", t.source], ["Match", t.match],
] as const).filter(([, v]) => v != null) as [string, string][])

const effectOf = (a: unknown, b: unknown): Effect => (!a ? "added" : !b ? "removed" : "changed")

export function reviewDiff(live: Spec | null, next: Spec, opts: ReviewOpts = {}): Review {
  const base = live ?? { ...next, nodes: {}, triggers: [] }
  const ch = (id: string) => placeName({ spec: next, catalog: opts.catalog }, id, false)
  const top = (s: Spec): Record<string, string> => ({ Name: s.name, ...(s.goal && { Goal: s.goal }), ...(s.label && { Label: s.label }), "Start step": target(s, s.start) })
  const access = (s: Spec): Record<string, string> => Object.fromEntries(([
    ["Visibility", s.visibility ?? "private"], ["Home channel", s.home && ch(s.home.channel)], ["Budget per step", s.defaults?.budget_usd != null && `$${s.defaults.budget_usd}`],
    ["Run budget", s.limits?.budget_usd != null && `$${s.limits.budget_usd}`], ["Deadline", s.limits?.deadline],
    ["Concurrency", s.concurrency && `${s.concurrency.on_conflict ?? "queue"}, limit ${s.concurrency.limit ?? 1}`],
  ] as const).filter(([, v]) => v) as [string, string][])
  const nodes: NodeReview[] = []
  const ids = [...Object.keys(next.nodes), ...Object.keys(base.nodes).filter((k) => !next.nodes[k])]
  for (const id of ids) {
    const a = base.nodes[id], b = next.nodes[id]
    if (JSON.stringify(a) === JSON.stringify(b) && JSON.stringify(flat(base, a, opts.catalog)) === JSON.stringify(flat(next, b, opts.catalog))) continue
    const n = (b ?? a)!
    const ins = a?.instructions !== b?.instructions && (a?.instructions || b?.instructions) ? trimContext(lineDiff(a?.instructions ?? "", b?.instructions ?? "")) : null
    const changes = fieldChanges(flat(base, a, opts.catalog), flat(next, b, opts.catalog))
    if (!ins && !changes.length) continue
    nodes.push({ id, title: titleOf(id, n), kind: n.kind, effect: effectOf(a, b), instructions: ins, changes })
  }
  const triggers: TriggerReview[] = []
  const tids = [...(next.triggers ?? []).map((t) => t.id), ...(base.triggers ?? []).filter((t) => !next.triggers?.some((x) => x.id === t.id)).map((t) => t.id)]
  for (const id of tids) {
    const a = base.triggers?.find((t) => t.id === id), b = next.triggers?.find((t) => t.id === id)
    if (JSON.stringify(a) === JSON.stringify(b)) continue
    triggers.push({ id, kind: (b ?? a)!.kind, effect: effectOf(a, b), changes: fieldChanges(triggerFlat(a), triggerFlat(b)) })
  }
  return { workflow: fieldChanges(top(base), top(next)), nodes, triggers, access: fieldChanges(access(base), access(next)) }
}

export const reviewCount = (r: Review) => r.workflow.length + r.nodes.length + r.triggers.length + r.access.length
