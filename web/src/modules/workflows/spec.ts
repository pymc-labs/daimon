// Pure spec logic shared by the canvas and the mock server: edges, references, validation, diffs and edits.
import type { AnswerShape, Change, Field, Grant, Kind, Node, Problem, RuleClass, Spec, Validation } from "./types"

export type EdgeKind = "next" | "route" | "case" | "else" | "error" | "max" | "otherwise"
/** One edge of the graph. `slot` says where `from` points at `to`, so edits can rewire it. */
export type FlowEdge = { from: string; to: string; kind: EdgeKind; label?: string; slot: string }

const routeTo = (r: string | { to: string }) => (typeof r === "string" ? r : r.to)

export function edgesOf(id: string, n: Node): FlowEdge[] {
  const out: FlowEdge[] = []
  const nexts = n.next == null ? [] : Array.isArray(n.next) ? n.next : [n.next]
  nexts.forEach((to, i) => out.push({ from: id, to, kind: "next", slot: `next.${i}` }))
  for (const [label, r] of Object.entries(n.routes ?? {})) out.push({ from: id, to: routeTo(r), kind: "route", label, slot: `routes.${label}` })
  n.cases?.forEach((c, i) => out.push({ from: id, to: c.to, kind: "case", label: caseLabel(c.when), slot: `cases.${i}` }))
  if (n.else) out.push({ from: id, to: n.else, kind: "else", label: "else", slot: "else" })
  if (n.otherwise) out.push({ from: id, to: n.otherwise, kind: "otherwise", label: "otherwise", slot: "otherwise" })
  if (n.on_error && n.on_error !== "fail") out.push({ from: id, to: n.on_error, kind: "error", label: "error", slot: "on_error" })
  if (n.on_max_visits) out.push({ from: id, to: n.on_max_visits, kind: "max", label: `after ${n.max_visits ?? 3} visits`, slot: "on_max_visits" })
  return out
}

/** A short edge label from a CEL condition: the last identifier, or the compared value. */
export function caseLabel(when: string) {
  const e = when.replace(/^\$\{\{\s*|\s*\}\}$/g, "")
  const eq = e.match(/==\s*["']?([\w-]+)["']?\s*$/)
  if (eq) return eq[1]!
  return e.split(/[.\s()!]+/).filter(Boolean).pop() ?? e
}

export const allEdges = (spec: Spec) => Object.entries(spec.nodes).flatMap(([id, n]) => edgesOf(id, n))

export function reachable(spec: Spec) {
  const seen = new Set<string>()
  const adj = adjacency(spec)
  const stack = spec.nodes[spec.start] ? [spec.start] : []
  while (stack.length) {
    const id = stack.pop()!
    if (seen.has(id)) continue
    seen.add(id)
    stack.push(...(adj.get(id) ?? []).filter((t) => spec.nodes[t]))
  }
  return seen
}

function adjacency(spec: Spec, reverse = false) {
  const m = new Map<string, string[]>()
  for (const e of allEdges(spec)) {
    const [a, b] = reverse ? [e.to, e.from] : [e.from, e.to]
    m.set(a, [...(m.get(a) ?? []), b])
  }
  return m
}

/** Every node with a path to `id`, loops included (a loop body reads earlier visits). */
export function ancestors(spec: Spec, id: string) {
  const rev = adjacency(spec, true)
  const seen = new Set<string>()
  const stack = [...(rev.get(id) ?? [])]
  while (stack.length) {
    const x = stack.pop()!
    if (seen.has(x) || !spec.nodes[x]) continue
    seen.add(x)
    stack.push(...(rev.get(x) ?? []))
  }
  return seen
}

/** Edges that close a cycle, found by depth-first search from the start. */
export function backEdges(spec: Spec) {
  const back = new Set<string>()
  const state = new Map<string, 1 | 2>()
  const visit = (id: string) => {
    state.set(id, 1)
    for (const e of edgesOf(id, spec.nodes[id]!)) {
      if (!spec.nodes[e.to]) continue
      const s = state.get(e.to)
      if (s === 1) back.add(edgeKey(e))
      else if (!s) visit(e.to)
    }
    state.set(id, 2)
  }
  if (spec.nodes[spec.start]) visit(spec.start)
  for (const id of Object.keys(spec.nodes)) if (!state.has(id)) visit(id)
  return back
}
export const edgeKey = (e: FlowEdge) => `${e.from}:${e.slot}`

// ---------- References ----------

const EXPR = /\$\{\{\s*([\s\S]+?)\s*\}\}/g
const REF = /\b(steps\.\w+(?:\.\w+)*|inputs(?:\.\w+)?|trigger(?:\.\w+)*|item(?:\.\w+)*|workflow(?:\.\w+)*)/g

/** Every expression string a node carries, with where it sits. */
function exprsOf(n: Node): string[] {
  const out: string[] = []
  const add = (v: unknown) => {
    if (typeof v === "string") out.push(v)
    else if (v && typeof v === "object") Object.values(v).forEach(add)
  }
  const d = finishDeliver(n) ?? n
  add([n.inputs, n.args, n.show, n.attach, n.set, n.items, n.until, n.event?.match, n.poll?.args, n.state, n.cases?.map((c) => c.when)])
  add([d.to, d.thread, d.also, d.mention, d.message])
  if (typeof n.output === "string") add(n.output)
  if (n.form) add(Object.values(n.form).map((f) => f.default))
  if (n.do) out.push(...exprsOf(n.do))
  return out
}

export function refsOf(n: Node) {
  const refs = new Set<string>()
  for (const s of exprsOf(n)) for (const m of s.matchAll(EXPR)) for (const r of m[1]!.matchAll(REF)) refs.add(r[1]!)
  return [...refs]
}

export type Ref = { path: string; type: string; untrusted?: boolean }
export type RefGroup = { id: string; label: string; kind?: Kind; refs: Ref[] }

export const fieldType = (f?: Field) => (!f ? "any" : f.enum ? "choice" : f.type === "string" ? "text" : f.type === "boolean" ? "yes/no" : f.type === "array" ? "list" : f.type ?? "any")

/** Where a Finish step delivers; an approval's `deliver` says where it asks instead. */
export const finishDeliver = (n: Node) => (typeof n.deliver === "object" ? n.deliver : null)

/** Output fields a step offers to later steps. */
export function outputFields(n: Node): Record<string, Field> {
  switch (n.kind) {
    case "agent": case "code": return typeof n.output === "object" ? n.output : {}
    case "approval": return { route: { enum: Object.keys(n.routes ?? { approve: 1, reject: 1 }) }, text: { type: "string" }, values: { type: "object" }, files: { type: "array" }, by: { type: "string" }, via: { type: "string" }, ...(n.form ?? {}) }
    case "transform": return Object.fromEntries(Object.keys(n.set ?? {}).map((k) => [k, {}]))
    default: return {}
  }
}

const UNTRUSTED_OUT: Kind[] = ["action", "wait"]
/** An approval that takes data (a form, text) from someone who may not be an owner or editor: what it returns is untrusted input. */
export const humanAnswer = (n: Node) =>
  n.kind === "approval" && (Object.keys(n.form ?? {}).length > 0 || n.answer === "text" || n.answer === "form") && [n.approvers ?? []].flat().some((a) => a !== "owner" && a !== "editors")
const untrustedOut = (n?: Node) => !!n && (UNTRUSTED_OUT.includes(n.kind) || humanAnswer(n))
const UNTRUSTED_TRIGGERS = ["webhook", "event", "mention", "poll"]
const hasUntrustedTrigger = (spec: Spec) => (spec.triggers ?? []).some((t) => UNTRUSTED_TRIGGERS.includes(t.kind))
/** Saved state and the last run's result were written by earlier runs that read outside data. */
const fromRuns = (r: string) => r === "workflow" || r.startsWith("workflow.state") || r.startsWith("workflow.last_run")

/** Steps upstream that produce files, as references an ask can attach. */
export function fileRefs(spec: Spec, id: string): RefGroup[] {
  const up = ancestors(spec, id)
  return Object.entries(spec.nodes)
    .filter(([sid, n]) => up.has(sid) && (n.kind === "agent" || n.kind === "code" || n.kind === "for_each"))
    .map(([sid, n]) => ({ id: sid, label: titleOf(sid, n), kind: n.kind, refs: [{ path: `steps.${sid}.files`, type: "files" }] }))
}

/** What a node may reference: inputs, the trigger and every upstream step's output. */
export function upstreamRefs(spec: Spec, id: string): RefGroup[] {
  const u = hasUntrustedTrigger(spec)
  const groups: RefGroup[] = [{
    id: "inputs", label: "Inputs",
    refs: [{ path: "inputs", type: "object", untrusted: u }, ...Object.entries(spec.inputs?.properties ?? {}).map(([k, f]) => ({ path: `inputs.${k}`, type: fieldType(f), untrusted: u }))],
  }]
  if (spec.triggers?.some((t) => t.kind !== "manual")) groups.push({ id: "trigger", label: "Trigger", refs: [{ path: "trigger.payload", type: "object", untrusted: true }, { path: "trigger.received_at", type: "time" }] })
  const up = ancestors(spec, id)
  for (const [sid, n] of Object.entries(spec.nodes)) {
    if (!up.has(sid) || n.kind === "finish") continue
    const base = `steps.${sid}`
    const untrusted = untrustedOut(n)
    const refs: Ref[] = [{ path: `${base}.output`, type: n.kind === "for_each" ? "list" : "object", untrusted }]
    for (const [k, f] of Object.entries(outputFields(n))) refs.push({ path: `${base}.output.${k}`, type: fieldType(f), untrusted })
    if (n.kind === "agent") refs.push({ path: `${base}.summary`, type: "text" })
    if (n.routes) refs.push({ path: `${base}.route`, type: "choice" })
    groups.push({ id: sid, label: titleOf(sid, n), kind: n.kind, refs })
  }
  return groups
}

// ---------- Validation ----------

// The only event source that fires: a finished run of that workflow.
const EVENT_SOURCE = /^workflow:[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$/
const BAD_EVENT_SOURCE = "An event source is a workflow, as `workflow:<id>`; other events never arrive. Use a webhook trigger for outside events"
const BAD_WAIT_SOURCE = "A wait's event is another workflow's run (`workflow:<id>`), a delivery to one of this workflow's webhook triggers (`trigger:<id>`) or `callback`"
const waitSource = (spec: Spec, source: string) =>
  EVENT_SOURCE.test(source) || source === "callback" || (source.startsWith("trigger:") && !!spec.triggers?.some((t) => t.kind === "webhook" && `trigger:${t.id}` === source))

const WRITE_TOOL = /send|tag|create|update|post|delete|comment|note|page|issue|folder|project/
export const toolWrites = (tool?: string) => !!tool && WRITE_TOOL.test(tool.split("/")[1] ?? tool)

export function titleOf(id: string, n?: Node) {
  return n?.title || (id.charAt(0).toUpperCase() + id.slice(1)).replace(/_/g, " ")
}

export function validate(spec: Spec): Validation {
  const errors: Problem[] = []
  const warnings: Problem[] = []
  const err = (code: string, message: string, node?: string) => errors.push({ severity: "error", code, message, node })
  const warn = (code: string, message: string, node?: string) => warnings.push({ severity: "warning", code, message, node })
  const nodes = spec.nodes
  if (!nodes[spec.start]) err("start", `Start step missing: ${spec.start}`)
  if (!Object.values(nodes).some((n) => n.kind === "finish")) err("no_finish", "No Finish step")
  if (spec.triggers?.some((t) => t.kind === "event" && !EVENT_SOURCE.test(t.source ?? ""))) err("bad_event_source", BAD_EVENT_SOURCE)
  const seen = reachable(spec)
  for (const [id, n] of Object.entries(nodes)) {
    if (n.kind === "wait" && n.event && !waitSource(spec, n.event.source)) err("bad_event_source", BAD_WAIT_SOURCE, id)
    const out = edgesOf(id, n)
    for (const e of out) if (!nodes[e.to]) err("unknown_target", `Unknown step: ${e.to}`, id)
    if (n.kind !== "finish" && !out.length) err("dead_end", "No next step", id)
    if (!seen.has(id)) err("unreachable", "Unreachable", id)
    if (n.kind === "approval" && n.binds) for (const b of [n.binds].flat()) if (!nodes[b]) err("unknown_bind", `Binds unknown step: ${b}`, id)
    const up = ancestors(spec, id)
    for (const r of refsOf(n)) {
      const [ns, sid, part, field] = r.split(".")
      if (ns === "inputs" && sid && !spec.inputs?.properties[sid]) err("unknown_ref", `Unknown input: ${r}`, id)
      if (ns !== "steps" || !sid) continue
      if (!nodes[sid]) err("unknown_ref", `Unknown step: ${r}`, id)
      else if (!up.has(sid)) err("not_upstream", `Not upstream: ${r}`, id)
      else if (part === "output" && field && Object.keys(outputFields(nodes[sid])).length && !outputFields(nodes[sid])[field]) err("unknown_ref", `Unknown field: ${r}`, id)
    }
    if (n.kind === "for_each" && (n.concurrency ?? 1) > 5 && n.do?.kind === "agent") warn("cost", "Agent per item, high concurrency", id)
  }
  const back = backEdges(spec)
  for (const e of allEdges(spec)) {
    if (!back.has(edgeKey(e))) continue
    const loop = [e.to, ...[...ancestors(spec, e.from)].filter((x) => ancestors(spec, x).has(e.to) || x === e.to), e.from]
    if (!loop.some((x) => nodes[x]?.max_visits) && !gated(spec, new Set(loop))) err("loop_limit", "Loop needs max visits", e.to)
    if (!spec.limits?.budget_usd && !spec.defaults?.budget_usd && loop.some((x) => nodes[x]?.kind === "agent")) warn("cost", "Agent loop without budget", e.to)
  }
  const { classes, grants } = safety(spec)
  for (const [id, c] of Object.entries(classes)) {
    // Scoped writes pin their destination, so only unscoped ones break the Rule of Two.
    if (c.includes("U") && writes(nodes[id]!) && !boundBy(spec, id) && !nodes[id]!.untrusted_waiver) err("rule_of_two", "Untrusted input reaches a write", id)
  }
  return { errors, warnings, grants, classes }
}

/** Every cycle through `loop` passes an approval: a person answers before each lap. */
function gated(spec: Spec, loop: Set<string>) {
  const rest = new Set([...loop].filter((x) => spec.nodes[x] && spec.nodes[x].kind !== "approval"))
  const state = new Map<string, 1 | 2>()
  const cycles = (id: string): boolean => {
    state.set(id, 1)
    for (const e of edgesOf(id, spec.nodes[id]!)) if (rest.has(e.to) && (state.get(e.to) === 1 || (!state.has(e.to) && cycles(e.to)))) return true
    state.set(id, 2)
    return false
  }
  return ![...rest].some((x) => !state.has(x) && cycles(x))
}

const boundBy = (spec: Spec, id: string) =>
  Object.entries(spec.nodes).find(([, n]) => n.kind === "approval" && [n.binds ?? []].flat().includes(id))?.[0]

/** A write beyond the step's `writes_to` scopes. */
export const writes = (n: Node) =>
  (n.kind === "action" && toolWrites(n.tool) && !(n.writes_to ?? []).some((s) => s.tool === n.tool)) || (n.kind === "agent" && n.writes === "allow")
const scopesOf = (n: Node) => (n.kind === "for_each" && n.do ? n.do : n).writes_to ?? []

/** Rule of Two classes per step and the grants the spec needs. */
export function safety(spec: Spec) {
  const classes: Record<string, RuleClass[]> = {}
  const tainted = new Set<string>()
  const ids = Object.keys(spec.nodes)
  const u0 = hasUntrustedTrigger(spec)
  // Taint flows along references until nothing changes.
  for (let changed = true; changed;) {
    changed = false
    for (const id of ids) {
      if (tainted.has(id)) continue
      const n = spec.nodes[id]!
      const refs = refsOf(n)
      const fromInputs = (u0 && refs.some((r) => r.startsWith("inputs") || r.startsWith("trigger"))) || refs.some(fromRuns)
      const fromSteps = refs.some((r) => {
        const sid = r.split(".")[1]!
        return r.startsWith("steps.") && (tainted.has(sid) || untrustedOut(spec.nodes[sid]))
      })
      const web = (n.builtins && n.builtins !== "none") || (n.do?.builtins && n.do.builtins !== "none") || (n.kind === "wait" && !!(n.event || n.poll))
      if (fromInputs || fromSteps || web) { tainted.add(id); changed = true }
    }
  }
  const grants: Grant[] = []
  for (const [id, n] of Object.entries(spec.nodes)) {
    const c: RuleClass[] = []
    if (tainted.has(id)) c.push("U")
    if (n.kind === "agent" || n.kind === "for_each" || (n.kind === "action" && !toolWrites(n.tool))) c.push("S")
    if (writes(n) || scopesOf(n).length) c.push("W")
    for (const s of scopesOf(n)) grants.push({ kind: "scoped_write", node: id, detail: s.tool })
    classes[id] = c
    if (writes(n) && !boundBy(spec, id)) grants.push({ kind: "unattended_write", node: id, detail: n.tool ?? "Agent writes" })
    const b = n.builtins ?? n.do?.builtins
    if (b && b !== "none") grants.push({ kind: "builtins", node: id, detail: b === "all" ? "All built-ins" : "Read built-ins" })
    if (n.kind === "code" && n.network === "on") grants.push({ kind: "code_network", node: id, detail: "Network on" })
    if (n.untrusted_waiver) grants.push({ kind: "untrusted_waiver", node: id, detail: n.untrusted_waiver })
  }
  for (const t of spec.triggers ?? []) if (t.kind === "webhook" && !t.verify) grants.push({ kind: "unverified_webhook", detail: t.id })
  const budget = Math.max(spec.defaults?.budget_usd ?? 0, spec.limits?.budget_usd ?? 0)
  if (budget > 5) grants.push({ kind: "budget_over_default", detail: `$${budget} a run` })
  return { classes, grants }
}

/** Problems on one node, errors first. */
export const problemsAt = (v: Validation | undefined, id: string) => [...(v?.errors ?? []), ...(v?.warnings ?? [])].filter((p) => p.node === id)

// ---------- Diff ----------

const TOP: (keyof Spec)[] = ["name", "goal", "home", "visibility", "label", "inputs", "triggers", "concurrency", "defaults", "limits", "start"]
const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b)

export function diff(base: Spec | null, next: Spec): Change[] {
  if (!base) return Object.keys(next.nodes).map((node) => ({ node, effect: "added" as const }))
  const out: Change[] = TOP.filter((k) => !same(base[k], next[k])).map((field) => ({ node: null, field, effect: "changed" as const }))
  for (const [id, n] of Object.entries(next.nodes)) {
    if (!base.nodes[id]) out.push({ node: id, effect: "added" })
    else if (!same(base.nodes[id], n)) out.push({ node: id, effect: "changed" })
  }
  for (const id of Object.keys(base.nodes)) if (!next.nodes[id]) out.push({ node: id, effect: "removed" })
  return out
}

// ---------- Edits ----------

export const clone = <T>(x: T): T => structuredClone(x)

export function newId(spec: Spec, base: string) {
  if (!spec.nodes[base]) return base
  for (let i = 2; ; i++) if (!spec.nodes[`${base}_${i}`]) return `${base}_${i}`
}

const DEFAULT_ID: Record<Kind, string> = {
  agent: "agent", branch: "branch", transform: "transform", code: "code", action: "action", approval: "approval",
  wait: "wait", for_each: "each", workflow: "call", post: "post", finish: "done",
}

/** `ask`: an approval that takes a free-text answer from someone and carries on. */
export type Preset = "ask"

export function blankNode(kind: Kind, next?: string, preset?: Preset): Node {
  switch (kind) {
    case "agent": return { kind, title: "New agent step", instructions: "", next }
    case "branch": return { kind, title: "New branch", cases: [], else: next }
    case "transform": return { kind, title: "New transform", set: {}, next }
    case "code": return { kind, title: "New code step", language: "python", source: "", network: "off", next }
    case "action": return { kind, title: "New action", tool: "", args: {}, next }
    case "approval":
      if (preset === "ask") return { kind, title: "Ask a person", approvers: ["owner"], deliver: "dm", answer: "text", next }
      return { kind, title: "Approve", approvers: ["owner"], routes: next ? { approve: next, reject: next } : {} }
    case "wait": return { kind, title: "Wait", for: "1h", next }
    case "for_each": return { kind, title: "For each", items: "", as: "item", do: { kind: "agent", instructions: "" }, concurrency: 3, next }
    case "workflow": return { kind, title: "Run workflow", workflow: "", version: "live", next }
    case "post": return { kind, title: "Post", to: "home", message: "", next }
    case "finish": return { kind, title: "Finish", outcome: "succeeded" }
  }
}

/** Point the slot of `from` named `slot` at `to`. */
function setSlot(n: Node, slot: string, to: string) {
  const [key, k] = slot.split(".") as [string, string | undefined]
  if (key === "next") {
    if (Array.isArray(n.next)) n.next[Number(k)] = to
    else n.next = to
  } else if (key === "routes") {
    const r = n.routes![k!]!
    n.routes![k!] = typeof r === "string" ? to : { ...r, to }
  } else if (key === "cases") n.cases![Number(k)]!.to = to
  else (n as Record<string, unknown>)[key] = to
}

/** Insert a `kind` node on an edge (or after a leaf, or before the start) and return the new spec and id. */
export function insertNode(spec: Spec, at: { from: string | null; slot?: string; to?: string }, kind: Kind, preset?: Preset) {
  const s = clone(spec)
  const id = newId(s, DEFAULT_ID[kind])
  s.nodes[id] = blankNode(kind, at.to, preset)
  if (at.from == null) s.start = id
  else if (at.slot) setSlot(s.nodes[at.from]!, at.slot, id)
  else s.nodes[at.from]!.next = id
  return { spec: s, id }
}

/** Change how an approval is answered: the way out follows (routes for approve and choice, one next for text and form). */
export function setAnswer(n: Node, shape: AnswerShape) {
  const routes = Object.values(n.routes ?? {}).map((r) => (typeof r === "string" ? r : r.to))
  const first = typeof n.next === "string" ? n.next : routes[0]
  const to = (i: number) => routes[i] ?? first
  const pair = (a: string, b: string) => Object.fromEntries([[a, to(0)], [b, to(1)]].filter(([, t]) => t))
  n.answer = shape === "approve" ? undefined : shape
  if (shape === "text" || shape === "form") { n.routes = undefined; n.next = first; return }
  n.next = undefined
  if (shape === "approve") n.routes = pair("approve", "reject")
  else if (Object.keys(n.routes ?? {}).length < 2 || (n.routes && "approve" in n.routes)) n.routes = pair("option_1", "option_2")
}

export function removeNode(spec: Spec, id: string) {
  const s = clone(spec)
  const n = s.nodes[id]!
  const only = edgesOf(id, n).length === 1 ? edgesOf(id, n)[0]!.to : undefined
  delete s.nodes[id]
  for (const [fid, f] of Object.entries(s.nodes)) for (const e of edgesOf(fid, f)) if (e.to === id && only) setSlot(f, e.slot, only)
  if (s.start === id && only) s.start = only
  return s
}

export function duplicateNode(spec: Spec, id: string) {
  const s = clone(spec)
  const copy = newId(s, id)
  const n = s.nodes[id]!
  const out = edgesOf(id, n)
  s.nodes[copy] = { ...clone(n), title: `${titleOf(id, n)} copy` }
  // A single next: the copy runs after the original.
  if (out.length === 1 && out[0]!.kind === "next") n.next = copy
  return { spec: s, id: copy }
}

export function renameNode(spec: Spec, from: string, to: string) {
  if (from === to || spec.nodes[to] || !/^[a-z][a-z0-9_]*$/.test(to)) return null
  const text = JSON.stringify(spec)
    .replaceAll(`"${from}"`, `"${to}"`)
    .replaceAll(`steps.${from}.`, `steps.${to}.`)
    .replaceAll(`steps.${from} `, `steps.${to} `)
  return JSON.parse(text) as Spec
}
