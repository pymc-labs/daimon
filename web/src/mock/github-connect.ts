// Mock API for the GitHub repo connect flow. Every view is one scenario, opened by a name:
//   /oauth/github/connect/<name>       redirects to the confirm step for editor and picker names
//   /oauth/github/confirm?state=<name> shows the view; add &cancel=1 to cancel
//   /oauth/github/callback?code=x&state=<name>, /oauth/github/setup?state=<name>
// Names: editor editor-follow editor-following editor-requested editor-error editor-fail picker picker-agent picker-requested install pending
// no-repos expired error error-retry transient cancelled already already-agent saved done done-agent done-other.
import { http, HttpResponse } from "msw"

import type { AgentRepo, ConnectInstallation, ConnectRepo, ConnectView, Outcome, Selection } from "@/pages/github/types"
import { wait } from "./util"

const ago = (d: number) => new Date(Date.now() - d * 86_400_000).toISOString()
const back = { url: "https://discord.com/channels/745261709622771773", platform: "discord" as const }

let next = 1000
const repo = (owner: string, installation: number, name: string, extra: Partial<ConnectRepo> = {}): ConnectRepo =>
  ({ id: next++, owner_id: installation * 10, installation_id: installation, full_name: `${owner}/${name}`, permission: "write", private: false, archived: false, ...extra })
const install = (id: number, login: string, repos: ConnectRepo[], extra: Partial<ConnectInstallation> = {}): ConnectInstallation =>
  ({ id, owner_id: id * 10, owner_login: login, repository_selection: "selected", repos, owner_type: "Organization", avatar_url: null, state: "active", total: repos.length, truncated: false, fetch_state: "ok", ...extra })

const mine = [repo("maya-l", 11, "notes", { private: true }), repo("maya-l", 11, "dotfiles"), repo("maya-l", 11, "mmm-experiments", { private: true }), repo("maya-l", 11, "old-site", { archived: true })]
const acme = [
  repo("acme", 12, "mmm-skills"), repo("acme", 12, "shared-skills"), repo("acme", 12, "pricing-models", { private: true }),
  ...Array.from({ length: 36 }, (_, i) => repo("acme", 12, `service-${String(i + 1).padStart(3, "0")}`, { private: i % 3 === 0 })),
]
const [mmm, shared, pricing] = acme as [ConnectRepo, ConnectRepo, ConnectRepo]
const INSTALLATIONS: ConnectInstallation[] = [
  install(11, "maya-l", mine, { owner_type: "User" }),
  install(12, "acme", acme),
  install(13, "fernhill-labs", [], { state: "suspended" }),
  install(14, "orbit-data", [], { fetch_state: "error" }),
  install(15, "northwind", [repo("northwind", 15, "forecasting")], { truncated: true }),
]
const grant = (r: ConnectRepo | string, extra: Partial<AgentRepo> = {}): AgentRepo => ({
  repo_id: typeof r === "string" ? next++ : r.id, full_name: typeof r === "string" ? r : r.full_name, scope: "agent", max_access: "write", baseline_access: "write", ceiling_access: "write",
  status: "active", added_by_account_id: null, added_at: ago(30), granted_by_account_id: null, granted_at: ago(30), is_working_repo: false, grant_version: 1, authorization_version: 1,
  installation_id: typeof r === "string" ? null : r.installation_id, ...extra,
})
const GRANTS = [
  grant(mmm, { is_working_repo: true }), grant(shared, { scope: "shared", max_access: "read", ceiling_access: "read" }), grant(mine[0]!),
  grant("acme/", { installation_id: 12 }), grant("northwind/", { installation_id: 15 }),
]

const editor = (extra: Partial<Extract<ConnectView, { kind: "agent_editor" }>> = {}, state = "editor"): ConnectView => {
  const visible = INSTALLATIONS.flatMap((i) => i.repos)
  const held = new Set(GRANTS.map((g) => g.repo_id))
  return {
    kind: "agent_editor", agent_name: "MMM Analyst", workspace_label: "Fernhill Labs", platform: "discord",
    confirm_url: "/oauth/github/confirm", check_url: `/oauth/github/confirm?state=${state}`, cancel_url: `/oauth/github/confirm?state=${state}&cancel=1`, install_url: "https://github.com/apps/acme-daimon/installations/new",
    state, invitation_hash: "inv", receipt: "r", snapshot: "s", snapshot_sig: "ss", visibility: "v", visibility_sig: "vs",
    installations: INSTALLATIONS, grants: GRANTS, visible_repos: visible, available: visible.filter((r) => !held.has(r.id)),
    repairable_repo_ids: [shared.id], needs_write: { [shared.id]: true }, requested_repo_id: null, working_repo_id: mmm.id,
    past_cap_repo_ids: [GRANTS[4]!.repo_id], owner_order: ["acme", "maya-l", "northwind"], owner_limit: 12, github_user_id: 110,
    pending_installation: null, selection_error: null, follow_offers: [], following: false, ...extra,
  }
}
const picker = (extra: Partial<Extract<ConnectView, { kind: "workspace_picker" }>> = {}, state = "picker"): ConnectView => ({
  kind: "workspace_picker", agent_name: null, workspace_label: "Fernhill Labs", platform: "slack",
  confirm_url: "/oauth/github/confirm", cancel_url: `/oauth/github/confirm?state=${state}&cancel=1`, install_url: "https://github.com/apps/acme-daimon/installations/new",
  state, invitation_hash: "inv", receipt: "r", installations: INSTALLATIONS, already_added_repo_ids: [mmm.id],
  needs_write: { [shared.id]: true, [pricing.id]: false }, requested_repo_id: null, selection_error: null, ...extra,
})

// An agent that works in one whole org only, so the "new repos go to the agent too" toggle can show:
// it appears once the agent holds every repo of Orbit Data (add the last one to see it).
const orbit = [repo("orbit-data", 16, "etl"), repo("orbit-data", 16, "dashboards", { private: true }), repo("orbit-data", 16, "forecast-notebooks")]
const orbitInstall = install(16, "orbit-data", orbit, { repository_selection: "all" })
const followEditor = (extra: Partial<Extract<ConnectView, { kind: "agent_editor" }>>, state: string): ConnectView => editor({
  installations: [orbitInstall], grants: [grant(orbit[0]!, { is_working_repo: true }), grant(orbit[1]!)], visible_repos: orbit, available: [orbit[2]!],
  repairable_repo_ids: [], needs_write: {}, working_repo_id: orbit[0]!.id, past_cap_repo_ids: [], owner_order: ["orbit-data"],
  follow_offers: [{ installation_id: 16, owner: "orbit-data", repo_ids: orbit.map((r) => r.id) }], ...extra,
}, state)

const SCENARIOS: Record<string, () => ConnectView> = {
  editor: () => editor(),
  "editor-follow": () => followEditor({}, "editor-follow"),
  "editor-following": () => followEditor({ follow_offers: [], following: true }, "editor-following"),
  "editor-requested": () => editor({ requested_repo_id: acme[7]!.id, pending_installation: { found: true, account_login: "Orbit Data" } }, "editor-requested"),
  "editor-error": () => editor({ selection_error: "Selection could not be verified." }, "editor-error"),
  "editor-fail": () => editor({}, "editor-fail"),
  picker: () => picker(),
  "picker-agent": () => picker({ agent_name: "MMM Analyst", platform: "discord" }, "picker-agent"),
  "picker-requested": () => picker({ requested_repo_id: acme[9]!.id, selection_error: "GitHub did not answer. Try again." }, "picker-requested"),
  install: () => ({ kind: "install", install_url: "https://github.com/apps/acme-daimon/installations/new", cancel_url: "/oauth/github/confirm?state=install&cancel=1" }),
  pending: () => ({ kind: "pending", check_url: "/oauth/github/confirm?state=pending", cancel_url: "/oauth/github/confirm?state=pending&cancel=1", organization: "Acme" }),
  "no-repos": () => ({ kind: "no_repos", share_link: "https://app.daimon.example/oauth/github/connect/abc", install_url: "https://github.com/apps/acme-daimon/installations/new" }),
  expired: () => ({ kind: "expired", requester_label: "Priya Raman" }),
  error: () => ({ kind: "error", message: "This link has expired.", status: 400, retry_url: null, action: null }),
  "error-retry": () => ({ kind: "error", message: "Selection could not be verified.", status: 400, retry_url: "/oauth/github/confirm?state=editor", action: null }),
  transient: () => ({ kind: "transient_error", message: "GitHub did not answer", status: 502, retry_url: "/oauth/github/confirm?state=editor", retry_selection: null }),
  cancelled: () => ({ kind: "cancelled", back }),
  already: () => ({ kind: "already_connected", count: 3, agent_name: null, back }),
  "already-agent": () => ({ kind: "already_connected", count: 2, agent_name: "MMM Analyst", back: { url: "/chat?agent=ag_mmm", platform: "web" } }),
  saved: () => saved(GRANTS),
  done: () => ({ kind: "done", count: 4, requester_label: "Priya Raman", same_person: true, agent_name: null, back: { url: "", platform: "slack" } }),
  "done-agent": () => ({ kind: "done", count: 2, requester_label: "Priya Raman", same_person: true, agent_name: "MMM Analyst", back }),
  "done-other": () => ({ kind: "done", count: 4, requester_label: "Priya Raman", same_person: false, agent_name: null, back: null }),
}
const saved = (grants: AgentRepo[], following = false): ConnectView => ({
  kind: "saved_agent", agent_name: "MMM Analyst", grants, visible_repo_ids: INSTALLATIONS.flatMap((i) => i.repos).map((r) => r.id), installations: INSTALLATIONS, back, following,
})

const page = (view: ConnectView): Outcome => ({ kind: "page", view })
const unknown = (): Outcome => page({ kind: "error", message: "This link has expired.", status: 400, retry_url: null, action: null })
const scenario = (name: string): Outcome => { const s = SCENARIOS[name]; return s ? page(s()) : unknown() }

let failed = false
/** The page a save leads to: the saved agent, the done page, or one failure that a retry then clears. */
function save(body: Selection): Outcome {
  if (body.state === "editor-fail" && !failed) {
    failed = true
    return page({ kind: "transient_error", message: "GitHub did not answer", status: 502, retry_url: "/oauth/github/confirm", retry_selection: body })
  }
  if (body.state.startsWith("editor-follow")) {
    const adds = orbit.filter((r) => body.repo.includes(String(r.id))).map((r) => grant(r))
    return page(saved([grant(orbit[0]!, { is_working_repo: true }), grant(orbit[1]!), ...adds], !!body.follow_org?.length))
  }
  if (body.state.startsWith("picker")) return scenario(body.state.endsWith("agent") ? "done-agent" : "done")
  const view = editor() as Extract<ConnectView, { kind: "agent_editor" }>
  const adds = view.available.filter((r) => body.repo.includes(String(r.id))).map((r) => grant(r))
  return page(saved([...GRANTS.filter((g) => !body.remove.includes(String(g.repo_id))), ...adds]))
}

const API = "/api/github/connect"
export const githubConnectHandlers = [
  http.post(`${API}/start`, async ({ request }) => {
    await wait()
    const { token } = (await request.json()) as { token: string }
    const confirm = token.startsWith("editor") || token.startsWith("picker")
    return HttpResponse.json(confirm ? { kind: "redirect", url: `${new URL(request.url).origin}/oauth/github/confirm?state=${token}` } : scenario(token))
  }),
  http.post(`${API}/callback`, async ({ request }) => { await wait(); return HttpResponse.json(scenario(((await request.json()) as { state: string }).state)) }),
  http.post(`${API}/setup`, async ({ request }) => { await wait(); return HttpResponse.json(scenario(((await request.json()) as { state: string }).state)) }),
  http.get(`${API}/view`, async ({ request }) => { await wait(); return HttpResponse.json(scenario(new URL(request.url).searchParams.get("state") ?? "")) }),
  http.post(`${API}/cancel`, async () => { await wait(); return HttpResponse.json(scenario("cancelled")) }),
  http.post(`${API}/save`, async ({ request }) => { await wait(500); return HttpResponse.json(save((await request.json()) as Selection)) }),
]
