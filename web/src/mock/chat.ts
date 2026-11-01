// Mock API for the chat module: sessions, streamed turns, cards, app items and per-turn credit.
// Keywords in a message pick the scenario: "approve", "credential", "refuse"; anything else is a rich reply.
import { http, HttpResponse, type HttpHandler } from "msw"

import type { ChatSession } from "@/lib/types"
import type { Card, CardAnswer, FileOut, Msg, StreamEvent } from "@/modules/chat/types"
import { forbidden, notFound, paged, sleep, sse, unauthorized, wait } from "./util"
import { agent, AGENTS, me, session, SESSIONS, spend } from "./world"

const uid = () => crypto.randomUUID().slice(0, 8)

// Sessions and threads live in memory, seeded from world.ts.
const sessions: Record<string, ChatSession[]> = {}
const threads: Record<string, Msg[]> = {}
const owners: Record<string, string> = {}
const mine = (pid: string) => (sessions[pid] ??= (SESSIONS[pid] ?? []).map((s) => ({ ...s })))

const RICH_MMM = `Larkspur's blended ROAS rose to **2.9x** in Q3, up from 2.6x in Q2. Most of the lift came from paid search and connected TV. Paid social is saturating.

## Channel results

| Channel | Spend | Contribution | ROAS | 94% interval |
|---|---|---|---|---|
| Paid search | $412k | 31% | 3.8x | 3.3 to 4.4 |
| Connected TV | $268k | 22% | 3.1x | 2.4 to 3.9 |
| Paid social | $355k | 19% | 2.1x | 1.7 to 2.5 |
| Podcasts | $74k | 6% | 2.4x | 1.1 to 3.9 |
| Display | $120k | 4% | 1.2x | 0.8 to 1.6 |

## Where to shift budget

- **Move about $60k** from paid social to connected TV. Social's response curve flattens above roughly $95k a month.
- **Hold search.** It is still on the steep part of its curve, but brand terms are already capped.
- **Test podcasts once more.** The interval is wide, so run one more flight with a larger holdout before scaling.

I attached the full channel table.`

const RICH_PRICING = `At **$34** the unlimited plan converts 9% better than at $29 and brings in **11% more revenue per visitor**. Elasticity is about **-1.4**, so a rise to $39 loses more in volume than it gains.

## Test results

| Price | Visitors | Conversion | Revenue per visitor |
|---|---|---|---|
| $29 | 6,210 | 4.1% | $1.19 |
| $34 | 6,140 | 3.9% | $1.33 |
| $39 | 6,070 | 3.0% | $1.17 |

## Reading

- **$34 is the best of the three.** The gap to $29 is outside the noise (interval on revenue per visitor: +4% to +18%).
- **$39 does not pay.** Conversion drops 23% against $34 for a 15% higher price.
- **Check mobile.** The drop at $39 is concentrated on mobile, which is worth a second test.

I attached the summary table.`

const RICH_GENERAL = `Spend this week is **$142.60**, 12% under last week. Most of it went to two agents, and one routine accounts for a fifth of the total.

| Agent | Turns | Spend |
|---|---|---|
| MMM Analyst | 38 | $61.20 |
| Daimon | 214 | $44.85 |
| Pricing Analyst | 29 | $21.40 |
| Code Reviewer | 17 | $15.15 |

The nightly Larkspur refresh is the single largest routine at $28.90 a week. Switching it to Haiku would cut that by about two thirds.`

const FOLLOW_UP = `Podcasts and display have the widest intervals. Podcasts span 1.1x to 3.9x because the holdout was small. Display is wide for a different reason: spend barely moved in Q3, so the model has little to learn from.

Search and connected TV are the tightest, both under 1.5 points wide.`

const lastWord = (s: string) => s.replace(/[.?!]+$/, "")
const files = (kind: "mmm" | "pricing" | "general"): FileOut[] =>
  kind === "mmm" ? [{ id: "f_larkspur", name: "larkspur_q3_channel_roi.csv", size: 4_812, kind: "csv" }]
    : kind === "pricing" ? [{ id: "f_pricing", name: "unlimited_plan_price_test.csv", size: 2_304, kind: "csv" }] : []

function seedThread(s: ChatSession): Msg[] {
  const t = new Date(s.updated_at).getTime()
  const at = (min: number) => new Date(t - min * 60_000).toISOString()
  if (s.id === "s_q3") {
    return [
      { id: "m_q3_1", role: "user", content: "Summarize the Q3 media mix readout for Larkspur and flag where we should shift budget.", created_at: at(22) },
      {
        id: "m_q3_2", role: "assistant", content: RICH_MMM, created_at: at(21),
        steps: [
          { id: "a", label: "Searched Almanac", detail: "Larkspur Q3 media mix · 6 results", status: "done", ms: 920 },
          { id: "b", label: "Queried warehouse", detail: "mmm_results, channel_roi · 214 rows", status: "done", ms: 1_340 },
          { id: "c", label: "Summarized model run", detail: "larkspur_q3_v4 · 4 chains, r-hat 1.00", status: "done", ms: 780 },
        ],
        files: files("mmm"), cost_usd: 0.46, ms: 14_200, feedback: "up",
      },
      { id: "m_q3_3", role: "user", content: "Which channels have the widest intervals?", created_at: at(12) },
      {
        id: "m_q3_4", role: "assistant", content: FOLLOW_UP, created_at: at(11),
        steps: [{ id: "a", label: "Summarized model run", detail: "larkspur_q3_v4 · intervals", status: "done", ms: 610 }],
        cost_usd: 0.09, ms: 5_600, feedback: null,
      },
    ]
  }
  if (s.id === "s_vis1") {
    return [
      { id: "m_v1", role: "user", content: "Price test for the unlimited plan", created_at: at(40) },
      {
        id: "m_v2", role: "assistant", content: RICH_PRICING, created_at: at(39), files: files("pricing"), cost_usd: 0.54, ms: 11_800, feedback: null,
        steps: [
          { id: "a", label: "Queried experiments", detail: "unlimited_plan_price_test · 18,420 sessions", status: "done", ms: 1_020 },
          { id: "b", label: "Fit elasticity model", detail: "log-log, 3 price points", status: "done", ms: 1_480 },
        ],
      },
      { id: "m_v3", role: "user", content: "Does the result hold on mobile only?", created_at: at(20) },
      {
        id: "m_v4", role: "assistant", cost_usd: 0.61, ms: 9_300, feedback: null, created_at: at(19),
        content: "On mobile alone the drop at $39 is steeper: conversion falls to 2.4%, a 31% loss against $34. Desktop is nearly flat across all three prices.\n\nThe sample is smaller (3,900 mobile sessions), so treat $34 as the safe choice and rerun the $39 arm on desktop only.",
        steps: [{ id: "a", label: "Queried experiments", detail: "mobile segment · 3,900 sessions", status: "done", ms: 880 }],
      },
    ]
  }
  return [
    { id: `m_${s.id}_1`, role: "user", content: s.title, created_at: at(8) },
    {
      id: `m_${s.id}_2`, role: "assistant", created_at: at(7), cost_usd: 0.12, ms: 6_400, feedback: null,
      content: "Here is a first pass. I pulled the relevant notes from Almanac and kept the open questions at the end so you can answer them in one go.",
      steps: [{ id: "a", label: "Searched Almanac", detail: `${s.title} · 4 results`, status: "done", ms: 840 }],
    },
  ]
}
function thread(sid: string) {
  if (!threads[sid]) {
    const s = Object.values(sessions).flat().find((x) => x.id === sid) ?? Object.values(SESSIONS).flat().find((x) => x.id === sid)
    if (!s) return null
    threads[sid] = seedThread(s)
  }
  return threads[sid]!
}
const find = (pid: string, sid: string) => mine(pid).find((s) => s.id === sid)

type Kind = "mmm" | "pricing" | "general"
const kindOf = (id: string): Kind => (id === "ag_mmm" ? "mmm" : id === "ag_pricing" ? "pricing" : "general")
const scenarioOf = (t: string) =>
  /refus|restricted|salary|confidential/i.test(t) ? "refusal"
    : /credential|api key|token|connect|hubspot/i.test(t) ? "credential"
      : /approv|send|email|publish/i.test(t) ? "approval" : "rich"

// Streaming helpers: the turn is a generator of events; the wrapper keeps the saved message in step.
async function* step(id: string, label: string, detail: string, ms: number): AsyncGenerator<StreamEvent> {
  yield { type: "step", step: { id, label, detail, status: "running" } }
  await sleep(ms)
  yield { type: "step", step: { id, label, detail, status: "done", ms } }
}
async function* type(text: string): AsyncGenerator<StreamEvent> {
  const words = text.match(/\S+\s*|\n+/g) ?? []
  for (let i = 0; i < words.length; i += 3) {
    yield { type: "delta", text: words.slice(i, i + 3).join("") }
    await sleep(34)
  }
}

const stopping = new Set<string>()

function turn(pid: string, sid: string, build: (m: Msg) => AsyncGenerator<StreamEvent>, cost: number) {
  const list = thread(sid)!
  const s = find(pid, sid)!
  const msg: Msg = { id: `m_${uid()}`, role: "assistant", content: "", steps: [], created_at: new Date().toISOString(), feedback: null }
  const t0 = Date.now()
  s.running = true
  stopping.delete(sid)
  return sse(async function* () {
    let ok = false
    try {
      for await (const e of build(msg)) {
        if (stopping.has(sid)) {
          msg.stopped = true
          msg.steps = msg.steps?.map((x) => (x.status === "running" ? { ...x, status: "error" as const } : x))
          break
        }
        if (e.type === "step") {
          const i = msg.steps!.findIndex((x) => x.id === e.step.id)
          if (i >= 0) msg.steps![i] = e.step
          else msg.steps!.push(e.step)
        } else if (e.type === "delta") msg.content += e.text
        else if (e.type === "card") msg.card = e.card
        yield e
      }
      ok = true
      msg.ms = Date.now() - t0
      msg.cost_usd = msg.stopped ? 0.03 : cost
      spend(pid, msg.cost_usd)
      list.push(msg)
      s.updated_at = new Date().toISOString()
      yield { type: "done", message: msg }
    } finally {
      s.running = false
      if (!ok) {
        msg.stopped = true
        msg.ms = Date.now() - t0
        msg.cost_usd = 0.03
        spend(pid, 0.03)
        list.push(msg)
      }
    }
  })
}

async function* rich(kind: Kind, msg: Msg): AsyncGenerator<StreamEvent> {
  if (kind === "mmm") {
    yield* step("a", "Searched Almanac", "Larkspur Q3 media mix · 6 results", 900)
    yield* step("b", "Queried warehouse", "mmm_results, channel_roi · 214 rows", 1_100)
    yield* step("c", "Summarized model run", "larkspur_q3_v4 · 4 chains, r-hat 1.00", 800)
    yield* type(RICH_MMM)
  } else if (kind === "pricing") {
    yield* step("a", "Queried experiments", "unlimited_plan_price_test · 18,420 sessions", 1_000)
    yield* step("b", "Fit elasticity model", "log-log, 3 price points", 1_200)
    yield* type(RICH_PRICING)
  } else {
    yield* step("a", "Searched Almanac", "spend by agent · 4 results", 900)
    yield* type(RICH_GENERAL)
  }
  msg.files = files(kind)
}

const APPROVAL: Card = {
  id: "", kind: "approval", state: "pending", always_allowed: true,
  title: 'Send email "Larkspur Q3 media mix readout"?',
  consequence: "It goes out from your Microsoft account.",
  details: [
    "To: priya.raman@fernhill.example, sam@larkspur.example",
    "Subject: Larkspur Q3 media mix readout",
    "Message: Blended ROAS rose to 2.9x in Q3. Shift about $60k from paid social to connected TV and rerun the podcast flight with a larger holdout.",
  ],
}

function scripted(pid: string, sid: string, text: string, ag: string) {
  const kind = kindOf(ag)
  const sc = scenarioOf(text)
  const name = agent(ag).name
  if (sc === "refusal") {
    return turn(pid, sid, async function* (m) {
      await sleep(500)
      yield { type: "card", card: { id: `c_${uid()}`, kind: "refusal", title: "Daimon can't accept your request here.", body: `${name} does not answer questions about restricted data in a web chat.`, help: "Ask Jonas Weber, an admin, to add you." } }
      m.content = ""
    }, 0)
  }
  if (sc === "approval") {
    return turn(pid, sid, async function* (m) {
      yield* step("a", "Drafted email", "Larkspur Q3 media mix readout", 900)
      yield* type("The draft is ready. Sending it needs your approval.")
      yield { type: "card", card: { ...APPROVAL, id: `c_${uid()}` } }
      void m
    }, 0.06)
  }
  if (sc === "credential") {
    return turn(pid, sid, async function* (m) {
      yield* step("a", "Looked up HubSpot connection", "Brightwave Telecom account", 800)
      yield* type("I can't read the Brightwave account yet. HubSpot needs a token.")
      yield { type: "card", card: { id: `c_${uid()}`, kind: "credential", state: "pending", headline: `${name} needs HUBSPOT_ACCESS_TOKEN`, fact: `Anyone using ${name} can use it. The value isn't shown in chat.`, key: "HUBSPOT_ACCESS_TOKEN" } }
      void m
    }, 0.04)
  }
  const cost = kind === "pricing" ? 0.96 : kind === "mmm" ? 0.46 : 0.14
  return turn(pid, sid, (m) => rich(kind, m), cost)
}

function continuation(pid: string, sid: string, card: Card, a: CardAnswer) {
  if (card.kind === "approval") {
    if (a.answer === "deny") {
      return turn(pid, sid, async function* () { yield* type("Understood, I did not send it. Tell me what to change and I will redraft.") }, 0.01)
    }
    const again = a.answer === "always" ? " I won't ask again before sending email from this chat." : ""
    return turn(pid, sid, async function* () {
      yield* step("a", "Sent email", "2 recipients", 900)
      yield* type(`Sent to Priya and Sam.${again}`)
    }, 0.02)
  }
  if (card.kind === "credential") {
    if (a.answer === "skip") return turn(pid, sid, async function* () { yield* type("No problem. I'll work from what is in Almanac instead.") }, 0.01)
    return turn(pid, sid, async function* (m) {
      yield* step("a", "Connected HubSpot", "HUBSPOT_ACCESS_TOKEN saved", 700)
      yield* step("b", "Searched HubSpot", "Brightwave Telecom · 3 deals", 1_000)
      yield* type("Brightwave Telecom has **3 open deals** worth $186k.\n\n| Deal | Stage | Amount |\n|---|---|---|\n| Unlimited plan pilot | Proposal | $96k |\n| Retention analytics | Negotiation | $62k |\n| Pricing workshop | Qualified | $28k |")
      void m
    }, 0.1)
  }
  return turn(pid, sid, async function* () { yield { type: "delta", text: "" } }, 0)
}

export const chatHandlers: HttpHandler[] = [
  http.get("/api/chat/agents", async () => {
    await wait()
    const p = session.user()
    if (!p) return unauthorized()
    return HttpResponse.json(p.role === "visitor" ? AGENTS.filter((a) => a.id === "ag_pricing") : AGENTS)
  }),
  http.get("/api/chat/sessions", async ({ request }) => {
    await wait()
    const p = session.user()
    if (!p) return unauthorized()
    return paged(request, [...mine(p.id)].sort((a, b) => b.updated_at.localeCompare(a.updated_at)))
  }),
  http.post("/api/chat/sessions", async ({ request }) => {
    await wait(120)
    const p = session.user()
    if (!p) return unauthorized()
    const { agent_id } = (await request.json()) as { agent_id: string }
    const ag = AGENTS.find((a) => a.id === agent_id)
    if (!ag || (p.role === "visitor" && ag.id !== "ag_pricing")) return forbidden()
    const s: ChatSession = { id: `s_${uid()}`, title: "New chat", agent: ag, updated_at: new Date().toISOString() }
    mine(p.id).unshift(s)
    threads[s.id] = []
    owners[s.id] = p.id
    return HttpResponse.json(s)
  }),
  // The newest `limit` messages (200 by default), oldest first; `before` is an index, `next` the one to pass for earlier ones.
  http.get("/api/chat/sessions/:id", async ({ params, request }) => {
    await wait()
    const p = session.user()
    if (!p) return unauthorized()
    const s = find(p.id, String(params.id))
    const msgs = s && thread(s.id)
    if (!s || !msgs) return notFound()
    const q = new URL(request.url).searchParams
    const end = q.has("before") ? Number(q.get("before")) : msgs.length
    const start = Math.max(0, end - (Math.min(Number(q.get("limit")) || 200, 200)))
    return HttpResponse.json({ session: s, messages: msgs.slice(start, end), ...(start > 0 && { next: start }) })
  }),
  http.patch("/api/chat/sessions/:id", async ({ params, request }) => {
    await wait(120)
    const p = session.user()
    const s = p && find(p.id, String(params.id))
    if (!s) return notFound()
    const { title } = (await request.json()) as { title: string }
    s.title = title
    return HttpResponse.json(s)
  }),
  http.delete("/api/chat/sessions/:id", async ({ params }) => {
    await wait(120)
    const p = session.user()
    if (!p) return unauthorized()
    sessions[p.id] = mine(p.id).filter((s) => s.id !== params.id)
    delete threads[String(params.id)]
    return HttpResponse.json({ ok: true })
  }),
  http.post("/api/chat/sessions/:id/messages", async ({ params, request }) => {
    const p = session.user()
    const m = me()
    if (!p || !m) return unauthorized()
    const sid = String(params.id)
    const s = find(p.id, sid)
    const list = s && thread(sid)
    if (!s || !list) return notFound()
    if (m.credit && m.credit.left_usd <= 0) return HttpResponse.json({ detail: "Out of credits" }, { status: 402 })
    const body = (await request.json()) as { id: string; content: string; attachments?: { id: string; name: string; size: number }[] }
    if (!list.some((x) => x.id === body.id)) {
      list.push({ id: body.id, role: "user", content: body.content, created_at: new Date().toISOString(), attachments: body.attachments?.length ? body.attachments.map(({ name, size }) => ({ name, size })) : undefined })
      if (s.title === "New chat") s.title = lastWord(body.content).slice(0, 56)
      s.updated_at = new Date().toISOString()
    }
    await wait(200)
    return scripted(p.id, sid, body.content, s.agent.id)
  }),
  http.post("/api/chat/sessions/:id/cards/:card", async ({ params, request }) => {
    const p = session.user()
    const m = me()
    if (!p || !m) return unauthorized()
    const sid = String(params.id)
    const s = find(p.id, sid)
    const msg = thread(sid)?.find((x) => x.card?.id === params.card)
    if (!s || !msg?.card) return notFound()
    const a = (await request.json()) as CardAnswer
    const c = msg.card
    if (c.kind === "approval") c.state = a.answer === "deny" ? "denied" : a.answer === "always" ? "always" : "approved"
    else if (c.kind === "credential") c.state = a.answer === "skip" ? "skipped" : "saved"
    await wait(150)
    return continuation(p.id, sid, c, a)
  }),
  http.post("/api/chat/sessions/:id/stop", async ({ params }) => {
    stopping.add(String(params.id))
    return HttpResponse.json({ ok: true })
  }),
  // Turns run in the page here, so a reload ends them: nothing to re-attach to.
  http.get("/api/chat/sessions/:id/stream", () => notFound()),
  http.post("/api/chat/uploads", async ({ request }) => {
    const f = (await request.formData()).get("file") as File
    await wait(250)
    return HttpResponse.json({ id: `up_${uid()}`, name: f.name, size: f.size })
  }),
  http.post("/api/chat/messages/:id/feedback", async ({ params, request }) => {
    const { rating } = (await request.json()) as { rating: "up" | "down" | "none" }
    for (const l of Object.values(threads)) {
      const m = l.find((x) => x.id === params.id)
      if (m) m.feedback = rating === "none" ? null : rating
    }
    await wait(120)
    return HttpResponse.json({ ok: true })
  }),
  http.get("/api/chat/files/:id", () =>
    new HttpResponse("channel,spend_usd,contribution,roas\npaid_search,412000,0.31,3.8\nconnected_tv,268000,0.22,3.1\npaid_social,355000,0.19,2.1\n", {
      headers: { "Content-Type": "text/csv", "Content-Disposition": "attachment; filename=export.csv" },
    })),
]
