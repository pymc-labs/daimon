import { screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, describe, expect, it, vi } from "vitest"

import { json, renderAt } from "@/test/render"
import { GithubConfirm } from "./Flow"
import type { AgentEditorView, ConnectRepo } from "./types"

const repo = (id: number, name: string): ConnectRepo => ({ id, owner_id: 1, installation_id: 7, full_name: `acme/${name}`, permission: "write", private: false, archived: false })
const grant = (r: ConnectRepo, working = false) => ({
  repo_id: r.id, full_name: r.full_name, scope: "agent", max_access: "write", baseline_access: "write", ceiling_access: "write", status: "active",
  added_by_account_id: null, added_at: "", granted_by_account_id: null, granted_at: "", is_working_repo: working, grant_version: 1, authorization_version: 1, installation_id: 7,
})
const [a, b, c] = [repo(1, "alpha"), repo(2, "beta"), repo(3, "gamma")] as const
const editor = {
  kind: "agent_editor", agent_name: "Analyst", workspace_label: "Fernhill", platform: "discord", confirm_url: "", check_url: "", cancel_url: "", install_url: "",
  state: "st", invitation_hash: "inv", receipt: "rc", snapshot: "sn", snapshot_sig: "ss", visibility: "vi", visibility_sig: "vs",
  installations: [{ id: 7, owner_id: 1, owner_login: "acme", repository_selection: "selected", repos: [a, b, c], owner_type: "Organization", avatar_url: null, state: "active", total: 3, truncated: false, fetch_state: "ok" }],
  grants: [grant(a, true), grant(b)], visible_repos: [a, b, c], available: [c], repairable_repo_ids: [], needs_write: {},
  requested_repo_id: null, working_repo_id: 1, past_cap_repo_ids: [], owner_order: ["acme"], owner_limit: 12, github_user_id: null, pending_installation: null, selection_error: null, follow_offers: [], following: false,
} as unknown as AgentEditorView

afterEach(() => vi.unstubAllGlobals())
const server = (routes: Record<string, () => Response>) => {
  const f = vi.fn(async (path: string, init?: RequestInit) => (routes[`${init?.method ?? "GET"} ${path.split("?")[0]}`] ?? (() => json({ detail: "No" }, 404)))())
  vi.stubGlobal("fetch", f)
  return f
}
const open = (query: string) => renderAt(`/oauth/github/confirm?${query}`, "/oauth/github/confirm", <GithubConfirm />)

describe("github connect", () => {
  it("saves the editor's removals, additions and working repo with the signed fields", async () => {
    const f = server({
      "GET /api/github/connect/view": () => json({ kind: "page", view: editor }),
      "POST /api/github/connect/save": () => json({ kind: "page", view: { kind: "done", count: 2, requester_label: "Maya", same_person: true, agent_name: "Analyst", back: null } }),
    })
    open("state=st")
    await userEvent.click(await screen.findByRole("button", { name: "Remove acme/beta" }))
    await userEvent.click(screen.getByRole("button", { name: "Add repos" }))
    await userEvent.click(await screen.findByRole("checkbox", { name: /gamma/ }))
    await userEvent.click(screen.getByRole("button", { name: "Add 1 repo" }))
    await userEvent.click(screen.getByRole("button", { name: "Change" }))
    await userEvent.click(screen.getByRole("radio", { name: /gamma/ }))
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }))
    expect(await screen.findByRole("heading", { name: "Repos connected" })).toBeInTheDocument()
    const body = JSON.parse(f.mock.calls.find(([p]) => p === "/api/github/connect/save")![1]!.body as string)
    expect(body).toEqual({
      state: "st", invitation: "inv", receipt: "rc", snapshot: "sn", snapshot_sig: "ss", visibility: "vi", visibility_sig: "vs",
      repo: ["3"], remove: ["2"], access: ["write"], working: ["3"], repo_access: {},
    })
  }, 30_000)

  describe("follow the org", () => {
    // The agent holds alpha and beta; gamma is the rest of the org, so adding it covers the whole org.
    const offered = { ...editor, follow_offers: [{ installation_id: 7, owner: "acme", repo_ids: [1, 2, 3] }] } as AgentEditorView
    const toggle = () => screen.queryByRole("checkbox", { name: "New acme repos go to Analyst too." })
    const addGamma = async () => {
      await userEvent.click(screen.getByRole("button", { name: "Add repos" }))
      await userEvent.click(await screen.findByRole("checkbox", { name: /gamma/ }))
      await userEvent.click(screen.getByRole("button", { name: "Add 1 repo" }))
    }
    const saveBody = (f: ReturnType<typeof server>) => JSON.parse(f.mock.calls.find(([p]) => p === "/api/github/connect/save")![1]!.body as string)
    const routes = (view: AgentEditorView) => ({
      "GET /api/github/connect/view": () => json({ kind: "page", view }),
      "POST /api/github/connect/save": () => json({ kind: "page", view: { kind: "done", count: 3, requester_label: "Maya", same_person: true, agent_name: "Analyst", back: null } }),
    })

    it("shows once the agent would hold the whole org, and hides when a repo is taken off", async () => {
      server(routes(offered))
      open("state=st")
      await screen.findByRole("button", { name: "Remove acme/beta" })
      expect(toggle()).toBeNull()
      await addGamma()
      const box = await screen.findByRole("checkbox", { name: "New acme repos go to Analyst too." })
      expect(box.className).toContain("min-h-11")
      await userEvent.click(screen.getByRole("button", { name: "Remove acme/gamma" }))
      expect(toggle()).toBeNull()
    })

    it("sends the org with the save only when ticked", async () => {
      const f = server(routes(offered))
      open("state=st")
      await screen.findByRole("button", { name: "Remove acme/beta" })
      await addGamma()
      await userEvent.click(await screen.findByRole("checkbox", { name: "New acme repos go to Analyst too." }))
      await userEvent.click(screen.getByRole("button", { name: "Save changes" }))
      await screen.findByRole("heading", { name: "Repos connected" })
      expect(saveBody(f).follow_org).toEqual(["7"])
    })

    it("is not shown when the core view offers nothing, even with the whole org held", async () => {
      server(routes(editor))
      open("state=st")
      await screen.findByRole("button", { name: "Remove acme/beta" })
      await addGamma()
      expect(toggle()).toBeNull()
    })

    it("says so when the agent already follows its org", async () => {
      server(routes({ ...editor, following: true } as AgentEditorView))
      open("state=st")
      expect(await screen.findByText("Analyst gets new repos added to its org on its own.")).toBeInTheDocument()
    })
  })

  it("shows a status view and redirects when the step says so", async () => {
    server({ "GET /api/github/connect/view": () => json({ kind: "page", view: { kind: "expired", requester_label: "Priya" } }) })
    open("state=x")
    expect(await screen.findByRole("heading", { name: "This link has expired" })).toBeInTheDocument()
    expect(screen.getByText("Ask Priya for a new link")).toBeInTheDocument()
  })

  it("follows a redirect outcome", async () => {
    const replace = vi.fn()
    vi.stubGlobal("location", { ...window.location, replace, origin: window.location.origin })
    server({ "GET /api/github/connect/view": () => json({ kind: "redirect", url: "https://github.com/apps/x/installations/new" }) })
    open("state=y")
    await waitFor(() => expect(replace).toHaveBeenCalledWith("https://github.com/apps/x/installations/new"))
  })
})
