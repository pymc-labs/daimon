// Mock API for the workspace's skill library and sandbox environments (admin only).
import { http, HttpResponse } from "msw"

import { ECOSYSTEMS, type EnvironmentDoc, type EnvironmentInput, type LibrarySkill, type Skill, type SkillDetail, type SkillUse, type SyncedSkill } from "@/modules/agents/types"
import { roster } from "./agents"
import { forbidden, notFound, unauthorized, wait } from "./util"

const ago = (h: number) => new Date(Date.now() - h * 3_600_000).toISOString()
const gone = new Set<string>()
const DEFAULT = new Set(["file-handling", "marimo_notebooks"])

function admin() {
  const v = roster.viewer()
  return !v ? { res: unauthorized() } : !v.admin ? { res: forbidden("The skill library needs an admin.") } : { v }
}

const PEOPLE = ["Maya Lindqvist", "Jonas Berg", "Priya Raman"]
const GITHUB = { "pr-review": "https://github.com/pymc-labs/skills/tree/main/pr-review" } as Record<string, string>
const CHANNEL_USES: Record<string, SkillUse[]> = { "pr-review": [{ kind: "channel", id: "C0ENG", name: "engineering", version: null }] }

type Ver = { version: string; at: string; author: string | null; files: Record<string, string> }
const history = new Map<string, Ver[]>()
const made: Skill[] = []

const skills = (): Skill[] => [...roster.library(), ...made].filter((s) => !gone.has(s.skill_id))

/** Three versions of a custom skill: the first a stub, then the shipped files. */
function versionsOf(id: string): Ver[] {
  let v = history.get(id)
  if (v) return v
  const head = roster.files.get(`${id}@3`) ?? { "SKILL.md": `---\nname: ${id}\ndescription: New skill.\n---\n\n# ${id}\n` }
  const skill = head["SKILL.md"]!
  const stub = skill.split("\n\n").slice(0, 2).join("\n\n") + "\n"
  v = [
    { version: "1", at: ago(900), author: null, files: { "SKILL.md": stub } },
    { version: "2", at: ago(400), author: PEOPLE[1]!, files: { "SKILL.md": skill.replace(/\n\n[^\n]*\n$/, "\n") } },
    { version: "3", at: ago(60), author: PEOPLE[0]!, files: head },
  ]
  history.set(id, v)
  return v
}

function library(): LibrarySkill[] {
  return skills().map((s, i) => {
    const custom = s.type === "custom"
    const vs = custom ? versionsOf(s.skill_id) : []
    return {
      id: s.skill_id, name: s.skill_id, title: s.title, description: s.description, source: custom ? "custom" : "anthropic",
      version: custom ? vs.at(-1)!.version : "latest", created_at: ago(900 - i * 40), updated_at: custom ? vs.at(-1)!.at : ago(1500), default: DEFAULT.has(s.skill_id),
      used_by: [
        ...roster.all().filter((r) => r.versions.at(-1)!.config.skills.some((k) => k.skill_id === s.skill_id)).map((r) => {
          const pin = r.versions.at(-1)!.config.skills.find((k) => k.skill_id === s.skill_id)!.version
          return { kind: "agent" as const, id: r.id, name: r.versions.at(-1)!.config.name, version: pin === "latest" ? null : pin }
        }),
        ...(CHANNEL_USES[s.skill_id] ?? []),
      ],
    }
  })
}

const detail = (id: string): SkillDetail | undefined => {
  const row = library().find((s) => s.id === id)
  return row && { ...row, source_url: GITHUB[id] ?? null }
}

const filesDoc = (id: string, v: Ver) => ({
  skill_id: id, version: v.version,
  files: Object.entries(v.files).sort(([x], [y]) => (x === "SKILL.md" ? -1 : y === "SKILL.md" ? 1 : x.localeCompare(y))).map(([path, content]) => ({ path, size: content.length, content })),
})

const publish = (id: string, files: Record<string, string>): Ver => {
  const vs = versionsOf(id)
  const v = { version: String(Number(vs.at(-1)!.version) + 1), at: new Date().toISOString(), author: PEOPLE[0]!, files }
  vs.push(v)
  return v
}

const slug = (t: string) => t.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "")

const ENVS: EnvironmentDoc[] = [
  { id: "env_default", name: "default", description: "", created_at: ago(2000), updated_at: ago(2000), managed: true, cloud: true, network: "limited", open_network: false, allowed_hosts: [], allow_package_managers: true, allow_mcp_servers: true, packages: { pip: ["numpy", "pandas"] }, used_by: [{ kind: "workspace", id: "ws", name: "Workspace default" }] },
  { id: "env_daimon", name: "daimon", description: "Analysis tools", created_at: ago(700), updated_at: ago(700), managed: false, cloud: true, network: "limited", open_network: true, allowed_hosts: ["api.example.com"], allow_package_managers: true, allow_mcp_servers: true, packages: { pip: ["pymc", "arviz"], apt: ["graphviz"] }, used_by: [{ kind: "channel", id: "C0ENG", name: "engineering" }, { kind: "channel", id: "C0CLV", name: "clv-models" }] },
  { id: "env_clv", name: "clv", description: "", created_at: ago(300), updated_at: ago(300), managed: false, cloud: true, network: "unrestricted", open_network: true, allowed_hosts: [], allow_package_managers: true, allow_mcp_servers: true, packages: {}, used_by: [] },
]

const toDoc = (id: string, spec: EnvironmentInput, was?: EnvironmentDoc): EnvironmentDoc => ({
  id, name: was?.name ?? spec.name, description: spec.description, created_at: was?.created_at ?? new Date().toISOString(), updated_at: new Date().toISOString(), managed: false, cloud: true,
  network: spec.network, open_network: spec.network === "unrestricted" || spec.allowed_hosts.length > 0,
  allowed_hosts: spec.network === "limited" ? spec.allowed_hosts : [], allow_package_managers: spec.allow_package_managers, allow_mcp_servers: spec.allow_mcp_servers,
  packages: Object.fromEntries(ECOSYSTEMS.map((k) => [k, spec.packages[k] ?? []]).filter(([, v]) => (v as string[]).length)), used_by: was?.used_by ?? [],
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
    const s = detail(String(params.id))
    if (!s) return notFound()
    if (s.source !== "custom") return HttpResponse.json([])
    return HttpResponse.json([...versionsOf(s.id)].reverse().map((v) => ({ version: v.version, created_at: v.at, author: v.author })))
  }),
  http.get("/api/agents/skills/:id/files", async ({ params, request }) => {
    await wait(120)
    const a = admin()
    if (a.res) return a.res
    const s = detail(String(params.id))
    if (!s) return notFound()
    if (s.source !== "custom") return HttpResponse.json({ detail: "The files of a built-in skill are not available." }, { status: 404 })
    const held = new URL(request.url).searchParams.get("version")
    const v = versionsOf(s.id).find((x) => x.version === (held ?? s.version))
    return v ? HttpResponse.json(filesDoc(s.id, v)) : notFound()
  }),
  http.put("/api/agents/skills/:id/files", async ({ params, request }) => {
    await wait(400)
    const a = admin()
    if (a.res) return a.res
    const s = detail(String(params.id))
    if (!s) return notFound()
    if (s.source !== "custom") return forbidden("A built-in skill can't be edited.")
    const { files, expected_version } = (await request.json()) as { files: Record<string, string | null>; expected_version: string }
    if (expected_version !== s.version) return HttpResponse.json({ detail: "This skill changed since you opened it. Reload to see the new version." }, { status: 409 })
    const base = versionsOf(s.id).find((x) => x.version === expected_version)!.files
    const next = Object.fromEntries(Object.entries(files).map(([p, body]) => [p, body ?? base[p] ?? ""]))
    if (!next["SKILL.md"]) return HttpResponse.json({ detail: "SKILL.md is missing." }, { status: 422 })
    const v = publish(s.id, next)
    return HttpResponse.json({ id: s.id, name: s.name, version: v.version })
  }),
  http.post("/api/agents/skills/:id/restore", async ({ params, request }) => {
    await wait(400)
    const a = admin()
    if (a.res) return a.res
    const s = detail(String(params.id))
    if (!s) return notFound()
    if (s.source !== "custom") return forbidden("A built-in skill can't be edited.")
    const { version } = (await request.json()) as { version: string }
    const old = versionsOf(s.id).find((x) => x.version === version)
    if (!old) return notFound()
    const v = publish(s.id, old.files)
    return HttpResponse.json({ id: s.id, name: s.name, version: v.version })
  }),
  http.get("/api/agents/skills/:id", async ({ params }) => {
    await wait(120)
    const a = admin()
    if (a.res) return a.res
    const s = detail(String(params.id))
    return s ? HttpResponse.json(s) : notFound()
  }),
  http.post("/api/agents/skills", async ({ request }) => {
    await wait(400)
    const a = admin()
    if (a.res) return a.res
    const { name, description } = (await request.json()) as { name: string; description: string }
    const id = slug(name)
    if (!id) return HttpResponse.json({ detail: "Give the skill a name." }, { status: 422 })
    if (skills().some((x) => x.skill_id === id)) return HttpResponse.json({ detail: `A skill named ${id} already exists.` }, { status: 409 })
    const title = name.trim()
    made.push({ type: "custom", skill_id: id, title, description: description.trim(), version: "1" })
    history.set(id, [{ version: "1", at: new Date().toISOString(), author: PEOPLE[0]!, files: { "SKILL.md": `---\nname: ${id}\ndescription: ${description.trim()}\n---\n\n# ${title}\n` } }])
    return HttpResponse.json({ id, name: id, version: "1" }, { status: 201 })
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
    const { spec, confirm_open_network, expected_updated_at } = (await request.json()) as { spec: EnvironmentInput; confirm_open_network: boolean; expected_updated_at?: string }
    if (expected_updated_at && expected_updated_at !== was.updated_at) return HttpResponse.json({ detail: "This environment changed since you opened it. Reload and try again." }, { status: 409 })
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
