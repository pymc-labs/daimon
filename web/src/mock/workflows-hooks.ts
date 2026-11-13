// Webhook triggers: URLs, secret, deliveries, replay and Listen.
import { http, HttpResponse, type HttpHandler } from "msw"

import type { DeliveryRow, Secret, WebhookInfo } from "@/modules/workflows/types"
import { notFound, wait } from "./util"
import { ago, find, launch, runsOf, specOf, type Stored } from "./workflows"
import { chain } from "./workflows-runs"

const HOST = "https://daimon.example/hooks/w"
type Hook = { secret: string; created: string; previous: string | null; listening: number | null; extra: DeliveryRow[] }
const HOOKS = new Map<string, Hook>()
const hook = (w: Stored): Hook => {
  let h = HOOKS.get(w.id)
  if (!h) HOOKS.set(w.id, h = { secret: "whsec_k3Qm9xT2vB8nLd0aPz4wRu7a91f", created: ago(24 * 41), previous: null, listening: null, extra: [] })
  return h
}
const hint = (s: string) => `whsec_...${s.slice(-4)}`
const triggerOf = (w: Stored) => specOf(w).triggers?.find((t) => t.kind === "webhook")

const HEADERS = (id: string, at: number) => ({
  "content-type": "application/json", "user-agent": "Formbridge/2.4", "webhook-id": id, "webhook-timestamp": String(Math.floor(at / 1000)), "webhook-signature": `v1,${chain("", id).slice(0, 24)}=`,
})
const env = (inputs: Record<string, unknown>) => ({ event: "form.submitted", form: "contact", data: inputs })

/** Deliveries newest first: one per run the trigger started, then a rejected and a duplicate for the log. */
function rows(w: Stored): DeliveryRow[] {
  const h = hook(w)
  const base = runsOf(w).filter((r) => r.delivery).map((r): DeliveryRow => {
    const at = Date.parse(r.events[0]!.at)
    return { id: r.delivery!, received_at: r.events[0]!.at, verified: true, outcome: "accepted", status: 202, run: { id: r.id, number: r.number }, headers: HEADERS(r.delivery!, at), payload: env(r.inputs) }
  })
  if (!base.length) return h.extra
  const first = base[0]!
  const bad: DeliveryRow = { id: "dlv_8c01f2a9d4", received_at: new Date(Date.parse(first.received_at) - 3 * 3_600_000).toISOString(), verified: false, outcome: "rejected", status: 401, run: null,
    headers: { ...first.headers, "webhook-id": "dlv_8c01f2a9d4", "webhook-signature": "v1,invalid" }, payload: first.payload }
  const dup: DeliveryRow = { ...base[1] ?? first, id: base[1]?.id ?? first.id, outcome: "duplicate", status: 200, run: null, received_at: new Date(Date.parse(first.received_at) - 5 * 3_600_000).toISOString() }
  return [...h.extra, ...base, bad, dup].sort((a, b) => b.received_at.localeCompare(a.received_at))
}

const info = (w: Stored): WebhookInfo => {
  const h = hook(w), id = triggerOf(w)?.id ?? "webhook"
  return {
    live_url: `${HOST}/${w.id}_${id}`, test_url: `${HOST}/${w.id}_${id}/test`, secret_hint: hint(h.secret), secret_created_at: h.created, previous_expires_at: h.previous,
    listening_until: h.listening && h.listening > Date.now() ? new Date(h.listening).toISOString() : null,
  }
}

export const workflowsHooksHandlers: HttpHandler[] = [
  http.get("/api/workflows/:id/webhook", async ({ params }) => {
    await wait(120)
    const w = find(params.id)
    return w ? HttpResponse.json(info(w)) : notFound()
  }),
  http.get("/api/workflows/:id/deliveries", async ({ params }) => {
    await wait()
    const w = find(params.id)
    return w ? HttpResponse.json(rows(w)) : notFound()
  }),
  http.post("/api/workflows/:id/webhook/rotate", async ({ params, request }) => {
    await wait(500)
    const w = find(params.id)
    if (!w) return notFound()
    const { overlap } = (await request.json().catch(() => ({ overlap: "24h" }))) as { overlap: string }
    const h = hook(w)
    h.secret = `whsec_${Array.from({ length: 28 }, () => "abcdefghjkmnpqrstuvwxyz23456789"[Math.floor(Math.random() * 31)]).join("")}`
    h.created = new Date().toISOString()
    h.previous = overlap === "now" ? null : new Date(Date.now() + (overlap === "1h" ? 1 : overlap === "7d" ? 168 : 24) * 3_600_000).toISOString()
    return HttpResponse.json({ secret: h.secret, created_at: h.created, previous_expires_at: h.previous } satisfies Secret)
  }),
  http.post("/api/workflows/:id/webhook/listen", async ({ params }) => {
    await wait(150)
    const w = find(params.id)
    if (!w) return notFound()
    const h = hook(w)
    h.listening = Date.now() + 600_000
    // A test delivery arrives a few seconds later, on the test URL.
    setTimeout(() => {
      if (!h.listening) return
      const id = `dlv_test_${Date.now().toString(36)}`
      const at = Date.now()
      h.extra.unshift({ id, received_at: new Date(at).toISOString(), verified: true, outcome: "accepted", status: 202, run: null, headers: HEADERS(id, at),
        payload: env({ name: "Mika Tanaka", email: "mika@harborline.example", message: "Do you work with retail chains in Japan?" }) })
      h.listening = null
    }, 3500)
    return HttpResponse.json(info(w))
  }),
  http.delete("/api/workflows/:id/webhook/listen", async ({ params }) => {
    await wait(100)
    const w = find(params.id)
    if (!w) return notFound()
    hook(w).listening = null
    return HttpResponse.json(info(w))
  }),
  http.post("/api/workflows/:id/deliveries/:did/replay", async ({ params }) => {
    await wait(500)
    const w = find(params.id)
    const d = w && rows(w).find((x) => x.id === params.did)
    if (!w || !d) return notFound()
    const t = triggerOf(w)!
    const inputs = ((d.payload as { data?: Record<string, unknown> }).data ?? {}) as Record<string, unknown>
    const id = `dlv_${Date.now().toString(36)}`
    const run = launch(w, inputs, null, { trigger: { kind: "webhook", id: t.id }, delivery: id })
    const row: DeliveryRow = { id, received_at: new Date().toISOString(), verified: true, outcome: "accepted", status: 202, run: { id: run.id, number: run.number }, headers: HEADERS(id, Date.now()), payload: d.payload }
    return HttpResponse.json(row, { status: 201 })
  }),
]
