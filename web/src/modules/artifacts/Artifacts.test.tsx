import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { MemoryRouter } from "react-router"
import { describe, expect, it, vi } from "vitest"

import { TooltipProvider } from "@/components/ui/tooltip"
import { api } from "@/lib/api"
import type { Me } from "@/lib/types"
import { filterArtifacts } from "./Artifacts"
import { Data } from "./Data"
import { refused } from "./rules"
import { ShareDialog } from "./Share"
import type { ArtifactDetail, ArtifactSummary } from "./types"

const row = (over: Partial<ArtifactSummary>): ArtifactSummary => ({
  id: "a1", slug: "a1", title: "Run sheet", url: "/a/a1/a1", agent: { id: "ag1", name: "Daimon" }, owner: { id: "p1", name: "Maya", kind: "member" }, mine: true, role: "owner",
  access: "workspace", people_count: 0, data: "off", events: false, ask: false, version: 1, size_bytes: 1, updated_at: "2026-11-01T00:00:00Z", created_at: "2026-11-01T00:00:00Z", expires_at: null, ...over,
})
const none: Parameters<typeof filterArtifacts>[1] = { q: "", agents: [], owners: [], access: [] }

describe("artifact list filter", () => {
  const rows = [row({ id: "a" }), row({ id: "b", title: "Pricing review", agent: { id: "ag2", name: "Pricing Analyst" }, access: "public", mine: false, owner: { id: "p2", name: "Priya", kind: "member" } })]
  const ids = (f: Partial<typeof none>) => filterArtifacts(rows, { ...none, ...f }).map((a) => a.id)

  it("matches title and agent text, case-insensitively", () => {
    expect(ids({ q: "PRICING" })).toEqual(["b"])
    expect(ids({ q: "daimon" })).toEqual(["a"])
  })
  it("narrows by agent, owner and access together", () => {
    expect(ids({ agents: ["ag2"] })).toEqual(["b"])
    expect(ids({ owners: ["You"] })).toEqual(["a"])
    expect(ids({ access: ["public"], owners: ["Priya"] })).toEqual(["b"])
    expect(ids({ access: ["public"], owners: ["You"] })).toEqual([])
  })
})

describe("public page rules", () => {
  it("refuses data write, read with events, and ask", () => {
    expect(refused("public", { data: "off", events: false, ask: false })).toEqual({ write: expect.any(String), read: null, events: null, ask: expect.any(String) })
    expect(refused("public", { data: "off", events: true, ask: false }).read).toEqual(expect.any(String))
    expect(refused("public", { data: "read", events: false, ask: false }).events).toEqual(expect.any(String))
    expect(refused("workspace", { data: "read", events: true, ask: true })).toEqual({ write: null, read: null, events: null, ask: null })
  })
})

const detail = (over: Partial<ArtifactDetail>): ArtifactDetail => ({
  ...row({}), people: [], editors: [], editors_can_share: true, data_scale: 1, collection_rules: {}, link: null, watches: [], asks: { threads: 0, turns_7d: 0 },
  usage: { documents: 0, max_documents: 5000, bytes: 0, max_bytes: 1 }, can: { edit: true, share: true, delete: true, transfer: true }, ...over,
})

describe("share dialog", () => {
  const open = (a: ArtifactDetail) => {
    const client = new QueryClient()
    client.setQueryData(["me"], { workspace: { name: "Fernhill Labs" } } as Me)
    render(<QueryClientProvider client={client}><TooltipProvider><ShareDialog artifact={a} open onOpenChange={() => {}} /></TooltipProvider></QueryClientProvider>)
    return screen.getByRole("radio", { name: /Anyone with the link/ })
  }

  it("allows going public with nothing the public cannot have", () => {
    expect(open(detail({ data: "read" }))).toHaveAttribute("aria-disabled", "false")
  })
  it.each([
    ["writes data", { data: "write" as const }],
    ["asks the agent", { ask: true }],
    ["reads data and sends events", { data: "read" as const, events: true }],
  ])("blocks going public when the page %s", (_, over) => {
    expect(open(detail(over))).toHaveAttribute("aria-disabled", "true")
  })
})

describe("share by email and rules", () => {
  const wrap = (ui: React.ReactNode) => {
    const client = new QueryClient()
    client.setQueryData(["me"], { workspace: { name: "Fernhill Labs" } } as Me)
    return render(<QueryClientProvider client={client}><MemoryRouter><TooltipProvider>{ui}</TooltipProvider></MemoryRouter></QueryClientProvider>)
  }

  it("invites a new address and shows its sign-in link once", async () => {
    const guest = { id: "p9", name: "ana", kind: "visitor" as const }
    const post = vi.spyOn(api, "post").mockResolvedValue({ ...detail({ access: "people", people: [guest] }), invite_link: "https://x.example/invite/t?next=%2Fa%2Fa1%2Fa1" } as never)
    wrap(<ShareDialog artifact={detail({ access: "people" })} open onOpenChange={() => {}} />)
    await userEvent.type(screen.getByRole("textbox", { name: "Email" }), "ana@example.com")
    await userEvent.click(screen.getByRole("button", { name: "Invite" }))
    expect(post).toHaveBeenCalledWith("/api/artifacts/a1/people", { email: "ana@example.com" })
    expect(await screen.findByText("Sign-in link for ana@example.com")).toBeInTheDocument()
  })
  it("lets only the owner or an admin turn editor sharing off", () => {
    wrap(<ShareDialog artifact={detail({ role: "editor" })} open onOpenChange={() => {}} />)
    expect(screen.getByRole("switch", { name: "Editors can share" })).toBeDisabled()
  })
  it("saves a collection's viewer rule", async () => {
    vi.spyOn(api, "get").mockResolvedValue([{ name: "votes", documents: 1, bytes: 9 }] as never)
    const patch = vi.spyOn(api, "patch").mockResolvedValue(detail({}) as never)
    wrap(<Data a={detail({ data: "write", collection_rules: { votes: { read: "own", write: "all" } } })} />)
    await userEvent.click(await screen.findByRole("radio", { name: "None" }))
    expect(patch).toHaveBeenCalledWith("/api/artifacts/a1", { collection_rules: { votes: { read: "own", write: "none" } } })
  })
})
