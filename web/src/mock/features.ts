// Features: the schema and a place's features, kept in memory. The real keys come from the server's registry.
import { http, HttpResponse, type HttpHandler } from "msw"

import type { FeaturesDoc, FeatureSchema, FeatureView, Tier } from "@/modules/features/types"
import { wait } from "./util"

const ON_OFF = [{ value: "on", label: "On" }, { value: "off", label: "Off" }]
const ALL: Tier[] = ["deployment", "workspace", "agent", "channel", "thread"]
const schema = (key: string, name: string, tiers: Tier[] = ALL): FeatureSchema => ({
  key, name, category: "chat", categoryName: "Chat", values: ON_OFF, default: "on", tiers, lockable: tiers.filter((t) => t === "workspace" || t === "channel"),
  platforms: ["discord", "slack", "teams"], threadAnyone: false, native: false, doc: "",
})
const SCHEMA = [
  schema("progress_plans", "Progress plans"), schema("participation", "Thread participation"),
  schema("guidance", "Setup guidance", ["deployment", "workspace", "agent"]), schema("tool.bash", "Built-in tool: bash"), schema("cost_footer", "Cost footer"),
]
/** Locked at the workspace, so every level below reads it as locked; `value` is what the lock holds. */
const locks = new Map<string, string>([["cost_footer", "off"]])
/** The one channel where the bash tool is unavailable (it has no sandbox), with a value of its own so the row shows. */
const NO_SANDBOX = "c_general"
const REASON = "Needs a sandbox. Ask an admin."
/** `place|tier|key` to the value set there. */
const own = new Map<string, string>([[`/${NO_SANDBOX}/|channel|tool.bash`, "off"], [`/${NO_SANDBOX}/t_launch|thread|tool.bash`, "off"]])
const place = (url: URL) => ["agent", "channel", "thread"].map((k) => url.searchParams.get(k) ?? "").join("/")
const tiersOf = (url: URL): Tier[] => ["workspace", ...(url.searchParams.get("agent") ? ["agent"] : []), ...(url.searchParams.get("channel") ? ["channel"] : []), ...(url.searchParams.get("thread") ? ["thread"] : [])] as Tier[]

function viewOf(url: URL, s: FeatureSchema): FeatureView {
  const lock = locks.get(s.key)
  const tiers = tiersOf(url).filter((t) => s.tiers.includes(t)).map((tier) => {
    if (tier === "workspace" && lock) return { tier, value: lock, locked: true, text: `${lock}:locked` }
    const text = own.get(`${place(url)}|${tier}|${s.key}`) ?? "default"
    return { tier, value: text.split(":")[0]!, locked: text.endsWith(":locked"), text }
  })
  const locked = tiers.find((t) => t.locked)
  const set = locked ?? [...tiers].reverse().find((t) => t.value !== "default")
  const value = set?.value ?? s.default
  const unavailable = s.key === "tool.bash" && url.searchParams.get("channel") === NO_SANDBOX ? REASON : null
  return { key: s.key, tiers, value, source: set?.tier ?? "default", locked: !!locked, unavailable, summary: ON_OFF.find((o) => o.value === value)?.label ?? value }
}

export const featuresHandlers: HttpHandler[] = [
  http.get("/api/features/schema", async () => { await wait(); return HttpResponse.json(SCHEMA) }),
  http.get("/api/features", async ({ request }) => {
    await wait()
    const url = new URL(request.url)
    return HttpResponse.json<FeaturesDoc>({ platform: "discord", features: SCHEMA.map((s) => viewOf(url, s)) })
  }),
  http.put("/api/features", async ({ request }) => {
    await wait()
    const url = new URL(request.url)
    const b = (await request.json()) as { key: string; tier: Tier; value: string; locked?: boolean }
    const s = SCHEMA.find((x) => x.key === b.key)
    if (!s) return HttpResponse.json({ detail: "No such feature." }, { status: 404 })
    if (b.tier === "workspace" && (b.locked || locks.has(b.key))) {
      if (b.locked) locks.set(b.key, b.value)
      else locks.delete(b.key)
    }
    own.set(`${place(url)}|${b.tier}|${b.key}`, b.locked ? `${b.value}:locked` : b.value)
    return HttpResponse.json(viewOf(url, s))
  }),
]
