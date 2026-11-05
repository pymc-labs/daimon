import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { api } from "@/lib/api"
import type { Agent, AgentConfig, ServerSkillMenu, Skill, SkillFollow } from "../types"
import type { Props } from "./parts"
import { SkillEditor, SkillsEditor } from "./SkillEditors"

vi.mock("sonner", () => ({ toast: Object.assign(vi.fn(), { error: vi.fn() }) }))
vi.mock("@/lib/api", async (orig) => ({ ...(await orig<typeof import("@/lib/api")>()), api: { get: vi.fn(), put: vi.fn(), post: vi.fn(), del: vi.fn() } }))

const plots: Skill = { type: "custom", skill_id: "sk_plots", title: "Plots", description: "Charts", version: "1" }
const menu: ServerSkillMenu = {
  label: "Data server",
  skills: [
    { uri: "skill://plots", name: "plots", description: "Charts", skill_id: "sk_plots", taken: false },
    { uri: "skill://notes", name: "notes", description: "Notes", skill_id: null, taken: false },
    { uri: "skill://review", name: "review", description: "Reviews", skill_id: null, taken: true },
  ],
}
let follows: Record<string, SkillFollow> = {}

const show = (ui: (p: Props) => React.ReactElement, skills: Skill[] = [plots]) => {
  const config = { skills, mcp_servers: [] } as unknown as AgentConfig
  const update = vi.fn((fn: (c: AgentConfig) => void) => fn(config))
  const p: Props = { node: "skills", agent: { id: "ag_1" } as Agent, config, catalog: { models: [], skills: [], channels: [], tools: [] } as never, ro: false, changed: new Set(), update, select: vi.fn() }
  render(<QueryClientProvider client={new QueryClient()}>{ui(p)}</QueryClientProvider>)
  return config
}

beforeEach(() => {
  vi.clearAllMocks()
  follows = { sk_plots: { follow: true, status: "current", version: "1" } }
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} })
  Element.prototype.scrollIntoView = vi.fn()
  vi.mocked(api.get).mockImplementation((url: string) =>
    Promise.resolve(url.endsWith("/server-skills") ? menu : url.endsWith("/skill-follows") ? follows
      : url.endsWith("/github") ? { configured: false, can_manage: false, can_connect: false, repos: [], sync: null, skills: [], release: null }
      : { files: [] }) as never)
})

describe("SkillsEditor", () => {
  it("offers the server's skills under its label and adds one at its version", async () => {
    vi.mocked(api.post).mockResolvedValueOnce({ id: "sk_notes", name: "notes", version: "4" } as never)
    const config = show((p) => <SkillsEditor {...p} />)
    expect(await screen.findByText("Data server")).toBeInTheDocument()
    expect(await screen.findByText("Following")).toBeInTheDocument()
    expect(screen.getByText("Name taken")).toBeInTheDocument()
    expect(screen.queryByText("Charts", { selector: ".line-clamp-1" })).not.toBeInTheDocument()
    await userEvent.click(screen.getByText("Notes", { selector: "span.block" }))
    await waitFor(() => expect(config.skills).toHaveLength(2))
    expect(api.post).toHaveBeenCalledWith("/api/agents/server-skills", { uri: "skill://notes" })
    expect(config.skills[1]).toEqual({ type: "custom", skill_id: "sk_notes", title: "Notes", description: "Notes", version: "4" })
  })
})

describe("SkillEditor", () => {
  it("pins with the toggle and offers a pinned agent the update", async () => {
    follows = { sk_plots: { follow: false, status: "current", version: "2" } }
    const config = show((p) => <SkillEditor {...p} skill={plots} />)
    expect(await screen.findByText("Update available")).toBeInTheDocument()
    await userEvent.click(screen.getByRole("button", { name: "Update to v2" }))
    expect(config.skills[0]!.version).toBe("2")
    await userEvent.click(screen.getByRole("switch"))
    expect(api.put).toHaveBeenCalledWith("/api/agents/ag_1/skill-follows", { skill_id: "sk_plots", follow: true })
  })

  it("offers a held update to the owner, who then re-confirms the backend", async () => {
    follows = { sk_plots: { follow: true, status: "needs_reconfirm", version: "2" } }
    vi.mocked(api.post).mockResolvedValueOnce(undefined as never)
    show((p) => <SkillEditor {...p} skill={plots} />)
    expect(await screen.findByText("Needs re-confirm")).toBeInTheDocument()
    await userEvent.click(screen.getByRole("button", { name: "Update to v2" }))
    expect(api.post).toHaveBeenCalledWith("/api/agents/ag_1/skill-follows/sk_plots/apply")
  })

  it("shows an unavailable skill and keeps it read-only", async () => {
    follows = { sk_plots: { follow: true, status: "unavailable", version: "1" } }
    show((p) => <SkillEditor {...p} skill={plots} />)
    expect(await screen.findByText("Unavailable")).toBeInTheDocument()
    expect(screen.queryByLabelText("SKILL.md")).not.toBeInTheDocument()
  })
})
