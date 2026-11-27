import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, describe, expect, it, vi } from "vitest"

import { json } from "@/test/render"
import { RevokeAsks, Thumb } from "./Viewing"

afterEach(() => vi.unstubAllGlobals())

describe("thumbnails", () => {
  it("shows the version's thumbnail, and the page icon until one exists", () => {
    const { container } = render(<Thumb id="a1" version={3} />)
    const img = container.querySelector("img")!
    expect(img).toHaveAttribute("src", "/api/artifacts/a1/versions/3/thumbnail")
    fireEvent.error(img)
    expect(container.querySelector("img")).toBeNull()
    expect(container.querySelector("svg")).not.toBeNull()
  })
})

describe("ask consent", () => {
  it("revokes a page's asks from the ask thread", async () => {
    const f = vi.fn(async (_: string, init?: RequestInit) => init?.method === "DELETE" ? new Response(null, { status: 204 }) : json({ allowed: true }))
    vi.stubGlobal("fetch", f)
    render(<QueryClientProvider client={new QueryClient()}><RevokeAsks id="a1" /></QueryClientProvider>)
    await userEvent.click(await screen.findByRole("button", { name: "Revoke asks" }))
    await waitFor(() => expect(screen.queryByRole("button", { name: "Revoke asks" })).toBeNull())
    expect(f).toHaveBeenCalledWith("/api/artifacts/a1/consent", expect.objectContaining({ method: "DELETE" }))
  })
})
