// Routines: scheduled prompts. Admins see everyone's, members their own.
import { http, HttpResponse, type HttpHandler } from "msw"

import type { CatchUp, Choices, Destination, NewRoutine, Routine, RoutineChange } from "@/modules/routines/types"
import { fromCron } from "@/modules/routines/cron"
import { forbidden, notFound, unauthorized, wait } from "./util"
import { AGENTS, me } from "./world"

const hours = (h: number) => new Date(Date.now() + h * 3_600_000).toISOString()

const DESTINATIONS: Destination[] = [
  { id: "d_mmm", name: "mmm-larkspur" }, { id: "d_pricing", name: "pricing" }, { id: "d_eng", name: "eng-review" },
  { id: "d_promo", name: "Promo test", kind: "thread" as const },
]

type Stored = Omit<Routine, "title"> & { owner_id: string }
const titleOf = (name: string | null, message: string) => name || (message.split("\n")[0] ?? "").slice(0, 60)
const routines: Stored[] = [
  { id: "r1", name: "Weekly channel readout", owner_id: "p_maya", owner: "Maya Lindqvist", mine: false, agent: { id: "ag_mmm", name: "MMM Analyst" }, cron: "0 9 * * 1", timezone: "Europe/Stockholm",
    schedule: "every Monday at 09:00", message: "Summarize last week's channel contribution for Larkspur and flag any interval wider than 20%.",
    enabled: true, catch_up: "run-once", destination: DESTINATIONS[0]!, dm: false, sandbox_tools: true, next_run: hours(60), last_run: hours(-108), failed: false, last_output: "Search and social held steady; TV's interval widened to 24%." },
  { id: "r2", name: "Overnight price test", owner_id: "p_priya", owner: "Priya Raman", mine: false, agent: { id: "ag_pricing", name: "Pricing Analyst" }, cron: "30 8 * * 1-5", timezone: "America/New_York",
    schedule: "every weekday at 08:30", message: "Post the overnight price test readout.",
    enabled: true, catch_up: "skip", destination: DESTINATIONS[3]!, dm: false, sandbox_tools: false, next_run: hours(14), last_run: hours(-10), failed: true, last_output: null },
  { id: "r3", name: null, owner_id: "p_priya", owner: "Priya Raman", mine: false, agent: { id: "ag_review", name: "Code Reviewer" }, cron: "0 17 * * 5", timezone: "Europe/London",
    schedule: "every Friday at 17:00", message: "List pull requests open for more than a week.",
    enabled: false, catch_up: "skip", destination: null, dm: true, sandbox_tools: false, next_run: null, last_run: hours(-200), failed: false, last_output: null },
]

const view = (r: Stored): Routine => {
  const { owner_id, ...rest } = r
  return { ...rest, title: titleOf(r.name, r.message), mine: owner_id === me()?.id }
}
const visible = () => {
  const m = me()
  return m ? routines.filter((r) => m.role === "owner" || m.role === "admin" || r.owner_id === m.id) : []
}
const next = (cron: string) => (fromCron(cron).kind === "custom" ? hours(6) : hours(18))
const words = (cron: string, tz: string) => {
  const s = fromCron(cron)
  const at = s.time
  const text = { hourly: `every hour at minute ${s.time.slice(3)}`, daily: `every day at ${at}`, weekdays: `every weekday at ${at}`,
    weekly: `every week at ${at}`, monthly: `every month on day ${s.day} at ${at}`, custom: cron }[s.kind]
  return s.kind === "custom" ? text : `${text} ${tz}`
}
const dest = (id: string | null | undefined) => (id ? DESTINATIONS.find((d) => d.id === id) ?? null : null)
const invalid = (detail: string) => HttpResponse.json({ detail }, { status: 422 })

export const routinesHandlers: HttpHandler[] = [
  http.get("/api/routines", async () => {
    await wait()
    return me() ? HttpResponse.json(visible().map(view)) : unauthorized()
  }),
  http.get("/api/routines/choices", async () => {
    await wait(60)
    const m = me()
    if (!m) return unauthorized()
    const body: Choices = {
      agents: AGENTS.map((a) => ({ id: a.id, name: a.name })), destinations: DESTINATIONS,
      can_create: m.capabilities.includes("routines.manage"), can_dm: m.workspace.platform === "discord", platform: m.workspace.platform,
    }
    return HttpResponse.json(body)
  }),
  http.post("/api/routines", async ({ request }) => {
    await wait(300)
    const m = me()
    if (!m?.capabilities.includes("routines.manage")) return forbidden()
    const b = (await request.json()) as NewRoutine
    if (!b.cron.trim() || b.cron.trim().split(/\s+/).length !== 5) return invalid("That schedule or time zone isn't valid.")
    const a = AGENTS.find((x) => x.id === b.agent_id)
    if (!a) return invalid("Pick an agent from the list.")
    const r: Stored = {
      id: `r${Date.now()}`, name: b.name?.trim() || null, owner_id: m.id, owner: m.name, mine: true, agent: { id: a.id, name: a.name }, cron: b.cron, timezone: b.timezone,
      schedule: words(b.cron, b.timezone), message: b.message, enabled: true, catch_up: b.catch_up as CatchUp, destination: b.dm ? null : dest(b.destination), dm: !!b.dm, sandbox_tools: false,
      next_run: next(b.cron), last_run: null, failed: false, last_output: null,
    }
    routines.unshift(r)
    return HttpResponse.json(view(r))
  }),
  http.patch("/api/routines/:id", async ({ params, request }) => {
    await wait(250)
    const m = me()
    if (!m?.capabilities.includes("routines.manage")) return forbidden()
    const r = visible().find((x) => x.id === params.id)
    if (!r) return notFound()
    const b = (await request.json()) as RoutineChange
    if (b.agent_id) {
      const a = AGENTS.find((x) => x.id === b.agent_id)
      if (!a) return invalid("Pick an agent from the list.")
      r.agent = { id: a.id, name: a.name }
    }
    if (b.cron) r.cron = b.cron
    if (b.timezone) r.timezone = b.timezone
    if (b.cron || b.timezone) { r.schedule = words(r.cron, r.timezone); if (r.enabled) r.next_run = next(r.cron) }
    if (b.name !== undefined) r.name = b.name.trim() || null
    if (b.message) r.message = b.message
    if (b.catch_up) r.catch_up = b.catch_up
    if (b.clear_destination) { r.destination = null; r.dm = false }
    else if (b.dm) { r.destination = null; r.dm = true }
    else if (b.destination) { r.destination = dest(b.destination); r.dm = false }
    return HttpResponse.json(view(r))
  }),
  ...(["pause", "resume"] as const).map((verb) => http.post(`/api/routines/:id/${verb}`, async ({ params }) => {
    await wait(200)
    const m = me()
    if (!m?.capabilities.includes("routines.manage")) return forbidden()
    const r = visible().find((x) => x.id === params.id)
    if (!r) return notFound()
    r.enabled = verb === "resume"
    r.next_run = r.enabled ? next(r.cron) : null
    return HttpResponse.json(view(r))
  })),
  http.post("/api/routines/:id/run", async ({ params }) => {
    await wait(200)
    const m = me()
    if (!m?.capabilities.includes("routines.manage")) return forbidden()
    const r = visible().find((x) => x.id === params.id)
    if (!r) return notFound()
    if (!r.enabled) return HttpResponse.json({ detail: "This routine is paused. Resume it to run it." }, { status: 422 })
    r.next_run = new Date().toISOString()
    return HttpResponse.json(view(r))
  }),
  http.delete("/api/routines/:id", async ({ params }) => {
    await wait(200)
    const m = me()
    if (!m?.capabilities.includes("routines.manage")) return forbidden()
    const i = routines.findIndex((x) => x.id === params.id && visible().includes(x))
    if (i < 0) return notFound()
    routines.splice(i, 1)
    return new HttpResponse(null, { status: 204 })
  }),
]
