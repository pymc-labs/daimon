import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render, screen, waitFor } from "@testing-library/react"
import { MemoryRouter, Route, Routes } from "react-router"
import { afterEach, describe, expect, it, vi } from "vitest"

import { Where } from "@/test/Where"
import { json, newClient } from "@/test/render"
import type { Me } from "@/lib/types"
import { WorkspaceLink } from "./WorkspaceLink"

const HERE = "6f1d0c4e-0000-4000-8000-000000000001"
const OTHER = "6f1d0c4e-0000-4000-8000-000000000002"
const STRANGER = "6f1d0c4e-0000-4000-8000-000000000003"

const ws = (id: string) => ({ id, name: id, platform: "discord", role: "member", members: 2 })
const me = { workspace: ws(HERE), workspaces: [ws(HERE), ws(OTHER)], capabilities: [], modules: [] } as unknown as Me

function open(path: string, switchTo: () => Response = () => json({ ok: true })) {
  const f = vi.fn(async (url: string, init?: RequestInit) => (url === "/api/workspaces/switch" && init?.method === "POST" ? switchTo() : json({}, 404)))
  vi.stubGlobal("fetch", f)
  const qc: QueryClient = newClient()
  qc.setQueryData(["me"], me)
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <Routes><Route path="/w/:workspace/*" element={<WorkspaceLink />} /><Route path="*" element={<Where />} /></Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
  return f
}
const where = () => screen.getByTestId("where")
afterEach(() => vi.unstubAllGlobals())

describe("workspace link", () => {
  it("opens the page at once when the workspace is the current one", async () => {
    const f = open(`/w/${HERE}/agents/ag_1?node=skills`)
    await waitFor(() => expect(where()).toHaveTextContent("/agents/ag_1?node=skills"))
    expect(f, "no switch is needed").not.toHaveBeenCalled()
  })

  it("switches to another of the person's workspaces, then opens the page", async () => {
    const f = open(`/w/${OTHER}/routines?routine=r1`)
    await waitFor(() => expect(where()).toHaveTextContent("/routines?routine=r1"))
    expect(f).toHaveBeenCalledWith("/api/workspaces/switch", expect.objectContaining({ method: "POST", body: JSON.stringify({ id: OTHER }) }))
  })

  it("sends a workspace the person is not in to the picker, keeping the page", async () => {
    const f = open(`/w/${STRANGER}/channels?channel=c1`)
    await waitFor(() => expect(where()).toHaveTextContent(`/workspaces?next=${encodeURIComponent("/channels?channel=c1")}`))
    expect(f, "a stranger's workspace is never asked for").not.toHaveBeenCalled()
  })

  it("sends a refused switch to the picker", async () => {
    open(`/w/${OTHER}/chat`, () => json({ detail: "Not found" }, 404))
    await waitFor(() => expect(where()).toHaveTextContent("/workspaces?next=%2Fchat"))
  })

  it("treats a malformed workspace id as not a member", async () => {
    const f = open("/w/not-a-uuid/chat")
    await waitFor(() => expect(where()).toHaveTextContent("/workspaces?next="))
    expect(f).not.toHaveBeenCalled()
  })

  it.each(["//evil.example.com/x", "/\\evil.example.com"])("never leaves the site through %s", async (rest) => {
    open(`/w/${HERE}${rest}`)
    await waitFor(() => expect(where()).toHaveTextContent("/chat"))
    expect(where().textContent, "no external target").not.toContain("evil")
  })
})
