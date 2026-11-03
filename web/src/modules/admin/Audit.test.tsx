import { screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { describe, expect, it, vi } from "vitest"

import { TooltipProvider } from "@/components/ui/tooltip"
import { api } from "@/lib/api"
import { renderAt } from "@/test/render"
import { Audit } from "./Audit"
import type { AuditEntry } from "./types"

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), page: vi.fn() },
}))

const event: AuditEntry = {
  id: "s1", source: "security", at: "2026-03-10T12:00:00Z", who: "Maya", actor_id: null, action: "Channel backend: clear pin", target: { kind: "other", id: null, name: "" }, detail: "Refused",
  facts: [["Tool", "Channel backend"], ["Reason", "needs_admin"]],
  channel: { id: "c9", name: null }, reference: "Message m1\nTurn t1",
}
const change: AuditEntry = { id: "a1", source: "web", at: "2026-03-10T11:00:00Z", who: "Jonas", actor_id: "p2", action: "Suspended", target: { kind: "person", id: "p1", name: "Mia" } }

function serve(rows: AuditEntry[]) {
  vi.mocked(api.page).mockImplementation(async (path: string) => ({ rows: path.includes("/people") ? [] : rows, next: null }))
  vi.mocked(api.get).mockImplementation(async (path: string) =>
    path === "/api/me" ? { capabilities: ["channels.view"], workspace: { name: "Workspace" } } : [{ source: "security", value: "channel_backend", label: "Channel backend" }])
}

describe("Audit", () => {
  it("asks for the filters in the address and nothing else", async () => {
    serve([event])
    renderAt("/audit?source=security&actor=p1&kind=channel_backend&from=2026-03-01&to=2026-03-31", "/audit", <TooltipProvider><Audit /></TooltipProvider>)
    await screen.findByText("Channel backend: clear pin")
    const path = vi.mocked(api.page).mock.calls.map(([p]) => p).find((p) => p.startsWith("/api/admin/audit"))!
    expect(new URL(path, "http://x").searchParams.toString()).toBe("source=security&actor=p1&kind=channel_backend&from=2026-03-01&to=2026-03-31")
  })

  it("lists both sources and shows the fields a security event stores", async () => {
    serve([event, change])
    renderAt("/audit", "/audit", <TooltipProvider><Audit /></TooltipProvider>)
    await userEvent.click(await screen.findByText("Channel backend: clear pin"))
    expect(await screen.findByText("needs_admin")).toBeInTheDocument()
    expect(screen.getByText("Chat and panels")).toBeInTheDocument()
    expect(await screen.findByRole("link", { name: "Unknown channel" })).toHaveAttribute("href", "/channels?channel=c9")
    expect(screen.queryByText(/Message m1|Turn t1/)).not.toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Copy reference" })).toBeInTheDocument()
    expect(screen.getByText("Suspended")).toBeInTheDocument()
    await waitFor(() => expect(vi.mocked(api.page).mock.calls.some(([p]) => p === "/api/admin/audit")).toBe(true))
  })
})
