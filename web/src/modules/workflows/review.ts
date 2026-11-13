// What a publish changes: per-node diffs, triggers, access and the grants a version needs.
import { safety, titleOf } from "./spec"
import type { Grant, Kind, Node, Spec, Trigger, TriggerKind } from "./types"

export type Effect = "added" | "changed" | "removed"
/** One labelled value that differs; null on a side means it is absent there. */
export type FieldChange = { label: string; from: string | null; to: string | null }
export type Line = { op: "keep" | "add" | "del"; text: string }
export type NodeReview = { id: string; title: string; kind: Kind; effect: Effect; instructions: Line[] | null; changes: FieldChange[] }
export type TriggerReview = { id: string; kind: TriggerKind; effect: Effect; changes: FieldChange[] }
export type Review = { workflow: FieldChange[]; nodes: NodeReview[]; triggers: TriggerReview[]; access: FieldChange[] }
export type ReviewOpts = { channel?: (id: string) => string }

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

/** A node's fields as label to value, so two nodes diff field by field. */
function flat(spec: Spec, n: Node | undefined): Record<string, string> {
  const out: Record<string, string> = {}
  if (!n) return out
  for (const [k, v] of Object.entries(n)) {
    if (v == null || k === "kind" || k === "instructions") continue
    if (k === "routes") for (const [r, to] of Object.entries(v as NonNullable<Node["routes"]>)) {
      const t = typeof to === "string" ? to : to.to
      out[`Route ${r}`] = target(spec, t)
      if (typeof to !== "string" && to.when) out[`Route ${r} when`] = to.when
    } else if (k === "output" && typeof v === "object") for (const [f, d] of Object.entries(v as Record<string, { type?: string }>)) out[`Output ${f}`] = d.type ?? "any"
    else if (k === "next" || k === "else" || k === "on_error" || k === "on_max_visits") out[k === "next" ? "Next" : k === "else" ? "Otherwise" : k === "on_error" ? "On error" : "After max visits"] = [v].flat().map((t) => target(spec, t)).join(", ")
    else if (k === "cases") (v as NonNullable<Node["cases"]>).forEach((c, i) => { out[`Case ${i + 1}`] = `${c.when} to ${target(spec, c.to)}` })
    else if ((k === "inputs" || k === "args" || k === "set") && typeof v === "object") for (const [f, e] of Object.entries(v as Record<string, string>)) out[`${k === "inputs" ? "Input" : k === "args" ? "Argument" : "Set"} ${f}`] = e
    else if (k === "form" && typeof v === "object") for (const f of Object.keys(v)) out[`Form ${f}`] = "field"
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
  ["Cron", t.cron], ["Time zone", t.timezone], ["Missed runs", t.catch_up], ["Verification", t.verify], ["Inputs", t.inputs], ["Response", t.respond], ["Source", t.source], ["Match", t.match],
] as const).filter(([, v]) => v != null) as [string, string][])

const effectOf = (a: unknown, b: unknown): Effect => (!a ? "added" : !b ? "removed" : "changed")

export function reviewDiff(live: Spec | null, next: Spec, opts: ReviewOpts = {}): Review {
  const base = live ?? { ...next, nodes: {}, triggers: [] }
  const ch = opts.channel ?? ((id: string) => id)
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
    if (JSON.stringify(a) === JSON.stringify(b) && JSON.stringify(flat(base, a)) === JSON.stringify(flat(next, b))) continue
    const n = (b ?? a)!
    const ins = a?.instructions !== b?.instructions && (a?.instructions || b?.instructions) ? trimContext(lineDiff(a?.instructions ?? "", b?.instructions ?? "")) : null
    const changes = fieldChanges(flat(base, a), flat(next, b))
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

const gkey = (g: Grant) => `${g.kind}|${g.node ?? ""}|${g.detail}`
/** Grants the draft needs that the live version did not already hold; only these need an admin. */
export function newGrants(live: Spec | null, next: Spec): Grant[] {
  const held = new Set((live ? safety(live).grants : []).map(gkey))
  return safety(next).grants.filter((g) => !held.has(gkey(g)))
}
