import { screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, describe, expect, it, vi } from "vitest"

import { json, renderAt } from "@/test/render"
import { InviteLanding } from "./Invite"
import { landingOf } from "./landing"

const invite = (over: object) => ({
  workspace: "Fernhill Labs", from: "Maya", email: "priya@fernhill.example", kind: "invite",
  agents: [], credit_usd: 0, expires_at: "2030-01-01T00:00:00Z", ...over,
})

/** Answers by method and path; unlisted paths are a 401. */
function server(routes: Record<string, () => Response>) {
  const f = vi.fn(async (path: string, init?: RequestInit) => (routes[`${init?.method ?? "GET"} ${path}`] ?? (() => json({ detail: "Sign in" }, 401)))())
  vi.stubGlobal("fetch", f)
  return f
}
afterEach(() => vi.unstubAllGlobals())

const open = (token: string) => renderAt(`/invite/${token}`, "/invite/:token", <InviteLanding />)

describe("invite landing", () => {
  it("lets a member accept an invite that names no agents, and lands on chat", async () => {
    const f = server({
      "GET /api/invites/t1": () => json(invite({})),
      "POST /api/invites/t1/accept": () => json({ ok: true }),
    })
    open("t1")
    expect(await screen.findByRole("heading", { name: "Join Fernhill Labs" })).toBeInTheDocument()
    await userEvent.click(screen.getByRole("button", { name: "Accept and start" }))
    await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent("/chat"))
    expect(screen.getByTestId("where")).not.toHaveTextContent("agent=")
    expect(f).toHaveBeenCalledWith("/api/invites/t1/accept", expect.objectContaining({ method: "POST" }))
  })

  it("opens the first named agent after accepting", async () => {
    server({
      "GET /api/invites/t2": () => json(invite({ agents: [{ id: "ag_1", name: "Pricing Analyst", avatar: "", model: "", description: "" }], credit_usd: 5 })),
      "POST /api/invites/t2/accept": () => json({ ok: true }),
    })
    open("t2")
    await userEvent.click(await screen.findByRole("button", { name: "Accept and start" }))
    await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent("/chat?agent=ag_1"))
    expect(landingOf({ agents: [] })).toBe("/chat")
  })

  it("shows the server's refusal when accepting fails", async () => {
    server({
      "GET /api/invites/t3": () => json(invite({})),
      "POST /api/invites/t3/accept": () => json({ detail: "This link was already used." }, 410),
    })
    open("t3")
    await userEvent.click(await screen.findByRole("button", { name: "Accept and start" }))
    expect(await screen.findByRole("alert")).toHaveTextContent("This link was already used.")
  })

  it("asks an owner link's reader to sign in first", async () => {
    server({ "GET /api/invites/t4": () => json(invite({ kind: "owner", email: "" })) })
    open("t4")
    const link = await screen.findByRole("link", { name: "Sign in to accept" })
    expect(link).toHaveAttribute("href", "/signin?next=%2Finvite%2Ft4")
  })

  it("says an unknown link expired", async () => {
    server({ "GET /api/invites/gone": () => json({ detail: "Not found" }, 404) })
    open("gone")
    expect(await screen.findByRole("heading", { name: "Invite expired" })).toBeInTheDocument()
  })
})
