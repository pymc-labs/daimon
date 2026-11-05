import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { MemoryRouter, Route, Routes } from "react-router"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { ConfirmProvider } from "@/components/confirm"
import { ApiError, api } from "@/lib/api"
import type { SkillDetail } from "../types"
import { SkillPage } from "./SkillPage"

vi.mock("sonner", () => ({ toast: Object.assign(vi.fn(), { error: vi.fn() }) }))
vi.mock("@/lib/api", async (orig) => ({ ...(await orig<typeof import("@/lib/api")>()), api: { get: vi.fn(), put: vi.fn(), post: vi.fn(), del: vi.fn() } }))

const skill = (version: string): SkillDetail => ({
  id: "sk_1", name: "review", title: "Review", description: "Reviews", source: "custom", source_url: null, default: false,
  version, created_at: "2030-01-01T00:00:00Z", updated_at: "2030-01-01T00:00:00Z", used_by: [],
})

let latest = "1"
let server: SkillDetail["server"] = null
const serve = () => vi.mocked(api.get).mockImplementation((url: string) => {
  if (url === "/api/me") return Promise.resolve({ capabilities: [], workspace: { name: "Workspace" } }) as never
  if (url === "/api/agents/skills") return Promise.resolve([skill(latest), { ...skill("1"), id: "sk_2", name: "review-copy" }]) as never
  if (url.endsWith("/versions")) return Promise.resolve([]) as never
  if (url.includes("/files")) return Promise.resolve({ skill_id: "sk_1", version: latest, files: [{ path: "SKILL.md", size: 5, content: `v${latest}` }] }) as never
  return Promise.resolve({ ...skill(latest), server }) as never
})

const show = () => render(
  <QueryClientProvider client={new QueryClient()}>
    <MemoryRouter initialEntries={["/agents/skills/sk_1"]}><ConfirmProvider>
      <Routes><Route path="/agents/skills/:id" element={<SkillPage />} /></Routes>
    </ConfirmProvider></MemoryRouter>
  </QueryClientProvider>,
)

beforeEach(() => {
  vi.clearAllMocks()
  latest = "1"
  server = null
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} })
  serve()
})

describe("SkillPage", () => {
  it("keeps the draft when the skill moves on, and offers Reload or Overwrite", async () => {
    vi.mocked(api.put).mockImplementationOnce(() => { latest = "2"; return Promise.reject(new ApiError(409, "changed")) })
    show()
    const editor = await screen.findByLabelText("SKILL.md")
    await userEvent.type(editor, " mine")
    await userEvent.click(screen.getByRole("button", { name: "Save version" }))
    expect(await screen.findByText(/This skill changed/)).toBeInTheDocument()
    expect(vi.mocked(api.put).mock.calls[0]![1]).toMatchObject({ expected_version: "1" })
    expect(screen.getByLabelText("SKILL.md")).toHaveValue("v1 mine")
    expect(screen.queryByRole("button", { name: "Save version" })).not.toBeInTheDocument()
    vi.mocked(api.put).mockResolvedValueOnce({ id: "sk_1", name: "review", version: "3" } as never)
    await userEvent.click(screen.getByRole("button", { name: "Overwrite" }))
    await waitFor(() => expect(api.put).toHaveBeenCalledTimes(2))
    expect(vi.mocked(api.put).mock.calls[1]![1]).toMatchObject({ expected_version: "2" })
  })

  it("drops the draft on Reload", async () => {
    vi.mocked(api.put).mockImplementationOnce(() => { latest = "2"; return Promise.reject(new ApiError(409, "changed")) })
    show()
    await userEvent.type(await screen.findByLabelText("SKILL.md"), " mine")
    await userEvent.click(screen.getByRole("button", { name: "Save version" }))
    await userEvent.click(await screen.findByRole("button", { name: "Reload" }))
    await waitFor(() => expect(screen.getByLabelText("SKILL.md")).toHaveValue("v2"))
    expect(screen.queryByText(/This skill changed/)).not.toBeInTheDocument()
  })

  it("keeps a server skill read-only and approves its waiting update", async () => {
    server = {
      server: "data", uri: "skill://review", version_label: "1.0", status: "pending", pending_digest: "d2", pending_label: "1.1",
      pending_changes: { added: ["run.py"], removed: [], changed: ["SKILL.md"] }, last_checked_at: null,
    }
    vi.mocked(api.post).mockResolvedValueOnce({ result: "updated", server: { ...server, status: "current" } } as never)
    show()
    expect(await screen.findByText("Needs approval")).toBeInTheDocument()
    expect(screen.getByText("run.py")).toBeInTheDocument()
    expect(screen.getByText("Skills server")).toBeInTheDocument()
    await userEvent.type(await screen.findByLabelText("SKILL.md"), " mine")
    expect(screen.queryByRole("button", { name: "Save version" })).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole("button", { name: "Approve update" }))
    expect(api.post).toHaveBeenCalledWith("/api/agents/skills/sk_1/approve", { digest: "d2" })
  })

  it("forks a server skill under a name no other skill holds", async () => {
    server = {
      server: "data", uri: "skill://review", version_label: "1.0", status: "current", pending_digest: null, pending_label: null,
      pending_changes: null, last_checked_at: null,
    }
    vi.mocked(api.post).mockResolvedValueOnce({ id: "sk_3", name: "review-mine", version: "1" } as never)
    show()
    await userEvent.click(await screen.findByRole("button", { name: "More" }))
    await userEvent.click(await screen.findByRole("menuitem", { name: "Fork" }))
    const name = await screen.findByLabelText("Name")
    expect(name).toHaveValue("review-copy")
    expect(await screen.findByText("review-copy is taken")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Fork" })).toBeDisabled()
    await userEvent.clear(name)
    await userEvent.type(name, "Review mine")
    await userEvent.click(screen.getByRole("button", { name: "Fork" }))
    expect(api.post).toHaveBeenCalledWith("/api/agents/skills/sk_1/fork", { name: "review-mine" })
  })
})
