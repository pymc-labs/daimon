import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { describe, expect, it, vi } from "vitest"

import { ApiError } from "@/lib/api"
import { QueryState } from "./QueryState"

const base = { data: undefined, error: null, isError: false, refetch: vi.fn() }

describe("QueryState", () => {
  it("shows a loading state before data", () => {
    render(<QueryState query={base}>{() => <p>rows</p>}</QueryState>)
    expect(screen.getByRole("status", { name: "Loading" })).toBeInTheDocument()
  })

  it("names the failure with the server's words and retries", async () => {
    const refetch = vi.fn()
    render(<QueryState query={{ ...base, isError: true, error: new ApiError(403, "Ask an admin or an owner."), refetch }}>{() => <p>rows</p>}</QueryState>)
    expect(screen.getByRole("alert")).toHaveTextContent("Couldn't load")
    expect(screen.getByText("Ask an admin or an owner.")).toBeInTheDocument()
    await userEvent.click(screen.getByRole("button", { name: "Retry" }))
    expect(refetch).toHaveBeenCalledOnce()
  })

  it("shows the empty text for an empty list, and rows otherwise", () => {
    const { rerender } = render(<QueryState query={{ ...base, data: [] as string[] }} empty="No people">{(d) => <p>{d.length} rows</p>}</QueryState>)
    expect(screen.getByText("No people")).toBeInTheDocument()
    rerender(<QueryState query={{ ...base, data: ["a", "b"] }} empty="No people">{(d) => <p>{d.length} rows</p>}</QueryState>)
    expect(screen.getByText("2 rows")).toBeInTheDocument()
  })

  it("keeps showing data while a refetch fails", () => {
    render(<QueryState query={{ ...base, data: ["a"], isError: true, error: new Error("x") }}>{(d) => <p>{d.length} rows</p>}</QueryState>)
    expect(screen.getByText("1 rows")).toBeInTheDocument()
  })
})
