// Profile: the signed-in person's own memory, accounts, keys and deletion.
import { http, HttpResponse, type HttpHandler } from "msw"

import type { DeletePreview, Profile } from "@/pages/Profile"
import { wait } from "./util"
import { me } from "./world"

const ago = (h: number) => new Date(Date.now() - h * 3_600_000).toISOString()
const ahead = (d: number) => new Date(Date.now() + d * 86_400_000).toISOString()
const GONE = () => HttpResponse.json({ detail: "That isn't here any more." }, { status: 404 })

const daimon = { id: "ag_default", name: "Daimon" }
const mmm = { id: "ag_mmm", name: "MMM Analyst" }

const state: Profile = {
  name: "",
  memory: [
    { agent: daimon, files: ["/style.md", "/projects/larkspur.md", "/people/team.md"] },
    { agent: mmm, files: ["/priors.md"] },
  ],
  connections: [
    { kind: "microsoft", label: "Microsoft 365", agent: null, key: "" },
    { kind: "mcp", label: "Linear", agent: daimon, key: "https://mcp.linear.app/sse" },
    { kind: "mcp", label: "Notion", agent: daimon, key: "https://mcp.notion.com/mcp" },
  ],
  sign_ins: [],
  chat_accounts: [{ platform: "discord", name: "maya.l" }],
  keys: [
    { id: "k1", agent: mmm, client: "claude-code", created_at: ago(30), expires_at: ahead(9), bound: false },
    { id: "k2", agent: daimon, client: "token", created_at: ago(200), expires_at: ahead(2), bound: true },
  ],
}

const preview: DeletePreview = {
  enabled: true,
  held: ["1 linked account", "2 routines", "saved settings", "1 saved Microsoft account", "14 saved conversations"],
  kept: ["3 conversations shared with other people"],
}

export const profileHandlers: HttpHandler[] = [
  http.get("/api/profile", async () => {
    await wait()
    const user = me()
    return HttpResponse.json({
      ...state,
      name: user?.name ?? "",
      sign_ins: (user?.identities ?? []).map((i, n) => ({ id: `i${n}`, provider: i.provider, label: i.label })),
    })
  }),
  http.post("/api/profile/memory/:id/forget", async ({ params }) => {
    await wait(300)
    const i = state.memory.findIndex((m) => m.agent.id === params.id)
    if (i < 0) return GONE()
    const [gone] = state.memory.splice(i, 1)
    return HttpResponse.json({ removed: gone!.files.length })
  }),
  http.post("/api/profile/connections/disconnect", async ({ request }) => {
    await wait(300)
    const b = (await request.json()) as { kind: string; agent_id?: string; key?: string }
    const i = state.connections.findIndex((c) => c.kind === b.kind && (c.agent?.id ?? undefined) === (b.agent_id ?? undefined) && c.key === (b.key ?? ""))
    if (i < 0) return GONE()
    state.connections.splice(i, 1)
    return HttpResponse.json({ ok: true })
  }),
  http.delete("/api/profile/sign-ins/:id", async () => {
    await wait(200)
    return HttpResponse.json({ detail: "This is your only sign-in. Add another before removing it." }, { status: 409 })
  }),
  http.delete("/api/profile/keys/:id", async ({ params }) => {
    await wait(200)
    const i = state.keys.findIndex((k) => k.id === params.id)
    if (i < 0) return GONE()
    state.keys.splice(i, 1)
    return HttpResponse.json({ ok: true })
  }),
  http.get("/api/profile/delete", async () => { await wait(); return HttpResponse.json(preview) }),
  http.post("/api/profile/delete", async ({ request }) => {
    await wait(500)
    const b = (await request.json()) as { confirm: string }
    if (b.confirm.trim().toLowerCase() !== (me()?.name ?? "").toLowerCase()) return HttpResponse.json({ detail: "Type your name exactly to confirm." }, { status: 422 })
    return HttpResponse.json({ ok: true })
  }),
]
