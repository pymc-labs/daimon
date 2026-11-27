// Identity, workspaces and dev switching. Module handlers live beside this file.
import { http, HttpResponse } from "msw"

import { accounts, agent, AGENTS, connectAccount, disconnectAccount, me, PEOPLE, session, SESSIONS } from "./world"
import { forbidden, notFound, paged, unauthorized, wait } from "./util"

export const coreHandlers = [
  http.get("/api/me", async () => {
    await wait()
    const m = me()
    return m ? HttpResponse.json(m) : unauthorized()
  }),
  http.get("/auth/providers", () => HttpResponse.json([
    { id: "discord", label: "Discord", kind: "redirect" },
    { id: "slack", label: "Slack", kind: "redirect" },
    { id: "microsoft", label: "Microsoft", kind: "redirect" },
  ])),
  http.get("/auth/legal", () => HttpResponse.json({ privacy: "https://example.com/privacy", terms: "https://example.com/terms" })),
  // Every provider signs in the same demo admin; the dev panel passes `email` to switch people.
  http.post("/auth/mock", async ({ request }) => {
    const { email } = (await request.json()) as { email?: string }
    session.signIn(email ?? "maya.lindqvist@fernhill.example")
    return HttpResponse.json({ ok: true })
  }),
  // Demo links: `inv_demo` invites a visitor, `inv_member` a member, `inv_owner` an owner.
  http.get("/api/invites/:token", async ({ params }) => {
    await wait()
    const base = { workspace: "Fernhill Labs", from: "Maya Lindqvist", expires_at: new Date(Date.now() + 14 * 86_400_000).toISOString() }
    if (params.token === "inv_demo") return HttpResponse.json({ ...base, kind: "invite", email: "jordan@brightwave.example", agents: [agent("ag_pricing")], credit_usd: 5 })
    if (params.token === "inv_member") return HttpResponse.json({ ...base, kind: "invite", email: "priya.raman@fernhill.example", agents: [], credit_usd: 0 })
    if (params.token === "inv_owner") return HttpResponse.json({ ...base, kind: "owner", email: "", agents: [], credit_usd: 0 })
    return notFound()
  }),
  http.post("/api/invites/:token/accept", ({ params }) => {
    if (params.token === "inv_owner") return session.user() ? HttpResponse.json({ ok: true }) : unauthorized("Sign in to this workspace, then open the link again.")
    session.signIn(params.token === "inv_member" ? "priya.raman@fernhill.example" : "jordan@brightwave.example")
    session.setWorkspace("ws_fernhill")
    return HttpResponse.json({ ok: true })
  }),
  // A connect has no upstream to visit in the demo: the account joins at once and the page reloads.
  http.post("/auth/:provider/connect", ({ params }) => {
    const user = session.user()
    if (!user) return unauthorized()
    if (user.role === "visitor") return forbidden("Guests can't connect an account.")
    connectAccount(String(params.provider))
    return HttpResponse.json({ url: "/chat" })
  }),
  http.delete("/api/me/accounts/:id", ({ params }) => {
    const hit = accounts().find((a) => a.id === params.id)
    if (!hit) return notFound()
    if (hit.current) return HttpResponse.json({ detail: "This is the account you signed in with." }, { status: 409 })
    disconnectAccount(hit.provider)
    return HttpResponse.json({ ok: true })
  }),
  http.post("/auth/logout", () => {
    session.signOut()
    return HttpResponse.json({ ok: true })
  }),
  http.post("/api/workspaces/switch", async ({ request }) => {
    const { id } = (await request.json()) as { id: string }
    session.setWorkspace(id)
    return HttpResponse.json({ ok: true })
  }),
  http.get("/api/dev/users", () => HttpResponse.json(PEOPLE)),
  http.post("/api/dev/switch", async ({ request }) => {
    const { email } = (await request.json()) as { email: string }
    session.signIn(email)
    session.setWorkspace("ws_fernhill")
    return HttpResponse.json({ ok: true })
  }),
  // Sidebar and palette read these; the chat module owns the rest of /api/chat.
  http.get("/api/chat/sessions", async ({ request }) => {
    await wait()
    const p = session.user()
    return p ? paged(request, SESSIONS[p.id] ?? []) : unauthorized()
  }),
  http.get("/api/chat/agents", async () => {
    await wait()
    const p = session.user()
    if (!p) return unauthorized()
    return HttpResponse.json(p.role === "visitor" ? AGENTS.filter((a) => a.id === "ag_pricing") : AGENTS)
  }),
]
