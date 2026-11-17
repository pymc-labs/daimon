import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { MemoryRouter } from "react-router"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { api, ApiError } from "@/lib/api"
import { Ctx } from "@/lib/confirm"
import { Limits } from "./Limits"
import { Menus } from "./Menus"
import { Destination } from "./Support"
import { useSupportPage } from "./api"
import type { Limit, SupportPage } from "./types"

Object.assign(Element.prototype, { hasPointerCapture: () => false, releasePointerCapture: () => {}, scrollIntoView: () => {} })

vi.mock("@/lib/api", async (orig) => ({ ...(await orig<typeof import("@/lib/api")>()), api: { get: vi.fn(), put: vi.fn() } }))

const limit: Limit = {
  scope: "channel", scope_id: "c1", name: "general", person_id: null, person_name: null,
  shared: "5", each_person: "2", window: "monthly", starts_at: null, ends_at: null,
  used: "1", state: "active", revision: 3, recent: [],
}
const menu = { key: "support", name: "Support", revision: 3, summary: "", options: 1, used_in: [], body: { name: "Support", options: [{ key: "look", label: "Look", price: { kind: "fixed", amount: "1" } }] } }
const data: SupportPage = {
  platform: "discord", everyone: { each_person: "20" }, undelivered: [],
  limits: [limit], menus: [menu], menu_use: [{ scope: "workspace", scope_id: "", name: "Workspace", menu: null }],
  places: [], agents: [{ id: "daimon", name: "Daimon" }],
  channel: { own_allowed: true, value: null, default: { name: "support" }, state: "ready", detail: null, open: { count: 0, where: null }, revision: 2 },
}

let current = data
const found = [{ id: "1100000000000000013", name: "Marcus Webb", handle: "marcus_w" }]
const Page = () => {
  const { data: d } = useSupportPage()
  return d ? <><Limits d={d} /><Menus d={d} /><Destination d={d} /></> : null
}
const view = (start: SupportPage = data, url = "/") => {
  current = start
  vi.mocked(api.get).mockImplementation(async (path: string) => path.startsWith("/api/support/people") ? (decodeURIComponent(path).endsWith("=mar") ? found : []) as never : (path === "/api/channels" ? { platform: "discord", channels: [{ id: "c1", name: "general", kind: "channel", platform: "discord" }, { id: "c2", name: "help", kind: "channel", platform: "discord" }] } : current) as never)
  vi.mocked(api.put).mockResolvedValue(data as never)
  render(
    <QueryClientProvider client={new QueryClient()}>
      <Ctx.Provider value={async () => "action"}><MemoryRouter initialEntries={[url]}><Page /></MemoryRouter></Ctx.Provider>
    </QueryClientProvider>,
  )
}
const stale = () => vi.mocked(api.put).mockRejectedValueOnce(new ApiError(409, "stale"))
const drawer = async (url: string, start = data) => { view(start, url); return within(await screen.findByRole("dialog")) }
const sentLimit = () => (vi.mocked(api.put).mock.calls.at(-1)![1] as { limit: { ends_at: string | null } }).limit.ends_at

beforeEach(() => {
  vi.clearAllMocks()
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} })
})

describe("limits", () => {
  it("names the last included day, one before the exclusive end", async () => {
    view({ ...data, limits: [{ ...limit, ends_at: "2030-03-11T00:00:00Z" }] })
    expect(await screen.findByRole("columnheader", { name: "Last day" })).toBeInTheDocument()
    expect(screen.getByRole("cell", { name: "10 Mar 2030" })).toBeInTheDocument()
  })

  describe("adding a person's limit", () => {
    const open = async () => {
      view()
      await userEvent.click(await screen.findByRole("button", { name: "Add limit" }))
      const dialog = within(screen.getByRole("dialog"))
      await userEvent.click(dialog.getByRole("radio", { name: "Person" }))
      await userEvent.click(dialog.getByRole("button", { name: "Choose" }))
      return dialog
    }
    const submit = async (dialog: ReturnType<typeof within>) => {
      await userEvent.type(dialog.getByLabelText("Credits"), "3")
      await userEvent.click(dialog.getByRole("button", { name: "Add limit" }))
      return vi.mocked(api.put).mock.calls.at(-1)![1] as { person_id: string }
    }

    it("finds a chat-only person by name and sends their platform id", async () => {
      const dialog = await open()
      await userEvent.type(screen.getByPlaceholderText("Find a person"), "mar")
      expect(await screen.findByText("marcus_w")).toBeInTheDocument()
      await userEvent.click(screen.getByRole("option", { name: /Marcus Webb/ }))
      expect(await dialog.findByRole("button", { name: "Marcus Webb" })).toBeInTheDocument()
      expect((await submit(dialog)).person_id).toBe("1100000000000000013")
    })

    it("offers a typed mention as an id to use", async () => {
      const dialog = await open()
      await userEvent.type(screen.getByPlaceholderText("Find a person"), "<@1100000000000000099>")
      await userEvent.click(await screen.findByRole("option", { name: "Use id 1100000000000000099" }))
      expect((await submit(dialog)).person_id).toBe("1100000000000000099")
    })

    it("offers no id for text that is not one", async () => {
      await open()
      await userEvent.type(screen.getByPlaceholderText("Find a person"), "nobody")
      await waitFor(() => expect(api.get).toHaveBeenCalledWith("/api/support/people?q=nobody"))
      expect(screen.queryByRole("option")).not.toBeInTheDocument()
    })
  })

  it("lists the built-in row first, locked", async () => {
    view()
    const rows = await screen.findAllByRole("row")
    expect(rows[1]).toHaveTextContent("Everyone")
    expect(within(rows[1]!).getByRole("img", { name: "Locked" })).toBeInTheDocument()
    expect(rows[2]).toHaveTextContent("general")
  })

  it("adds a workspace limit as a new row", async () => {
    view()
    await userEvent.click(await screen.findByRole("button", { name: "Add limit" }))
    const dialog = within(screen.getByRole("dialog"))
    await userEvent.click(dialog.getByRole("radio", { name: "Workspace" }))
    await userEvent.type(dialog.getByLabelText("Shared"), "10")
    await userEvent.click(dialog.getByRole("button", { name: "Add limit" }))
    expect(api.put).toHaveBeenCalledWith("/api/support/limits", {
      scope: "workspace", scope_id: "", person_id: null, expected_revision: null,
      limit: { shared: "10", each_person: null, window: "monthly", starts_at: null, ends_at: null },
    })
  })

  it("sends the revision as read and removes a row with a null limit", async () => {
    const d = await drawer("/?limit=channel%7Cc1%7C")
    await userEvent.click(d.getByRole("button", { name: "Remove" }))
    await waitFor(() => expect(api.put).toHaveBeenCalledWith("/api/support/limits", { scope: "channel", scope_id: "c1", person_id: null, limit: null, expected_revision: 3 }))
  })

  it("reloads the form after a 409, so a second save cannot overwrite the other change", async () => {
    const d = await drawer("/?limit=channel%7Cc1%7C")
    stale()
    current = { ...data, limits: [{ ...limit, each_person: "4", shared: "9", revision: 4 }] }
    await userEvent.clear(d.getByLabelText("Each person"))
    await userEvent.type(d.getByLabelText("Each person"), "3")
    await userEvent.click(d.getByRole("button", { name: "Save" }))
    expect(await d.findByRole("alert")).toHaveTextContent("Changed meanwhile. Reloaded.")
    expect(d.getByLabelText("Each person")).toHaveValue("4")
    expect(d.getByLabelText("Shared")).toHaveValue("9")
    await userEvent.type(d.getByLabelText("Each person"), "5")
    await userEvent.click(d.getByRole("button", { name: "Save" }))
    expect(api.put).toHaveBeenLastCalledWith("/api/support/limits", expect.objectContaining({ expected_revision: 4, limit: expect.objectContaining({ each_person: "45", shared: "9" }) }))
  })

  it("shows a server refusal next to the input it names", async () => {
    const d = await drawer("/?limit=channel%7Cc1%7C")
    vi.mocked(api.put).mockRejectedValueOnce(new ApiError(422, "invalid", [{ loc: ["limit", "each_person"], msg: "must be more than 0" }]))
    await userEvent.type(d.getByLabelText("Each person"), "1")
    await userEvent.click(d.getByRole("button", { name: "Save" }))
    expect(await d.findByText("must be more than 0")).toBeInTheDocument()
    expect(d.getByLabelText("Each person")).toBeInvalid()
  })

  describe("last day", () => {
    const ending = (ends_at: string) => ({ ...data, limits: [{ ...limit, starts_at: "2026-11-01T00:00:00Z", ends_at }] })
    const edit = async (ends_at: string) => (await drawer("/?limit=channel%7Cc1%7C", ending(ends_at))).getByLabelText("Last day")

    it("sends the day after the one picked", async () => {
      const input = await edit("2026-12-21T00:00:00Z")
      fireEvent.change(input, { target: { value: "2026-12-17" } })
      await userEvent.click(screen.getByRole("button", { name: "Save" }))
      expect(sentLimit()).toBe("2026-12-18")
    })

    it("shows an end at midnight as the day before", async () => {
      expect(await edit("2026-12-18T00:00:00Z")).toHaveValue("2026-12-17")
    })

    it("keeps an end set mid-day as it was when untouched", async () => {
      const input = await edit("2026-11-24T09:00:00Z")
      expect(input).toHaveValue("2026-11-24")
      await userEvent.type(screen.getByLabelText("Each person"), "1")
      await userEvent.click(screen.getByRole("button", { name: "Save" }))
      expect(sentLimit()).toBe("2026-11-24T09:00:00Z")
    })
  })
})

describe("menus", () => {
  it("sends the menu with the revision read and a key made from the name", async () => {
    view()
    await userEvent.click(await screen.findByRole("button", { name: "Add menu" }))
    const d = within(await screen.findByRole("dialog"))
    await userEvent.type(d.getByLabelText("Menu name"), "Quick Help")
    await userEvent.type(d.getByLabelText("Label of option 1"), "Review this thread")
    await userEvent.clear(d.getByLabelText("Credits of option 1"))
    await userEvent.type(d.getByLabelText("Credits of option 1"), "0.5")
    await userEvent.click(d.getByRole("button", { name: "Save" }))
    expect(api.put).toHaveBeenCalledWith("/api/support/menus/quick-help", {
      expected_revision: null,
      menu: { name: "Quick Help", options: [{ key: "review_this_thread", label: "Review this thread", price: { kind: "fixed", amount: "0.5" } }] },
    })
  })

  it("expects the revision of the menu as opened", async () => {
    const d = await drawer("/?menu=support")
    await userEvent.type(d.getByLabelText("Menu name"), "!")
    await userEvent.click(d.getByRole("button", { name: "Save" }))
    expect(api.put).toHaveBeenCalledWith("/api/support/menus/support", expect.objectContaining({ expected_revision: 3 }))
  })

  it("reloads the menu form after a 409 and expects the new revision", async () => {
    const d = await drawer("/?menu=support")
    stale()
    const theirs = { name: "Their menu", options: [{ key: "call", label: "Call", price: { kind: "fixed", amount: "2" } }] }
    current = { ...data, menus: [{ ...menu, revision: 4, body: theirs }] }
    await userEvent.click(d.getByRole("button", { name: "Save" }))
    expect(await d.findByRole("alert")).toHaveTextContent("Changed meanwhile. Reloaded.")
    expect(d.getByLabelText("Menu name")).toHaveValue("Their menu")
    await userEvent.click(d.getByRole("button", { name: "Save" }))
    expect(api.put).toHaveBeenLastCalledWith("/api/support/menus/support", { menu: expect.objectContaining({ name: "Their menu" }), expected_revision: 4 })
  })

  it("shows a server refusal next to the input it names", async () => {
    const d = await drawer("/?menu=support")
    vi.mocked(api.put).mockRejectedValueOnce(new ApiError(422, "invalid", [{ loc: ["menu", "options", 0, "label"], msg: "String should have at least 1 character" }]))
    await userEvent.click(d.getByRole("button", { name: "Save" }))
    expect(await d.findByText("String should have at least 1 character")).toBeInTheDocument()
  })

  it("cannot remove a menu that is in use", async () => {
    const used = { ...menu, used_in: [{ scope: "channel" as const, scope_id: "c1", name: "general" }] }
    const d = await drawer("/?menu=support", { ...data, menus: [used] })
    expect(d.getByRole("button", { name: "Remove" })).toBeDisabled()
  })
})

describe("requests go to", () => {
  it("asks before moving open requests, then sends the revision read", async () => {
    view({ ...data, channel: { ...data.channel, open: { count: 3, where: "support" } } })
    await userEvent.click(await screen.findByRole("combobox", { name: "Channel" }))
    await userEvent.click(await screen.findByRole("option", { name: /help/ }))
    await waitFor(() => expect(api.put).toHaveBeenCalledWith("/api/support/channel", { channel_id: "c2", expected_revision: 2 }))
  })

  it("lists requests no channel received only when there are some", async () => {
    view({ ...data, undelivered: [{ at: "2026-01-01T00:00:00Z", person: "Priya", credits: "1", what: "Look" }] })
    expect(await screen.findByText("Not delivered")).toBeInTheDocument()
  })

  it("has no undelivered row by default", async () => {
    view(data)
    await screen.findByText("Open requests")
    expect(screen.queryByText("Not delivered")).not.toBeInTheDocument()
  })

  it("shows only the number of open requests", async () => {
    view({ ...data, channel: { ...data.channel, open: { count: 3, where: "support" } } })
    expect(await screen.findByText("3")).toBeInTheDocument()
    expect(screen.queryByText(/in support/)).not.toBeInTheDocument()
  })

  it("says so when the channel changed meanwhile", async () => {
    view()
    stale()
    await userEvent.click(await screen.findByRole("combobox", { name: "Channel" }))
    await userEvent.click(await screen.findByRole("option", { name: /help/ }))
    expect(await screen.findByText("Changed meanwhile. Reloaded.")).toBeInTheDocument()
  })

  it("shows a lock when the deployment sets the channel", async () => {
    view({ ...data, channel: { ...data.channel, own_allowed: false } })
    expect(await screen.findByText(/Locked by the deployment/)).toBeInTheDocument()
    expect(screen.queryByRole("combobox", { name: "Channel" })).not.toBeInTheDocument()
  })
})
