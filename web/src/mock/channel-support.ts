// Support plans for Ask a person: plans, menus and own caps, as `/api/support-plans` serves them.
import { http, HttpResponse, type HttpHandler } from "msw"

import type { CapChange, MenuChange, PersonCap, PlanChange, PlanValue, SupportMenu, SupportPlan, SupportPlans } from "@/modules/channels/types"
import { forbidden, notFound, wait } from "./util"
import { me } from "./world"

const AGENTS = [{ id: "daimon", name: "Daimon" }, { id: "clv-desk", name: "CLV Desk" }]
const PLACES: Record<string, string> = { c_general: "general", c_clv: "clv-models", daimon: "Daimon", "clv-desk": "CLV Desk" }
const invalid = (detail: string) => HttpResponse.json({ detail }, { status: 422 })
const stale = () => HttpResponse.json({ detail: "This changed since you loaded it." }, { status: 409 })
const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b)
const amount = (v: string | null) => v == null || (/^\d+(\.\d+)?$/.test(v) && Number(v) >= 0)

const menu = (key: string, catalog: Record<string, unknown>, revision = 1): SupportMenu => {
  const options = Array.isArray(catalog.options) ? (catalog.options as { label?: string }[]) : []
  return { key, name: String(catalog.name ?? key), revision, summary: options.map((o) => o.label).join(", "), catalog }
}
let menus: SupportMenu[] = [menu("support", { name: "Support", options: [{ key: "review", label: "Review", price: { kind: "range", min: "1", max: "3" } }] })]
let plans: SupportPlan[] = []
let defaults: PersonCap[] = [{ platform_user_id: "u_ana", name: "Ana", credits: "5" }]

function planOf(v: PlanValue, allowances: PersonCap[] = []): SupportPlan {
  const used = v.pool ? `0 of ${v.pool} credits used` : "No shared credits"
  return { ...v, name: v.scope === "workspace" ? null : (PLACES[v.scope_id] ?? null), pool_used: "0", state: "active", summary: [used, v.per_person && `${v.per_person} per person`].filter(Boolean).join(", "), allowances }
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
    if (!amount(b.plan.pool) || !amount(b.plan.per_person)) return invalid("Credits must be a number, 0 or more.")
    if (b.plan.catalog && !menus.some((m) => m.key === b.plan!.catalog)) return invalid("No menu has that key.")
    const next = planOf({ scope: b.scope, scope_id: b.scope_id ?? "", ...b.plan }, at?.allowances)
    plans = at ? plans.map((p) => (p === at ? next : p)) : [...plans, next]
    return page()
  }),
  http.put("/api/support-plans/catalogs/:key", async ({ params, request }) => {
    const key = params.key as string
    const b = (await request.json()) as MenuChange
    const at = menus.find((m) => m.key === key)
    if ((at?.revision ?? null) !== (b.expected_revision || null)) return stale()
    if (!b.catalog) {
      if (plans.some((p) => p.catalog === key)) return invalid("A plan still uses this menu. Change that plan first.")
      menus = menus.filter((m) => m !== at)
      return page()
    }
    if (!/^[a-z0-9_-]+$/.test(key)) return invalid("A menu key uses lowercase letters, digits, - and _.")
    if (!Array.isArray(b.catalog.options) || !b.catalog.options.length) return invalid("A menu needs at least one option.")
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
    if (!amount(b.credits)) return invalid("Credits must be a number, 0 or more.")
    const next = caps.filter((c) => c !== had).concat(b.credits ? [{ platform_user_id: b.platform_user_id, name: had?.name ?? null, credits: b.credits }] : [])
    if (plan) plan.allowances = next
    else defaults = next
    return page()
  }),
]
