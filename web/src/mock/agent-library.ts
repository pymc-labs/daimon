// Mock API for the workspace's skill library and sandbox environments (admin only).
import { http, HttpResponse } from "msw"

import { ECOSYSTEMS, type EnvironmentDoc, type EnvironmentInput, type LibrarySkill, type SyncedSkill } from "@/modules/agents/types"
import { roster } from "./agents"
import { forbidden, notFound, unauthorized, wait } from "./util"

const ago = (h: number) => new Date(Date.now() - h * 3_600_000).toISOString()
const gone = new Set<string>()
const DEFAULT = new Set(["file-handling", "marimo_notebooks"])

function admin() {
  const v = roster.viewer()
  return !v ? { res: unauthorized() } : !v.admin ? { res: forbidden("The skill library needs an admin.") } : { v }
}

function library(): LibrarySkill[] {
  return roster.library().filter((s) => !gone.has(s.skill_id)).map((s, i) => ({
    id: s.skill_id, name: s.skill_id, source: "custom", version: String(3 + (i % 4)), created_at: ago(900 - i * 90), default: DEFAULT.has(s.skill_id),
    used_by: roster.all().filter((r) => r.versions.at(-1)!.config.skills.some((k) => k.skill_id === s.skill_id)).map((r) => ({ kind: "agent" as const, name: r.versions.at(-1)!.config.name })),
  }))
}

const ENVS: EnvironmentDoc[] = [
  { id: "env_default", name: "default", description: "", created_at: ago(2000), managed: true, cloud: true, network: "limited", open_network: false, allowed_hosts: [], allow_package_managers: true, allow_mcp_servers: true, packages: { pip: ["numpy", "pandas"] } },
  { id: "env_daimon", name: "daimon", description: "Analysis tools", created_at: ago(700), managed: false, cloud: true, network: "limited", open_network: true, allowed_hosts: ["api.example.com"], allow_package_managers: true, allow_mcp_servers: true, packages: { pip: ["pymc", "arviz"], apt: ["graphviz"] } },
  { id: "env_clv", name: "clv", description: "", created_at: ago(300), managed: false, cloud: true, network: "unrestricted", open_network: true, allowed_hosts: [], allow_package_managers: true, allow_mcp_servers: true, packages: {} },
]

const toDoc = (id: string, spec: EnvironmentInput, was?: EnvironmentDoc): EnvironmentDoc => ({
  id, name: was?.name ?? spec.name, description: spec.description, created_at: was?.created_at ?? new Date().toISOString(), managed: false, cloud: true,
  network: spec.network, open_network: spec.network === "unrestricted" || spec.allowed_hosts.length > 0,
  allowed_hosts: spec.network === "limited" ? spec.allowed_hosts : [], allow_package_managers: spec.allow_package_managers, allow_mcp_servers: spec.allow_mcp_servers,
  packages: Object.fromEntries(ECOSYSTEMS.map((k) => [k, spec.packages[k] ?? []]).filter(([, v]) => (v as string[]).length)),
})

/** A sealed channel runs in "daimon", so opening its network is confirmed first. */
const sealedOpening = (spec: EnvironmentInput, was: EnvironmentDoc) => was.name === "daimon" && was.network === "limited" && spec.network === "unrestricted"

export const agentLibraryHandlers = [
  http.get("/api/agents/skills", async () => {
    await wait()
    const a = admin()
    return a.res ?? HttpResponse.json(library())
  }),
  http.get("/api/agents/skills/:id/versions", async ({ params }) => {
    await wait(120)
    const a = admin()
    if (a.res) return a.res
    const s = library().find((x) => x.id === params.id)
    if (!s) return notFound()
    return HttpResponse.json(Array.from({ length: Number(s.version) }, (_, i) => ({ version: String(Number(s.version) - i), created_at: ago(60 + i * 200) })))
  }),
  http.post("/api/agents/skills/sync", async ({ request }) => {
    await wait(900)
    const a = admin()
    if (a.res) return a.res
    const { url } = (await request.json()) as { url: string }
    if (!/^https:\/\/github\.com\/[^/]+\/[^/]+/.test(url)) return HttpResponse.json({ detail: "That repository could not be read. It may be private or the address wrong." }, { status: 422 })
    const out: SyncedSkill[] = [
      { name: "weekly-review", action: "created", detail: null },
      { name: "data-cleaning", action: "updated", detail: null },
      { name: "broken-skill", action: "failed", detail: "No SKILL.md" },
    ]
    return HttpResponse.json(out)
  }),
  http.delete("/api/agents/skills/:id", async ({ params }) => {
    await wait(300)
    const a = admin()
    if (a.res) return a.res
    const s = library().find((x) => x.id === params.id)
    if (!s) return notFound()
    if (s.default) return forbidden(`${s.name} is a default skill and can't be deleted.`)
    if (s.used_by.length) return HttpResponse.json({ detail: `${s.name} is still used by ${[...new Set(s.used_by.map((u) => u.name))].join(", ")}. Remove it there first.` }, { status: 409 })
    gone.add(s.id)
    return new HttpResponse(null, { status: 204 })
  }),
  http.get("/api/agents/environments", async () => {
    await wait()
    const a = admin()
    return a.res ?? HttpResponse.json([...ENVS].sort((x, y) => x.name.localeCompare(y.name)))
  }),
  http.post("/api/agents/environments", async ({ request }) => {
    await wait(400)
    const a = admin()
    if (a.res) return a.res
    const spec = (await request.json()) as EnvironmentInput
    if (ENVS.some((e) => e.name.toLowerCase() === spec.name.toLowerCase())) return HttpResponse.json({ detail: `environment '${spec.name}' already exists` }, { status: 409 })
    const doc = toDoc(`env_${Date.now().toString(36)}`, spec)
    ENVS.push(doc)
    return HttpResponse.json(doc)
  }),
  http.put("/api/agents/environments/:id", async ({ params, request }) => {
    await wait(400)
    const a = admin()
    if (a.res) return a.res
    const i = ENVS.findIndex((e) => e.id === params.id)
    if (i < 0) return notFound()
    const was = ENVS[i]!
    if (was.managed) return forbidden(`${was.name} is managed by the server and can't be changed here.`)
    const { spec, confirm_open_network } = (await request.json()) as { spec: EnvironmentInput; confirm_open_network: boolean }
    if (!confirm_open_network && sealedOpening(spec, was)) return HttpResponse.json({ detail: "A sealed channel runs in this environment. Opening its network lets that channel's content go anywhere. Confirm to go ahead." }, { status: 428 })
    ENVS[i] = toDoc(was.id, spec, was)
    return HttpResponse.json(ENVS[i])
  }),
  http.delete("/api/agents/environments/:id", async ({ params }) => {
    await wait(400)
    const a = admin()
    if (a.res) return a.res
    const i = ENVS.findIndex((e) => e.id === params.id)
    if (i < 0) return notFound()
    const was = ENVS[i]!
    if (was.managed) return forbidden(`${was.name} is managed by the server and can't be changed here.`)
    ENVS.splice(i, 1)
    return HttpResponse.json({ note: `Archived the ${was.name} environment. No channel or workspace picked it.` })
  }),
]
