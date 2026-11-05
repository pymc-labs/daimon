import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render, screen } from "@testing-library/react"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { TooltipProvider } from "@/components/ui/tooltip"
import { api } from "@/lib/api"
import type { Agent } from "../types"
import type { AgentGithub, GithubSkill } from "./github"
import { SkillSource, SkillsRelease } from "./SkillSource"

vi.mock("@/lib/api", () => ({ api: { get: vi.fn() } }))

const AT = "2026-11-01T00:00:00Z"
const agent = { id: "ag_w", can_view_details: true } as Agent
const skill = (extra: Partial<GithubSkill>): GithubSkill => ({
  skill_id: "a", title: "Data checks", version: null, origin: "built_in", repo: null, branch: null, path: null, url: null, updated_at: AT, ...extra,
})
const link = (skills: GithubSkill[], release: AgentGithub["release"] = null): AgentGithub => ({
  configured: true, can_manage: false, can_connect: false, repos: [], sync: null, skills, release,
})
const serve = (g: AgentGithub) => vi.mocked(api.get).mockResolvedValue(g as never)
const wrap = (ui: React.ReactNode) => render(<QueryClientProvider client={new QueryClient()}><TooltipProvider>{ui}</TooltipProvider></QueryClientProvider>)

beforeEach(() => {
  vi.clearAllMocks()
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} })
})

describe("SkillSource", () => {
  it("links a repo skill's address and says when it was updated", async () => {
    serve(link([skill({ origin: "repo", repo: "acme/data", branch: "main", path: "skills/a", url: "https://github.com/acme/data/tree/main/skills/a" })]))
    wrap(<SkillSource agent={agent} skillId="a" />)
    expect(await screen.findByRole("link", { name: "acme/data@main/skills/a" })).toHaveAttribute("href", "https://github.com/acme/data/tree/main/skills/a")
    expect(screen.getByText(/^Updated/)).toBeInTheDocument()
  })

  it("shows a repo address as text unless it is https", async () => {
    serve(link([skill({ origin: "repo", repo: "acme/data", branch: "main", path: "skills/a", url: "javascript:alert(1)" })]))
    wrap(<SkillSource agent={agent} skillId="a" />)
    expect(await screen.findByText("acme/data@main/skills/a")).toBeInTheDocument()
    expect(screen.queryByRole("link")).toBeNull()
  })

  it.each([["library", "Library"], ["external", "Published outside Daimon"]] as const)("names a %s skill", async (origin, label) => {
    serve(link([skill({ origin })]))
    wrap(<SkillSource agent={agent} skillId="a" />)
    expect(await screen.findByText(label)).toBeInTheDocument()
    expect(screen.getByText(/^Updated/)).toBeInTheDocument()
  })

  it("leaves out Updated when no time is known", async () => {
    serve(link([skill({ origin: "library", updated_at: null })]))
    wrap(<SkillSource agent={agent} skillId="a" />)
    expect(await screen.findByText("Library")).toBeInTheDocument()
    expect(screen.queryByText(/Updated/)).toBeNull()
  })

  it("shows nothing for a built-in skill or one the link does not list", async () => {
    serve(link([skill({ origin: "built_in" })]))
    const { container } = wrap(<><SkillSource agent={agent} skillId="a" /><SkillSource agent={agent} skillId="zz" /></>)
    await vi.waitFor(() => expect(api.get).toHaveBeenCalled())
    expect(container).toBeEmptyDOMElement()
  })

  it("shows nothing and asks for nothing without setup access", () => {
    const { container } = wrap(<><SkillSource agent={{ ...agent, can_view_details: false }} skillId="a" /><SkillsRelease agent={{ ...agent, can_view_details: false }} /></>)
    expect(container).toBeEmptyDOMElement()
    expect(api.get).not.toHaveBeenCalled()
  })
})

describe("SkillsRelease", () => {
  it("shows the release name as a link and when it was made", async () => {
    serve(link([], { name: "v2.4.1", at: AT, url: "https://github.com/acme/data/releases/tag/v2.4.1" }))
    wrap(<SkillsRelease agent={agent} />)
    expect(await screen.findByText("Release")).toBeInTheDocument()
    expect(screen.getByRole("link", { name: "v2.4.1" })).toHaveAttribute("href", "https://github.com/acme/data/releases/tag/v2.4.1")
  })

  it("shows a non-https release as text", async () => {
    serve(link([], { name: "v2.4.1", at: AT, url: "data:text/html,hi" }))
    wrap(<SkillsRelease agent={agent} />)
    expect(await screen.findByText("v2.4.1")).toBeInTheDocument()
    expect(screen.queryByRole("link")).toBeNull()
  })

  it("shows nothing without a release", async () => {
    serve(link([]))
    const { container } = wrap(<SkillsRelease agent={agent} />)
    await vi.waitFor(() => expect(api.get).toHaveBeenCalled())
    expect(container).toBeEmptyDOMElement()
  })
})
