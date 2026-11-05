import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { MemoryRouter, Route, Routes } from "react-router"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { ConfirmProvider } from "@/components/confirm"
import { ApiError, api } from "@/lib/api"
import type { SkillDetail } from "../types"
import { SkillPage } from "./SkillPage"

vi.mock("sonner", () => ({ toast: { error: vi.fn() } }))
vi.mock("@/lib/api", async (orig) => ({ ...(await orig<typeof import("@/lib/api")>()), api: { get: vi.fn(), put: vi.fn(), post: vi.fn(), del: vi.fn() } }))

const skill = (version: string, source: SkillDetail["source"] = "custom"): SkillDetail => ({
  id: "sk_1", name: "review", title: "Review", description: "Reviews", source, source_url: null, default: false,
  version, created_at: "2030-01-01T00:00:00Z", updated_at: "2030-01-01T00:00:00Z", used_by: [],
})

let latest = "1"
let source: SkillDetail["source"] = "custom"
const serve = () => vi.mocked(api.get).mockImplementation((url: string) => {
  if (url === "/api/me") return Promise.resolve({ capabilities: [], workspace: { name: "Workspace" } }) as never
  if (url.endsWith("/versions")) return Promise.resolve([]) as never
  if (url.includes("/files")) return Promise.resolve({ skill_id: "sk_1", version: latest, files: [{ path: "SKILL.md", size: 5, content: `v${latest}` }] }) as never
  return Promise.resolve(skill(latest, source)) as never
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
  source = "custom"
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

  it("reads files only for a custom skill", async () => {
    source = "anthropic"
    const { unmount } = show()
    expect(await screen.findByText("The files of this skill are not available.")).toBeInTheDocument()
    expect(vi.mocked(api.get).mock.calls.some(([url]) => String(url).includes("/files"))).toBe(false)
    unmount()
    source = "custom"
    show()
    expect(await screen.findByLabelText("SKILL.md")).toHaveValue("v1")
    expect(vi.mocked(api.get).mock.calls.some(([url]) => String(url).includes("/files"))).toBe(true)
  })
})
