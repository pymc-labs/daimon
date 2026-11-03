import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { MemoryRouter } from "react-router"
import { describe, expect, it, vi } from "vitest"

import { InheritedSegmented } from "./inherited"
import { AgentLink, ChannelLink, EnvLink, SkillLink } from "./links"
import { MultiPicker } from "./picker"

const withMe = (caps: string[], ui: React.ReactNode) => {
  const qc = new QueryClient()
  qc.setQueryData(["me"], { capabilities: caps })
  return render(<QueryClientProvider client={qc}><MemoryRouter>{ui}</MemoryRouter></QueryClientProvider>)
}

globalThis.ResizeObserver ??= class { observe() {} unobserve() {} disconnect() {} } as unknown as typeof ResizeObserver
Element.prototype.scrollIntoView ??= () => {}

describe("InheritedSegmented", () => {
  it("shows the applied default in the control and marks an own value", async () => {
    const onChange = vi.fn()
    const { rerender } = render(<InheritedSegmented label="Joins threads" value={null} inherited={{ value: true, from: "Workspace" }} onChange={onChange} />)
    expect(screen.getByRole("radio", { name: "Workspace default (On)" })).toBeChecked()
    expect(screen.queryByRole("img", { name: "Set here" })).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole("radio", { name: "Off" }))
    expect(onChange).toHaveBeenCalledWith(false)
    rerender(<InheritedSegmented label="Joins threads" value={false} inherited={{ value: true, from: "Workspace" }} onChange={onChange} />)
    expect(screen.getByRole("img", { name: "Set here" })).toBeInTheDocument()
    await userEvent.click(screen.getByRole("radio", { name: "Workspace default (On)" }))
    expect(onChange).toHaveBeenLastCalledWith(null)
  })
})

describe("MultiPicker", () => {
  it("lists chips, labels a missing id plainly and toggles from the list", async () => {
    const onAdd = vi.fn(), onRemove = vi.fn()
    render(<MultiPicker items={[{ id: "a", label: "Alpha" }, { id: "b", label: "Beta" }]} value={["a", "x"]} onAdd={onAdd} onRemove={onRemove} unknown="Deleted agent" />)
    expect(screen.getByText("Alpha")).toBeInTheDocument()
    expect(screen.getByText("Deleted agent")).toBeInTheDocument()
    expect(screen.queryByText("x")).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole("button", { name: "Remove Alpha" }))
    expect(onRemove).toHaveBeenCalledWith("a")
    await userEvent.click(screen.getByRole("button", { name: "Add" }))
    await userEvent.click(await screen.findByRole("option", { name: "Beta" }))
    expect(onAdd).toHaveBeenCalledWith("b")
  })
})

describe("entity links", () => {
  it("links when the viewer may open it and the target has a name", () => {
    withMe(["agents.view", "channels.view"], <><AgentLink id="ag_1" name="CLV Analyst" node="skill:s1" /><ChannelLink id="c1" name="Launch" kind="thread" /></>)
    expect(screen.getByRole("link", { name: /CLV Analyst/ })).toHaveAttribute("href", "/agents/ag_1?node=skill%3As1")
    expect(screen.getByRole("link", { name: "Launch" })).toHaveAttribute("href", "/channels?channel=c1")
  })
  it("opens skills and sandboxes only for editors, as their endpoints require", () => {
    const ui = <><SkillLink id="s1" name="Report" /><EnvLink id="e1" name="Sandbox A" /></>
    const { unmount } = withMe(["agents.view"], ui)
    expect(screen.queryByRole("link")).not.toBeInTheDocument()
    unmount()
    withMe(["agents.edit"], ui)
    expect(screen.getAllByRole("link")).toHaveLength(2)
  })
  it("reads plain without the capability, and never shows an id for a missing name", () => {
    withMe([], <><AgentLink id="ag_1" name="CLV Analyst" /><AgentLink id="ag_9" name={null} /><ChannelLink id="c9" name={null} /></>)
    expect(screen.queryByRole("link")).not.toBeInTheDocument()
    expect(screen.getByText("Deleted agent")).toBeInTheDocument()
    expect(screen.getByText("Deleted channel")).toBeInTheDocument()
    expect(screen.queryByText(/ag_9|c9/)).not.toBeInTheDocument()
  })
})
