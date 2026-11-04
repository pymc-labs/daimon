// Mock API for an agent's GitHub link: repos, skill origins, push resync and the release label.
import { http, HttpResponse } from "msw"

import type { AgentGithub, GithubRepo, GithubSkill } from "@/modules/agents/setup/github"
import { roster } from "./agents"
import { forbidden, notFound, unauthorized, wait } from "./util"

const ago = (h: number) => new Date(Date.now() - h * 3_600_000).toISOString()
const repo = (name: string, extra: Partial<GithubRepo> = {}): GithubRepo => ({
  name, url: `https://github.com/${name}`, access: "read", working: false, shared: false, status: "active", installation: "active",
  granted_at: ago(200), granted_by: "Maya Lindqvist", ...extra,
})

type Link = Omit<AgentGithub, "skills" | "can_manage"> & { origins: Record<string, Partial<GithubSkill>> }
const fromRepo = (name: string, path: string, at: number): Partial<GithubSkill> =>
  ({ origin: "repo", repo: name, branch: "main", path, url: `https://github.com/${name}/tree/main/${path}`, updated_at: ago(at) })

/** Per agent; one with a working repo and a shared one, one whose sync is failing, the rest with no repos. */
const LINKS = new Map<string, Link>([
  ["ag_mmm", {
    configured: true,
    repos: [repo("acme/mmm-skills", { access: "write", working: true }), repo("acme/shared-skills", { shared: true, granted_by: "Priya Raman", granted_at: ago(500) })],
    sync: { state: "done", at: ago(3), error: null },
    release: { name: "v2.4.1", at: ago(30), url: "https://github.com/acme/mmm-skills/releases/tag/v2.4.1" },
    origins: {
      "data-validation": fromRepo("acme/mmm-skills", "skills/data-validation", 3),
      "exploratory-data-analysis": fromRepo("acme/shared-skills", "skills/exploratory-data-analysis", 70),
      "warehouse-pulls": fromRepo("acme/mmm-skills", "skills/warehouse-pulls", 3),
      "pymc-artifact-style": { origin: "library", updated_at: ago(400) },
      "pymc-marketing": { origin: "external", updated_at: ago(40) },
      "eda-storytelling": { origin: "external", updated_at: ago(300) },
    },
  }],
  ["ag_pricing", {
    configured: true,
    repos: [repo("acme/pricing-models", { access: "write", working: true, status: "suspended", installation: "suspended", granted_by: "Priya Raman" })],
    sync: { state: "done", at: ago(26), error: "GitHub says the install is paused" },
    release: null,
    origins: { "data-validation": fromRepo("acme/pricing-models", "skills/data-validation", 90), "data-cleaning": fromRepo("acme/pricing-models", "skills/data-cleaning", 90) },
  }],
])
const EMPTY: Link = { configured: true, repos: [], sync: null, release: null, origins: {} }

/** Who sees an agent's setup and who may change it: the owners and admins. */
function access(id: string) {
  const v = roster.viewer(), r = roster.find(id)
  if (!v) return { res: unauthorized() }
  if (!r) return { res: notFound() }
  if (!roster.details(r, v)) return { res: forbidden("Ask the agent's owner or an admin.") }
  return { r, v }
}

function body(id: string, r: NonNullable<ReturnType<typeof roster.find>>): AgentGithub {
  const { origins, ...link } = LINKS.get(id) ?? EMPTY
  const skills = r.versions.at(-1)!.config.skills.map((s): GithubSkill => ({
    skill_id: s.skill_id, title: s.title, version: s.version === "latest" ? null : `v${s.version}`, origin: "library",
    repo: null, branch: null, path: null, url: null, updated_at: null, ...origins[s.skill_id],
  }))
  return { ...link, can_manage: true, skills }
}

const GH = "/api/agents/:id/github"
export const agentGithubHandlers = [
  http.get(GH, async ({ params }) => {
    await wait()
    const id = String(params.id), a = access(id)
    if (a.res) return a.res
    return HttpResponse.json(body(id, a.r))
  }),
  http.post(`${GH}/connect`, async ({ params }) => {
    await wait()
    const a = access(String(params.id))
    if (a.res) return a.res
    return HttpResponse.json({ url: "https://github.com/apps/acme-daimon/installations/new" })
  }),
  http.put(`${GH}/working`, async ({ params, request }) => {
    await wait(300)
    const id = String(params.id), a = access(id)
    if (a.res) return a.res
    const { repo: name } = (await request.json()) as { repo: string }
    const link = LINKS.get(id) ?? { ...EMPTY }
    if (!link.repos.some((x) => x.name === name)) return HttpResponse.json({ detail: `${name} is not connected to this agent.` }, { status: 404 })
    link.repos = link.repos.map((x) => ({ ...x, working: x.name === name }))
    LINKS.set(id, link)
    return HttpResponse.json(body(id, a.r))
  }),
  http.delete(`${GH}/repos/:owner/:name`, async ({ params }) => {
    await wait(300)
    const id = String(params.id), a = access(id)
    if (a.res) return a.res
    const name = `${String(params.owner)}/${String(params.name)}`
    const link = LINKS.get(id) ?? { ...EMPTY }
    link.repos = link.repos.filter((x) => x.name !== name)
    LINKS.set(id, link)
    return HttpResponse.json(body(id, a.r))
  }),
  http.post(`${GH}/resync`, async ({ params }) => {
    await wait(700)
    const id = String(params.id), a = access(id)
    if (a.res) return a.res
    const link = LINKS.get(id)
    if (!link?.repos.length) return HttpResponse.json({ results: [] })
    if (link.sync?.error) return HttpResponse.json({ results: Object.keys(link.origins).map((name) => ({ name, action: "failed", detail: link.sync!.error })) })
    link.sync = { state: "done", at: new Date().toISOString(), error: null }
    return HttpResponse.json({
      results: Object.entries(link.origins).filter(([, o]) => o.origin === "repo")
        .map(([name], i) => ({ name, action: i ? "skipped" : "updated", detail: i ? "No change" : null })),
    })
  }),
]
