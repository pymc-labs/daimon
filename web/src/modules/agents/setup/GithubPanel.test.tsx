import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render, screen, waitFor, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { ConfirmProvider } from "@/components/confirm"
import { TooltipProvider } from "@/components/ui/tooltip"
import { api } from "@/lib/api"
import type { Agent } from "../types"
import type { AgentGithub } from "./github"
import { GithubFacts, GithubPanel } from "./GithubPanel"

vi.mock("@/lib/api", () => ({ api: { get: vi.fn(), put: vi.fn(), post: vi.fn(), del: vi.fn() } }))

const agent = { id: "ag_w", can_view_details: true } as Agent
const select = vi.fn()
const props = { node: "github", agent, config: agent, ro: false, changed: new Set<string>(), update: () => {}, select }
const AT = "2026-11-01T00:00:00Z"

const repo = (name: string, extra: Partial<AgentGithub["repos"][number]> = {}): AgentGithub["repos"][number] => ({
  name, url: `https://github.com/${name}`, access: "read", working: false, shared: false, status: "active", installation: "active",
  granted_at: AT, granted_by: "Ada", ...extra,
})
const full: AgentGithub = {
  configured: true, can_manage: true, can_connect: true,
  repos: [repo("acme/data", { access: "write", working: true }), repo("acme/shared", { shared: true })],
  sync: { state: "done", at: AT, error: null },
  skills: [
    { skill_id: "a", title: "Data checks", version: "v3", origin: "repo", repo: "acme/data", branch: "main", path: "skills/a", url: "https://github.com/acme/data/tree/main/skills/a", updated_at: AT },
    { skill_id: "c", title: "Cleanup", version: null, origin: "repo", repo: "Acme/Data", branch: "main", path: "skills/c", url: null, updated_at: AT },
    { skill_id: "b", title: "Notes", version: null, origin: "external", repo: null, branch: null, path: null, url: null, updated_at: AT },
  ],
  release: { name: "v2.4.1", at: AT, url: "https://github.com/acme/data/releases/tag/v2.4.1" },
}

const view = () =>
  render(<QueryClientProvider client={new QueryClient()}><TooltipProvider><ConfirmProvider><GithubPanel {...props} /></ConfirmProvider></TooltipProvider></QueryClientProvider>)
const serve = (g: AgentGithub) => vi.mocked(api.get).mockResolvedValue(g as never)

beforeEach(() => {
  vi.clearAllMocks()
  vi.unstubAllGlobals()
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} })
})

describe("GithubPanel", () => {
  it("shows repos and the sync, with no skill or release sections", async () => {
    serve(full)
    view()
    expect(await screen.findByRole("link", { name: "acme/data" })).toHaveAttribute("href", "https://github.com/acme/data")
    expect(screen.getByLabelText("Working repo")).toBeInTheDocument()
    expect(screen.getByText("Can change")).toBeInTheDocument()
    expect(screen.getByText("Shared")).toBeInTheDocument()
    expect(screen.getByText("Synced")).toBeInTheDocument()
    expect(screen.queryByText("Skills")).toBeNull()
    expect(screen.queryByText("Data checks")).toBeNull()
    expect(screen.queryByText("Release")).toBeNull()
    expect(screen.queryByText("v2.4.1")).toBeNull()
  })

  it("counts each repo's skills by name and opens the Skills node", async () => {
    serve(full)
    view()
    await userEvent.click(await screen.findByRole("button", { name: "2 skills" }))
    expect(select).toHaveBeenCalledWith("skills")
    expect(screen.queryByRole("button", { name: /^1 skill/ }), "a repo with no skills shows no count").toBeNull()
  })

  it("says 1 skill for one", async () => {
    serve({ ...full, skills: [full.skills[0]!] })
    view()
    expect(await screen.findByRole("button", { name: "1 skill" })).toBeInTheDocument()
  })

  it("leaves out the sync when there is none, and names a failing repo", async () => {
    serve({ ...full, release: null, sync: null, can_manage: false, repos: [repo("acme/data", { status: "suspended", installation: "removed" })] })
    view()
    expect(await screen.findByText("Install removed")).toBeInTheDocument()
    expect(screen.queryByText("Sync")).toBeNull()
  })

  it("links only https addresses", async () => {
    serve({
      ...full,
      repos: [repo("acme/data", { url: "javascript:alert(1)" })],
    })
    view()
    expect(await screen.findByText("acme/data")).toBeInTheDocument()
    expect(screen.queryAllByRole("link"), "a non-https address renders as text").toEqual([])
  })

  it("shows the sync error", async () => {
    serve({ ...full, sync: { state: "done", at: AT, error: "GitHub says the install is paused" } })
    view()
    expect(await screen.findByText("GitHub says the install is paused")).toBeInTheDocument()
    expect(screen.getByText("Failed")).toBeInTheDocument()
  })

  it("offers no actions to someone who cannot manage", async () => {
    serve({ ...full, can_manage: false, can_connect: false })
    view()
    await screen.findByText("acme/data")
    expect(screen.getByText("View only")).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: /Actions|Connect|Resync/ })).toBeNull()
  })

  it("offers resync but not connect to someone who can manage only", async () => {
    serve({ ...full, can_connect: false })
    view()
    expect(await screen.findByRole("button", { name: "Resync now" }), "managing allows a resync").toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "Connect" }), "connecting needs its own right").toBeNull()
    expect(screen.queryByText("View only"), "a manager is not view only").toBeNull()
  })

  it("is plain facts when GitHub is not set up", async () => {
    serve({ ...full, configured: false, repos: [], skills: [], sync: null, release: null })
    view()
    expect(await screen.findByText("GitHub isn't set up on this server")).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: /connect/i })).toBeNull()
  })

  it("says None for an agent with no repos", async () => {
    serve({ ...full, repos: [], skills: [], sync: null, release: null })
    view()
    expect(await screen.findByText("None")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Connect" })).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "Resync now" })).toBeNull()
  })

  it("sets the working repo", async () => {
    serve(full)
    vi.mocked(api.put).mockResolvedValue({ ...full, repos: [repo("acme/data"), repo("acme/shared", { working: true })] } as never)
    view()
    await userEvent.click(await screen.findByRole("button", { name: "Actions for acme/shared" }))
    await userEvent.click(await screen.findByRole("menuitem", { name: "Set as working repo" }))
    await waitFor(() => expect(api.put).toHaveBeenCalledWith("/api/agents/ag_w/github/working", { repo: "acme/shared" }))
  })

  it("removes a repo only after a confirm", async () => {
    serve(full)
    vi.mocked(api.del).mockResolvedValue({ ...full, repos: [repo("acme/data", { working: true })] } as never)
    view()
    await userEvent.click(await screen.findByRole("button", { name: "Actions for acme/shared" }))
    await userEvent.click(await screen.findByRole("menuitem", { name: "Remove" }))
    expect(api.del).not.toHaveBeenCalled()
    await userEvent.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Remove" }))
    await waitFor(() => expect(api.del).toHaveBeenCalledWith("/api/agents/ag_w/github/repos/acme/shared"))
    await waitFor(() => expect(screen.queryByText("acme/shared")).toBeNull())
  })

  it("resyncs and lists what happened to each skill", async () => {
    serve(full)
    vi.mocked(api.post).mockResolvedValue({ results: [{ name: "data-checks", action: "updated", detail: null }, { name: "notes", action: "failed", detail: "No SKILL.md" }] } as never)
    view()
    await userEvent.click(await screen.findByRole("button", { name: "Resync now" }))
    await waitFor(() => expect(api.post).toHaveBeenCalledWith("/api/agents/ag_w/github/resync"))
    expect(await screen.findByText("Updated")).toBeInTheDocument()
    expect(screen.getByText("Failed, No SKILL.md")).toBeInTheDocument()
  })

  it("goes to the connect link in this tab", async () => {
    serve(full)
    vi.mocked(api.post).mockResolvedValue({ url: "https://github.com/apps/acme/installations/new" } as never)
    const assign = vi.fn()
    vi.stubGlobal("location", { ...window.location, assign })
    view()
    await userEvent.click(await screen.findByRole("button", { name: "Connect" }))
    await waitFor(() => expect(assign).toHaveBeenCalledWith("https://github.com/apps/acme/installations/new"))
  })
})

const card = () =>
  render(<QueryClientProvider client={new QueryClient()}><TooltipProvider><GithubFacts agent={agent} /></TooltipProvider></QueryClientProvider>)

describe("GithubFacts", () => {
  it("shows the working repo and when it last synced, not the release", async () => {
    serve(full)
    card()
    expect(await screen.findByText("acme/data")).toBeInTheDocument()
    expect(screen.getByText(/^Synced/)).toBeInTheDocument()
    expect(screen.queryByText("v2.4.1")).toBeNull()
    expect(screen.queryByText(/Skills changed/)).toBeNull()
  })

  it("says the sync failed", async () => {
    serve({ ...full, sync: { state: "done", at: AT, error: "paused" } })
    card()
    expect(await screen.findByText("Sync failed")).toBeInTheDocument()
  })

  it("shows Not connected and no sync line without a sync", async () => {
    serve({ ...full, repos: [], sync: null })
    card()
    expect(await screen.findByText("Not connected")).toBeInTheDocument()
    expect(screen.queryByText(/Sync/)).toBeNull()
  })
})
