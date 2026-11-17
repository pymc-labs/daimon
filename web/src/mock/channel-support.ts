// Support plans for Ask a person: plans, menus and own caps, as `/api/support-plans` serves them.
import { http, HttpResponse, type HttpHandler } from "msw"

import type { CapChange, MenuChange, PersonCap, PlanChange, PlanValue, SupportMenu, SupportPlan, SupportPlans } from "@/modules/channels/types"
import { forbidden, notFound, wait } from "./util"
import { me } from "./world"

const AGENTS = [{ id: "daimon", name: "Daimon" }, { id: "clv-desk", name: "CLV Desk" }]
const PLACES: Record<string, string> = { c_general: "general", c_clv: "clv-models", daimon: "Daimon", "clv-desk": "CLV Desk" }
const invalid = (detail: string) => HttpResponse.json({ detail }, { status: 422 })
const stale = () => HttpResponse.json({ detail: "This changed since you opened it. Reload and try again." }, { status: 409 })
const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b)
const NO_MENU = "No menu has that key."
const KEY_RULE = "A catalog key is 1-32 lowercase letters, digits or dashes."

/** A decimal as the server reads it: a number with at most two decimals, from `low` to `high`. */
function credits(v: unknown, name: string, low: number, high: number, what = `from ${low} to ${high}`): string | null {
  const text = typeof v === "string" ? v.trim() : ""
  if (!/^\d+(\.\d*)?$/.test(text)) return `${name} must be a number of credits, got ${JSON.stringify(v)}`
  if ((text.split(".")[1] ?? "").length > 2) return `${name} must have at most two decimal places`
  return Number(text) < low || Number(text) > high ? `${name} must be ${what}` : null
}
const limit = (v: string | null, name: string) => (v == null ? null : credits(v, name, 0, 100000))
const price = (v: unknown, name = "amount") => credits(v, name, 0.01, 1000, "more than 0 and at most 1000")

const str = (v: unknown, max: number): string | null =>
  typeof v !== "string" || !v.trim() ? "String should have at least 1 character" : v.trim().length > max ? `String should have at most ${max} characters` : null
const dup = (list: unknown[]) => new Set(list).size !== list.length
const bag = (v: unknown): Record<string, unknown> => (v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : {})
const list = (v: unknown): unknown[] => (Array.isArray(v) ? v : [])

/** The catalog rules of `daimon.core.support_plans`, answered the way its errors read: "where: what". */
function catalogProblems(c: Record<string, unknown>): string[] {
  const out: string[] = []
  const add = (where: string, what: string | null) => what && out.push(where ? `${where}: ${what}` : what)
  add("name", str(c.name, 75))
  const options = list(c.options)
  if (!options.length) add("options", "Tuple should have at least 1 item after validation, not 0")
  if (options.length > 10) add("options", `Tuple should have at most 10 items after validation, not ${options.length}`)
  options.forEach((raw, i) => {
    const o = bag(raw), at = `options.${i}`, p = bag(o.price), tag = String(p.kind)
    add(`${at}.key`, /^[a-z0-9_]{1,32}$/.test(String(o.key)) ? null : "String should match pattern '^[a-z0-9_]{1,32}$'")
    add(`${at}.label`, str(o.label, 75))
    if (tag === "fixed") add(`${at}.price.fixed.amount`, price(p.amount))
    else if (tag === "range") {
      add(`${at}.price.range.min`, price(p.min, "min"))
      add(`${at}.price.range.max`, price(p.max))
      if (Number(p.min) >= Number(p.max)) add(`${at}.price.range`, "a range needs min below max")
    } else if (tag === "quoted") {
      if (p.min != null) add(`${at}.price.quoted.min`, credits(p.min, "min", 0, 1000, "0 and at most 1000"))
      if (p.max != null) add(`${at}.price.quoted.max`, price(p.max))
    } else if (tag === "steps") {
      add(`${at}.price.steps.label`, str(p.label, 45))
      const steps = list(p.steps).map(bag)
      if (steps.length < 2) add(`${at}.price.steps.steps`, `Tuple should have at least 2 items after validation, not ${steps.length}`)
      steps.forEach((s, j) => { add(`${at}.price.steps.steps.${j}.label`, str(s.label, 75)); add(`${at}.price.steps.steps.${j}.amount`, price(s.amount)) })
      if (dup(steps.map((s) => s.label))) add(`${at}.price.steps`, "step labels must be different")
    } else add(`${at}.price`, "Input tag '" + tag + "' found using 'kind' does not match any of the expected tags: 'fixed', 'steps', 'range', 'quoted'")
    const fields = list(o.fields).map(bag)
    fields.forEach((f, k) => {
      const q = `${at}.fields.${k}`, kind = String(f.kind)
      add(`${q}.${kind}.key`, /^[a-z0-9_]{1,32}$/.test(String(f.key)) ? null : "String should match pattern '^[a-z0-9_]{1,32}$'")
      add(`${q}.${kind}.label`, str(f.label, 45))
      const cap = kind === "text" ? 200 : 3000
      if ((kind === "text" || kind === "paragraph") && f.max_chars != null && !(Number.isInteger(Number(f.max_chars)) && Number(f.max_chars) >= 1 && Number(f.max_chars) <= cap))
        add(`${q}.${kind}.max_chars`, `Input should be less than or equal to ${cap}`)
      if (kind === "choice") {
        const choices = list(f.choices)
        if (choices.length < 2) add(`${q}.choice.choices`, `Tuple should have at least 2 items after validation, not ${choices.length}`)
        else if (dup(choices)) add(`${q}.choice`, "choices must be different")
      }
    })
    if (fields.length + (tag === "steps" ? 1 : 0) > 4) add(at, "an option takes at most 4 fields (a steps price is one)")
    else if (dup(fields.map((f) => f.key))) add(at, "field keys must be different")
  })
  if (dup(options.map((o) => bag(o).key))) add("", "option keys must be different")
  return out
}
const parseCatalog = (c: Record<string, unknown>) => {
  const problems = catalogProblems(c)
  return problems.length ? `Catalog not valid: ${problems.slice(0, 3).join("; ")}` : null
}

const word = (n: string) => `${n} ${Number(n) === 1 ? "credit" : "credits"}`
/** A price in a few words, as the server's summary says it: `0.5 credits`, `1-3 credits`, `Quoted`. */
function priceText(p: Record<string, unknown>): string {
  if (p.kind === "fixed") return word(String(p.amount))
  if (p.kind === "quoted") return p.max == null ? "Quoted" : `Quoted, up to ${word(String(p.max))}`
  const [low, high] = p.kind === "steps"
    ? [Math.min(...list(p.steps).map((s) => Number(bag(s).amount))), Math.max(...list(p.steps).map((s) => Number(bag(s).amount)))]
    : [Number(p.min), Number(p.max)]
  return low === high ? word(String(low)) : `${low}-${high} credits`
}
const menu = (key: string, catalog: Record<string, unknown>, revision = 1): SupportMenu => ({
  key, name: String(catalog.name ?? key), revision, catalog,
  summary: list(catalog.options).map((o) => `${bag(o).label} (${priceText(bag(bag(o).price))})`).join(", "),
})
let menus: SupportMenu[] = [menu("support", { name: "Support", options: [{ key: "review", label: "Review", price: { kind: "range", min: "1", max: "3" } }] })]
let plans: SupportPlan[] = []
let defaults: PersonCap[] = [{ platform_user_id: "u_ana", name: "Ana", credits: "5" }]

/** A bare date is read as 00:00 UTC, as the server does. */
const instant = (v: string | null) => (v && /^\d{4}-\d{2}-\d{2}$/.test(v) ? `${v}T00:00:00Z` : v)

function planOf(w: PlanValue, allowances: PersonCap[] = []): SupportPlan {
  const v = { ...w, starts_at: instant(w.starts_at), ends_at: instant(w.ends_at) }
  // The end is exclusive: the plan runs through the day before it.
  const last = v.window === "fixed" && v.ends_at ? new Date(Date.parse(v.ends_at) - 1) : null
  const end = last && `until ${last.getUTCDate()} ${last.toLocaleString("en", { month: "short", timeZone: "UTC" })}`
  const summary = [v.pool && `0 of ${word(v.pool)} used`, v.per_person && `${v.per_person} per person`, v.window === "monthly" && "this month", end].filter(Boolean).join(", ")
  return { ...v, name: v.scope === "workspace" ? null : (PLACES[v.scope_id] ?? null), pool_used: "0", state: "active", summary, allowances }
}
const valueOf = ({ scope, scope_id, catalog, pool, per_person, window, starts_at, ends_at }: SupportPlan): PlanValue => ({ scope, scope_id, catalog, pool, per_person, window, starts_at, ends_at })
const page = () => HttpResponse.json<SupportPlans>({ platform: "discord", default_credits: "20", default_allowances: defaults, plans, catalogs: menus, agents: AGENTS })

export const supportHandlers: HttpHandler[] = [
  ...["/api/support-plans", "/api/support-plans/*"].map((path) => http.all(path, async () => {
    await wait()
    if (!me()?.capabilities.includes("channels.manage")) return forbidden()
  })),
  http.get("/api/support-plans", page),
  http.put("/api/support-plans/plan", async ({ request }) => {
    const b = (await request.json()) as PlanChange
    const at = plans.find((p) => p.scope === b.scope && p.scope_id === (b.scope_id ?? ""))
    if (!same(at ? valueOf(at) : null, b.expected)) return stale()
    if (!b.plan) {
      plans = plans.filter((p) => p !== at)
      return page()
    }
    const bad = limit(b.plan.pool, "pool") ?? limit(b.plan.per_person, "per_person")
    if (bad) return invalid(bad)
    if (b.plan.pool == null && b.plan.per_person == null) return invalid("Set a pool, a per-person limit or both.")
    if (b.plan.catalog && !menus.some((m) => m.key === b.plan!.catalog)) return HttpResponse.json({ detail: NO_MENU }, { status: 404 })
    const next = planOf({ scope: b.scope, scope_id: b.scope_id ?? "", ...b.plan }, at?.allowances)
    plans = at ? plans.map((p) => (p === at ? next : p)) : [...plans, next]
    return page()
  }),
  http.put("/api/support-plans/catalogs/:key", async ({ params, request }) => {
    const key = String(params.key).trim()
    const b = (await request.json()) as MenuChange
    if (!/^[a-z0-9-]{1,32}$/.test(key)) return invalid(KEY_RULE)
    const at = menus.find((m) => m.key === key)
    if ((at?.revision ?? null) !== (b.expected_revision || null)) return stale()
    if (!b.catalog) {
      if (!at) return HttpResponse.json({ detail: NO_MENU }, { status: 404 })
      if (plans.some((p) => p.catalog === key)) return invalid("A plan still uses this menu. Change that plan first.")
      menus = menus.filter((m) => m !== at)
      return page()
    }
    const problem = parseCatalog(b.catalog)
    if (problem) return invalid(problem)
    const next = menu(key, b.catalog, (at?.revision ?? 0) + 1)
    menus = at ? menus.map((m) => (m === at ? next : m)) : [...menus, next]
    return page()
  }),
  http.put("/api/support-plans/allowances", async ({ request }) => {
    const b = (await request.json()) as CapChange
    const plan = b.scope ? plans.find((p) => p.scope === b.scope && p.scope_id === (b.scope_id ?? "")) : null
    if (b.scope && !plan) return notFound()
    const caps = plan ? plan.allowances : defaults
    const had = caps.find((c) => c.platform_user_id === b.platform_user_id)
    if ((had?.credits ?? null) !== b.expected) return stale()
    const bad = b.credits == null ? null : credits(b.credits, "credits", 0, 100000)
    if (bad) return invalid(bad)
    const next = caps.filter((c) => c !== had).concat(b.credits ? [{ platform_user_id: b.platform_user_id, name: had?.name ?? null, credits: b.credits }] : [])
    if (plan) plan.allowances = next
    else defaults = next
    return page()
  }),
]
