// Ask a person admin: limits, menus, which menu each place uses and the request channel, as `/api/support*` serves them.
import { http, HttpResponse, type HttpHandler } from "msw"

import type { ChannelChange, Limit, LimitChange, LimitValue, Menu, MenuChange, MenuUseChange, Scope, SupportPage, Undelivered } from "@/modules/admin/support/types"
import { overridden, supportFlag } from "./features"
import { forbidden, wait } from "./util"
import { me } from "./world"

const AGENTS = [{ id: "daimon", name: "Daimon" }, { id: "clv-desk", name: "CLV Desk" }]
const CHANNELS = [{ id: "c_general", name: "general" }, { id: "c_clv", name: "clv-models" }, { id: "c_questions", name: "questions" }, { id: "c_mmm", name: "mmm" }]
const NAMES: Record<string, string> = Object.fromEntries([...AGENTS, ...CHANNELS].map((p) => [p.id, p.name]))
const PEOPLE: Record<string, string> = { p_priya: "Priya Raman", p_aiko: "Aiko Tanaka", p_owen: "Owen Brooks" }
/** Everyone the chat has seen, by platform user id: web people and those who never signed in. */
const CHAT: { id: string; name: string; handle: string | null }[] = [
  { id: "1100000000000000011", name: "Priya Raman", handle: "priya" },
  { id: "1100000000000000012", name: "Aiko Tanaka", handle: "aiko.t" },
  { id: "1100000000000000013", name: "Marcus Webb", handle: "marcus_w" },
  { id: "1100000000000000014", name: "Dana Okafor", handle: "dana" },
  { id: "1100000000000000015", name: "Dario Lenz", handle: "dlenz" },
]

const stale = () => HttpResponse.json({ detail: "This changed since you opened it. Reload and try again." }, { status: 409 })
const missing = (detail: string) => HttpResponse.json({ detail }, { status: 404 })
/** A refusal as the server words it: a list of `{ loc, msg }`, `loc` starting at the body key. */
const refuse = (problems: { loc: (string | number)[]; msg: string }[]) => HttpResponse.json({ detail: problems }, { status: 422 })
const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b)

type Problem = { loc: (string | number)[]; msg: string }

/** A decimal as the server reads it: a number with at most two decimals, from `low` to `high`. */
function credits(v: unknown, low: number, high: number, what = `from ${low} to ${high}`): string | null {
  const text = typeof v === "string" ? v.trim() : ""
  if (!/^\d+(\.\d*)?$/.test(text)) return "Input should be a number of credits"
  if ((text.split(".")[1] ?? "").length > 2) return "Input should have at most two decimal places"
  return Number(text) < low || Number(text) > high ? `Input should be ${what}` : null
}
const price = (v: unknown) => credits(v, 0.01, 1000, "more than 0 and at most 1000")

const str = (v: unknown, max: number): string | null =>
  typeof v !== "string" || !v.trim() ? "String should have at least 1 character" : v.trim().length > max ? `String should have at most ${max} characters` : null
const dup = (list: unknown[]) => new Set(list).size !== list.length
const bag = (v: unknown): Record<string, unknown> => (v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : {})
const list = (v: unknown): unknown[] => (Array.isArray(v) ? v : [])

/** The menu rules of `daimon.core.support`, answered as the server's pydantic errors read. */
function menuProblems(c: Record<string, unknown>): Problem[] {
  const out: Problem[] = []
  const add = (loc: (string | number)[], msg: string | null) => msg && out.push({ loc: ["menu", ...loc], msg })
  add(["name"], str(c.name, 75))
  const options = list(c.options)
  if (!options.length) add(["options"], "Tuple should have at least 1 item after validation, not 0")
  if (options.length > 10) add(["options"], `Tuple should have at most 10 items after validation, not ${options.length}`)
  options.forEach((raw, i) => {
    const o = bag(raw), p = bag(o.price), tag = String(p.kind), at = ["options", i]
    add([...at, "key"], /^[a-z0-9_]{1,32}$/.test(String(o.key)) ? null : "String should match pattern '^[a-z0-9_]{1,32}$'")
    add([...at, "label"], str(o.label, 75))
    if (tag === "fixed") add([...at, "price", "fixed", "amount"], price(p.amount))
    else if (tag === "range") {
      add([...at, "price", "range", "min"], price(p.min))
      add([...at, "price", "range", "max"], price(p.max))
      if (Number(p.min) >= Number(p.max)) add([...at, "price", "range"], "a range needs min below max")
    } else if (tag === "quoted") {
      if (p.min != null) add([...at, "price", "quoted", "min"], credits(p.min, 0, 1000, "0 and at most 1000"))
      if (p.max != null) add([...at, "price", "quoted", "max"], price(p.max))
    } else if (tag === "steps") {
      add([...at, "price", "steps", "label"], str(p.label, 45))
      const steps = list(p.steps).map(bag)
      if (steps.length < 2) add([...at, "price", "steps", "steps"], `Tuple should have at least 2 items after validation, not ${steps.length}`)
      steps.forEach((s, j) => { add([...at, "price", "steps", "steps", j, "label"], str(s.label, 75)); add([...at, "price", "steps", "steps", j, "amount"], price(s.amount)) })
      if (dup(steps.map((s) => s.label))) add([...at, "price", "steps"], "step labels must be different")
    } else add([...at, "price"], `Input tag '${tag}' found using 'kind' does not match any of the expected tags: 'fixed', 'steps', 'range', 'quoted'`)
    const fields = list(o.fields).map(bag)
    fields.forEach((f, k) => {
      const q = [...at, "fields", k], kind = String(f.kind)
      add([...q, kind, "key"], /^[a-z0-9_]{1,32}$/.test(String(f.key)) ? null : "String should match pattern '^[a-z0-9_]{1,32}$'")
      add([...q, kind, "label"], str(f.label, 45))
      const cap = kind === "text" ? 200 : 3000
      if ((kind === "text" || kind === "paragraph") && f.max_chars != null && !(Number.isInteger(Number(f.max_chars)) && Number(f.max_chars) >= 1 && Number(f.max_chars) <= cap))
        add([...q, kind, "max_chars"], `Input should be less than or equal to ${cap}`)
      if (kind === "choice") {
        const choices = list(f.choices)
        if (choices.length < 2) add([...q, "choice", "choices"], `Tuple should have at least 2 items after validation, not ${choices.length}`)
        else if (dup(choices)) add([...q, "choice"], "choices must be different")
      }
    })
    if (fields.length + (tag === "steps" ? 1 : 0) > 4) add(at, "an option takes at most 4 fields (a steps price is one)")
    else if (dup(fields.map((f) => f.key))) add(at, "field keys must be different")
  })
  if (dup(options.map((o) => bag(o).key))) add([], "option keys must be different")
  return out
}

const word = (n: string) => `${n} ${Number(n) === 1 ? "credit" : "credits"}`
/** A price in a few words: `0.5 credits`, `1-3 credits`, `Quoted later`. */
function priceText(p: Record<string, unknown>): string {
  if (p.kind === "fixed") return word(String(p.amount))
  if (p.kind === "quoted") return p.max == null ? "Quoted later" : `Quoted later, up to ${word(String(p.max))}`
  const amounts = list(p.steps).map((s) => Number(bag(s).amount))
  const [low, high] = p.kind === "steps" ? [Math.min(...amounts), Math.max(...amounts)] : [Number(p.min), Number(p.max)]
  return low === high ? word(String(low)) : `${low}-${high} credits`
}
const summary = (body: Record<string, unknown>) => list(body.options).map((o) => `${bag(o).label} (${priceText(bag(bag(o).price))})`).join(", ")

const menuOf = (key: string, body: Record<string, unknown>, revision = 1): Omit<Menu, "used_in"> =>
  ({ key, name: String(body.name ?? key), revision, body, summary: summary(body), options: list(body.options).length })
let menus: Omit<Menu, "used_in">[] = [
  menuOf("support", { name: "Support", options: [
    { key: "review", label: "Review this thread", price: { kind: "range", min: "1", max: "3" } },
    { key: "coaching", label: "Live coaching", price: { kind: "steps", label: "Length", steps: [{ label: "30 minutes", amount: "2" }, { label: "1 hour", amount: "4" }] }, fields: [{ key: "slots", label: "Time slots", kind: "paragraph" }] },
    { key: "build", label: "Build it", price: { kind: "quoted", max: "20" } },
  ] }, 3),
  menuOf("quick", { name: "Quick help", options: [{ key: "look", label: "Take a look", price: { kind: "fixed", amount: "0.5" } }] }),
]
let uses: Record<string, string | null> = { "workspace|": "support", "channel|c_general": "quick" }

const DAY = 86_400_000
const ago = (days: number) => new Date(Date.now() - days * DAY).toISOString().slice(0, 10) + "T00:00:00Z"
const stateOf = (v: LimitValue): Limit["state"] => (v.starts_at && Date.parse(v.starts_at) > Date.now() ? "not_started" : v.ends_at && Date.parse(v.ends_at) <= Date.now() ? "ended" : "active")
/** A bare date is read as 00:00 UTC, as the server does. */
const instant = (v: string | null) => (v && /^\d{4}-\d{2}-\d{2}$/.test(v) ? `${v}T00:00:00Z` : v)

const RECENT = [
  { at: ago(1), person: "Priya Raman", credits: "2", what: "Live coaching, 1 hour" },
  { at: ago(3), person: "Aiko Tanaka", credits: "1.5", what: "Review this thread" },
  { at: ago(6), person: "Priya Raman", credits: "0.5", what: "Take a look" },
]
const row = (scope: Scope, scope_id: string, person_id: string | null, v: Partial<LimitValue>, used: string, revision = 1): Limit => {
  const value: LimitValue = { shared: null, each_person: null, window: "monthly", starts_at: null, ends_at: null, ...v }
  return { ...value, scope, scope_id, name: scope === "workspace" ? null : (NAMES[scope_id] ?? null), person_id, person_name: person_id ? (PEOPLE[person_id] ?? CHAT.find((c) => c.id === person_id)?.name ?? null) : null, used, state: stateOf(value), revision, recent: Number(used) ? RECENT : [] }
}
let limits: Limit[] = [
  row("workspace", "", null, { shared: "200", each_person: "20" }, "64.5", 2),
  row("agent", "daimon", null, { each_person: "10", window: "total" }, "12"),
  row("channel", "c_general", null, { shared: "50", each_person: "5" }, "46", 4),
  row("channel", "c_clv", null, { shared: "30", window: "total", starts_at: ago(60), ends_at: ago(5) }, "22.5"),
  row("workspace", "", "1100000000000000011", { each_person: "40" }, "7.5"),
  row("channel", "c_clv", "1100000000000000012", { each_person: "3", ends_at: new Date(Date.now() + 21 * DAY).toISOString().slice(0, 10) + "T00:00:00Z" }, "1.5"),
  row("agent", "clv-desk", null, { shared: "15", window: "total", starts_at: new Date(Date.now() + 9 * DAY).toISOString().slice(0, 10) + "T00:00:00Z" }, "0"),
]
/** `?support=undelivered` shows requests no channel received and nobody handled; a charged one was paid at submit. */
let lost: Undelivered[] = [
  { id: "r_1", at: ago(0.2), person: "Priya Raman", credits: "2", what: "Live coaching, 1 hour", charged: true, price: { kind: "fixed", min: "2", max: null } },
  { id: "r_2", at: ago(2), person: "Aiko Tanaka", credits: "3", what: "Review this thread", charged: false, price: { kind: "range", min: "1", max: "3" } },
  { id: "r_3", at: ago(4), person: "Owen Brooks", credits: "20", what: "Build it", charged: false, price: { kind: "quoted", min: "0", max: "20" } },
]
let channel = { id: "c_questions", name: "questions", revision: 2 as number | null }

const posting = () => {
  const flag = supportFlag()
  return flag === "blocked" ? { state: "blocked" as const, detail: "Give Daimon Send Messages and Embed Links in #questions." } : flag === "unverified" ? { state: "unverified" as const, detail: "Couldn't check. Try again." } : { state: "ready" as const, detail: null }
}

const page = (): SupportPage => {
  const own = supportFlag() !== "locked"
  const none = supportFlag() === "unmet"
  const picked = own && !none ? channel : null
  return {
    platform: "discord", undelivered: supportFlag() === "undelivered" ? lost : [], everyone: { each_person: "20" }, limits,
    menus: menus.map((m) => ({ ...m, used_in: Object.entries(uses).filter(([, k]) => k === m.key).map(([at]) => { const [scope, id] = at.split("|") as [Scope, string]; return { scope, scope_id: id, name: scope === "workspace" ? "Workspace" : (NAMES[id] ?? id) } }) })),
    menu_use: [
      { scope: "workspace", scope_id: "", name: "Workspace", menu: uses["workspace|"] ?? null },
      ...AGENTS.map((a) => ({ scope: "agent" as const, scope_id: a.id, name: a.name, menu: uses[`agent|${a.id}`] ?? null })),
      ...CHANNELS.map((c) => ({ scope: "channel" as const, scope_id: c.id, name: c.name, menu: uses[`channel|${c.id}`] ?? null })),
    ],
    places: overridden("support_escalation").map((p) => ({ scope: p.scope as "agent" | "channel", id: p.id, name: NAMES[p.id] ?? p.id })),
    agents: AGENTS,
    channel: {
      own_allowed: own, value: picked && { id: picked.id, name: picked.name }, default: { name: "general" },
      ...(none ? { state: "none" as const, detail: "Pick a support channel." } : posting()),
      open: { count: none ? 0 : 3, where: none ? [] : [...new Set([(picked ?? { name: "general" }).name, "questions"])] }, revision: picked?.revision ?? null,
    },
  }
}
const ok = () => HttpResponse.json<SupportPage>(page())
const known = (scope: Scope, id: string) => scope === "workspace" ? id === "" : (scope === "agent" ? AGENTS : CHANNELS).some((p) => p.id === id)

function checkLimit(b: LimitChange): Problem[] {
  const out: Problem[] = []
  const v = b.limit!
  for (const k of ["shared", "each_person"] as const) if (v[k] != null) { const m = credits(v[k], 0, 100000); if (m) out.push({ loc: ["limit", k], msg: m }) }
  if (v.shared == null && v.each_person == null) out.push({ loc: ["limit"], msg: "Set a shared limit, a limit for each person, or both" })
  if (b.person_id && v.shared != null) out.push({ loc: ["limit", "shared"], msg: "A person's limit has no shared part" })
  if (b.person_id && v.each_person == null) out.push({ loc: ["limit", "each_person"], msg: "Field required" })
  if (v.starts_at && v.ends_at && Date.parse(instant(v.ends_at)!) <= Date.parse(instant(v.starts_at)!)) out.push({ loc: ["limit", "ends_at"], msg: "The end must be after the start" })
  return out
}

export const supportHandlers: HttpHandler[] = [
  http.all("/api/support/*", async () => { await wait(); if (!me()?.capabilities.includes("workspace.manage")) return forbidden() }),
  http.get("/api/support", async () => { await wait(); return me()?.capabilities.includes("workspace.manage") ? ok() : forbidden() }),
  http.get("/api/support/people", async ({ request }) => {
    const q = (new URL(request.url).searchParams.get("q") ?? "").trim().toLowerCase()
    if (!q || q.length > 100) return refuse([{ loc: ["query", "q"], msg: "String should have at least 1 character" }])
    return HttpResponse.json(CHAT.filter((c) => `${c.name} ${c.handle ?? ""}`.toLowerCase().includes(q)).slice(0, 8))
  }),
  http.post("/api/support/requests/:id/settle", async ({ params, request }) => {
    const r = lost.find((x) => x.id === params.id)
    if (!r) return missing("No such request.")
    const { charge } = (await request.json()) as { charge: string }
    const low = Number(r.price.min), high = r.price.kind === "fixed" ? low : r.price.max == null ? 100000 : Number(r.price.max)
    const bad = credits(charge, low, high)
    if (bad) return refuse([{ loc: ["charge"], msg: bad }])
    lost = lost.filter((x) => x !== r)
    return ok()
  }),
  http.post("/api/support/requests/:id/decline", async ({ params }) => {
    const r = lost.find((x) => x.id === params.id)
    if (!r) return missing("No such request.")
    lost = lost.filter((x) => x !== r)
    return ok()
  }),
  http.put("/api/support/limits", async ({ request }) => {
    const b = (await request.json()) as LimitChange
    if (b.limit && !known(b.scope, b.scope_id)) return missing("No such place.")
    const at = limits.find((l) => l.scope === b.scope && l.scope_id === b.scope_id && l.person_id === b.person_id)
    if ((at?.revision ?? null) !== b.expected_revision) return stale()
    if (!b.limit) { limits = limits.filter((l) => l !== at); return ok() }
    const problems = checkLimit(b)
    if (problems.length) return refuse(problems)
    const next = row(b.scope, b.scope_id, b.person_id, { ...b.limit, starts_at: instant(b.limit.starts_at), ends_at: instant(b.limit.ends_at) }, at?.used ?? "0", (at?.revision ?? 0) + 1)
    limits = at ? limits.map((l) => (l === at ? next : l)) : [...limits, next]
    return ok()
  }),
  http.put("/api/support/menus/use", async ({ request }) => {
    const b = (await request.json()) as MenuUseChange
    if (b.menu && !known(b.scope, b.scope_id)) return missing("No such place.")
    const k = `${b.scope}|${b.scope_id}`
    if ((uses[k] ?? null) !== b.expected) return stale()
    if (b.menu && !menus.some((m) => m.key === b.menu)) return missing("No menu has that key.")
    uses = { ...uses, [k]: b.menu }
    return ok()
  }),
  http.put("/api/support/menus/:key", async ({ params, request }) => {
    const key = String(params.key).trim()
    const b = (await request.json()) as MenuChange
    if (!/^[a-z0-9-]{1,32}$/.test(key)) return refuse([{ loc: ["path", "key"], msg: "A menu key is 1-32 lowercase letters, digits or dashes" }])
    const at = menus.find((m) => m.key === key)
    if ((at?.revision ?? null) !== (b.expected_revision || null)) return stale()
    if (!b.menu) {
      if (!at) return missing("No menu has that key.")
      if (Object.values(uses).includes(key)) return refuse([{ loc: ["menu"], msg: "A place still uses this menu. Change that place first" }])
      menus = menus.filter((m) => m !== at)
      return ok()
    }
    const problems = menuProblems(b.menu)
    if (problems.length) return refuse(problems)
    const next = menuOf(key, b.menu, (at?.revision ?? 0) + 1)
    menus = at ? menus.map((m) => (m === at ? next : m)) : [...menus, next]
    return ok()
  }),
  http.put("/api/support/channel", async ({ request }) => {
    const b = (await request.json()) as ChannelChange
    if (supportFlag() === "locked") return forbidden("The deployment keeps its own request channel.")
    if (!same(channel.revision, b.expected_revision)) return stale()
    const found = CHANNELS.find((c) => c.id === b.channel_id)
    if (b.channel_id && !found) return missing("No such channel.")
    const now = posting()
    if (b.channel_id && now.state === "blocked") return refuse([{ loc: ["channel_id"], msg: now.detail }])
    channel = found ? { ...found, revision: (channel.revision ?? 0) + 1 } : { id: "", name: "", revision: null }
    return ok()
  }),
]
