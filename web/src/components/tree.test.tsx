import { render, screen, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { TreeTable } from "./tree"
import { flatten, useTreeExpanded, type TreeRow } from "./tree-state"

type R = { name: string }
const r = (id: string, parentId: string | null, kind: "group" | "item"): TreeRow<R> => ({ id, parentId, kind, row: { name: id } })
const rows = [
  r("cat", null, "group"), r("clv", "cat", "item"), r("q3", "clv", "item"), r("priors", "clv", "item"), r("mmm", "cat", "item"),
  r("general", null, "item"), r("launch", "general", "item"),
]
const ids = (f: ReturnType<typeof flatten<R>>) => f.map((x) => `${x.depth}:${x.node.id}`)

describe("flatten", () => {
  it("nests children under parents, groups open and channels with threads closed", () => {
    expect(ids(flatten(rows, {}))).toEqual(["0:cat", "1:clv", "1:mmm", "0:general"])
  })

  it("opens what was toggled and counts children", () => {
    const f = flatten(rows, { clv: true, cat: false })
    expect(ids(f)).toEqual(["0:cat", "0:general"])
    expect(f[0]).toMatchObject({ children: 2, open: false })
    expect(ids(flatten(rows, { clv: true }))).toEqual(["0:cat", "1:clv", "2:q3", "2:priors", "1:mmm", "0:general"])
  })

  it("keeps matches and their ancestors, opened whatever was toggled", () => {
    const f = flatten(rows, { cat: false }, (x) => x.name === "q3")
    expect(ids(f)).toEqual(["0:cat", "1:clv", "2:q3"])
    expect(f.every((x) => x.open || x.children === 0)).toBe(true)
  })

  it("keeps a matching group whole", () => {
    expect(ids(flatten(rows, {}, (x) => x.name === "cat"))).toEqual(["0:cat", "1:clv", "2:q3", "2:priors", "1:mmm"])
  })

  it("roots a row whose parent is missing", () => {
    expect(ids(flatten([r("a", "gone", "item")], {}))).toEqual(["0:a"])
  })
})

function Harness({ onOpen, filter }: { onOpen: (n: string) => void; filter?: ((x: R) => boolean) | null }) {
  const [expanded, toggle] = useTreeExpanded("test")
  return (
    <TreeTable
      rows={rows} expanded={expanded} onToggle={toggle} filter={filter} onOpen={(x) => onOpen(x.name)}
      count={(_, n) => `${n} threads`}
      columns={[{ key: "name", header: "Name", render: (x) => x.name }, { key: "other", header: "Other", render: (x) => `${x.name}!` }]}
    />
  )
}

describe("TreeTable", () => {
  beforeEach(() => localStorage.clear())

  it("shows a count when collapsed, expands from the chevron without opening the row, and remembers it", async () => {
    const open = vi.fn()
    const { unmount } = render(<Harness onOpen={open} />)
    const clv = screen.getByRole("row", { name: /clv/ })
    expect(within(clv).getByText("2 threads")).toBeInTheDocument()
    await userEvent.click(within(clv).getByRole("button", { name: "Expand" }))
    expect(open).not.toHaveBeenCalled()
    expect(screen.getByText("q3")).toBeInTheDocument()
    expect(within(screen.getByRole("row", { name: /clv/ })).getByRole("button", { name: "Collapse" })).toHaveAttribute("aria-expanded", "true")
    expect(JSON.parse(localStorage.getItem("daimon:tree:test")!)).toEqual({ clv: true })
    unmount()
    render(<Harness onOpen={open} />)
    expect(screen.getByText("q3")).toBeInTheDocument()
  })

  it("only folds a group and opens an item", async () => {
    const open = vi.fn()
    render(<Harness onOpen={open} />)
    await userEvent.click(screen.getByRole("row", { name: /cat/ }))
    expect(screen.queryByText("mmm")).not.toBeInTheDocument()
    expect(open).not.toHaveBeenCalled()
    await userEvent.click(screen.getByRole("row", { name: /general/ }))
    expect(open).toHaveBeenCalledWith("general")
  })

  it("expands and collapses with the arrow keys", async () => {
    render(<Harness onOpen={vi.fn()} />)
    screen.getByRole("row", { name: /clv/ }).focus()
    await userEvent.keyboard("{ArrowRight}")
    expect(screen.getByText("priors")).toBeInTheDocument()
    await userEvent.keyboard("{ArrowLeft}")
    expect(screen.queryByText("priors")).not.toBeInTheDocument()
  })

  it("shows ancestors of a search match, expanded", () => {
    render(<Harness onOpen={vi.fn()} filter={(x) => x.name === "priors"} />)
    expect(screen.getAllByRole("row").slice(1).map((x) => x.getAttribute("data-depth"))).toEqual(["0", "1", "2"])
    expect(screen.queryByText("q3")).not.toBeInTheDocument()
  })
})
