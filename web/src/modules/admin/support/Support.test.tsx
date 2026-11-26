import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { MemoryRouter } from "react-router"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { api, ApiError } from "@/lib/api"
import { Ctx } from "@/lib/confirm"
import { Limits } from "./Limits"
import { Menus } from "./Menus"
import { Destination, Support } from "./Support"
import { ChannelSupport } from "./ChannelSupport"
import { UndeliveredDrawer } from "./Undelivered"
import { useSupportPage } from "./api"
import type { FeatureSchema, FeatureView } from "@/modules/features/types"
import type { Limit, SupportPage } from "./types"

Object.assign(Element.prototype, { hasPointerCapture: () => false, releasePointerCapture: () => {}, scrollIntoView: () => {} })

vi.mock("@/lib/api", async (orig) => ({ ...(await orig<typeof import("@/lib/api")>()), api: { get: vi.fn(), put: vi.fn(), post: vi.fn() } }))

let features: FeatureView[] = []
const ok = <T,>(d: T) => ({ data: d, error: null, isError: false, refetch: () => {} })
const schema = (key: string, name: string): FeatureSchema => ({
  key, name, category: "chat", categoryName: "Chat", default: "on", tiers: ["deployment", "workspace", "agent", "channel", "thread"],
  lockable: ["workspace", "channel"], platforms: ["discord"], threadAnyone: false, native: false, doc: "",
  values: [{ value: "on", label: "On" }, { value: "off", label: "Off" }],
})
vi.mock("@/modules/features/api", () => ({
  useFeatureSchema: () => ok([schema("support_escalation", "Ask a person"), schema("support_feedback", "Thumbs-down feedback")]),
  useFeatures: () => ok({ platform: "discord", features }),
  useFeatureSave: () => ({ pending: false, run: vi.fn() }),
}))
vi.mock("@/lib/hooks", async (orig) => ({ ...(await orig<typeof import("@/lib/hooks")>()), useCan: () => () => true }))
const feature = (key: string, workspace: string, over: Partial<FeatureView> = {}): FeatureView => ({
  key, tiers: [{ tier: "workspace", value: workspace, locked: false, text: workspace }, { tier: "channel", value: "default", locked: false, text: "default" }],
  value: workspace === "on" ? "on" : "off", source: "workspace", locked: false, unavailable: null, summary: "", ...over,
})

const limit: Limit = {
  scope: "channel", scope_id: "c1", name: "general", person_id: null, person_name: null,
  shared: "5", each_person: "2", window: "monthly", starts_at: null, ends_at: null,
  used: "1", state: "active", revision: 3, recent: [],
}
const lost = { id: "r1", at: "2026-01-01T00:00:00Z", person: "Priya", credits: "2", what: "Look", charged: false, price: { kind: "range" as const, min: "1", max: "3" } }
const menu = { key: "support", name: "Support", revision: 3, summary: "", options: 1, used_in: [], channel: null, body: { name: "Support", options: [{ key: "look", label: "Look", price: { kind: "fixed", amount: "1" } }] } }
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
const confirmed = vi.fn(async (..._args: unknown[]) => "action")
const mount = (Body: () => React.ReactNode, url = "/") =>
  render(
    <QueryClientProvider client={new QueryClient()}>
      <Ctx.Provider value={confirmed as never}><MemoryRouter initialEntries={[url]}><Body /></MemoryRouter></Ctx.Provider>
    </QueryClientProvider>,
  )
const view = (start: SupportPage = data, url = "/", Body: () => React.ReactNode = Page) => {
  current = start
  vi.mocked(api.get).mockImplementation(async (path: string) => path.startsWith("/api/support/people") ? (decodeURIComponent(path).endsWith("=mar") ? found : []) as never : (path === "/api/channels" ? { platform: "discord", channels: [{ id: "c1", name: "general", kind: "channel", platform: "discord" }, { id: "c2", name: "help", kind: "channel", platform: "discord" }] } : current) as never)
  vi.mocked(api.put).mockResolvedValue(data as never)
  vi.mocked(api.post).mockResolvedValue(data as never)
  mount(Body, url)
}
const stale = () => vi.mocked(api.put).mockRejectedValueOnce(new ApiError(409, "stale"))
const drawer = async (url: string, start = data) => { view(start, url); return within(await screen.findByRole("dialog")) }
const sentLimit = () => (vi.mocked(api.put).mock.calls.at(-1)![1] as { limit: { ends_at: string | null } }).limit.ends_at

beforeEach(() => {
  vi.clearAllMocks()
  features = [feature("support_escalation", "on"), feature("support_feedback", "on")]
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

  it("sends where a menu's requests go", async () => {
    const d = await drawer("/?menu=support")
    await userEvent.click(d.getByRole("combobox", { name: "Requests go to" }))
    await userEvent.click(await screen.findByRole("option", { name: /help/ }))
    await userEvent.click(d.getByRole("button", { name: "Save" }))
    expect(api.put).toHaveBeenCalledWith("/api/support/menus/support", expect.objectContaining({ menu: expect.objectContaining({ channel: { id: "c2" } }) }))
  })

  it("drops a menu's channel when the deployment sets where requests go", async () => {
    const own = { ...menu, channel: { id: "c2", name: "help" }, body: { ...menu.body, channel: { id: "c2" } } }
    const d = await drawer("/?menu=support", { ...data, menus: [own], channel: { ...data.channel, own_allowed: false } })
    await userEvent.click(d.getByRole("button", { name: "Save" }))
    expect(api.put).toHaveBeenCalledWith("/api/support/menus/support", expect.objectContaining({ menu: expect.not.objectContaining({ channel: expect.anything() }) }))
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

  it("says where a menu in use is used instead of removing it", async () => {
    const used = { ...menu, used_in: [{ scope: "channel" as const, scope_id: "c1", name: "general" }, { scope: "workspace" as const, scope_id: "", name: "Workspace" }] }
    const d = await drawer("/?menu=support", { ...data, menus: [used] })
    expect(d.getByRole("button", { name: "Remove" })).toBeEnabled()
    await userEvent.click(d.getByRole("button", { name: "Remove" }))
    expect(await d.findByText("Used in general, Workspace. Pick another menu there first.")).toBeInTheDocument()
    expect(api.put).not.toHaveBeenCalled()
  })

  it("disables a steps price once the option has four fields", async () => {
    const full = { ...menu, body: { name: "Support", options: [{ key: "look", label: "Look", price: { kind: "fixed", amount: "1" }, fields: ["a", "b", "c", "d"].map((k) => ({ key: k, label: k, kind: "text" })) }] } }
    const d = await drawer("/?menu=support", { ...data, menus: [full] })
    if (!d.queryByRole("combobox", { name: "Price of option 1" })) await userEvent.click(d.getByRole("button", { name: "Open option 1" }))
    await userEvent.click(await d.findByRole("combobox", { name: "Price of option 1" }))
    expect(await screen.findByRole("option", { name: "Pick a price" })).toHaveAttribute("aria-disabled", "true")
  })
})

describe("requests go to", () => {
  it("asks before moving open requests, then sends the revision read", async () => {
    view({ ...data, channel: { ...data.channel, open: { count: 3, where: ["support"] } } })
    await userEvent.click(await screen.findByRole("combobox", { name: "Channel" }))
    await userEvent.click(await screen.findByRole("option", { name: /help/ }))
    await waitFor(() => expect(api.put).toHaveBeenCalledWith("/api/support/channel", { channel_id: "c2", expected_revision: 2 }))
  })

  it.each([
    [null, "3 open requests stay. New ones go to the new channel."],
    [["support"], "3 open requests stay in support. New ones go to the new channel."],
    [["support", "help", "support"], "3 open requests stay in support, help. New ones go to the new channel."],
  ])("names where open requests stay, never null: %j", async (where, body) => {
    const ask = vi.fn(async () => "action")
    current = { ...data, channel: { ...data.channel, open: { count: 3, where } } }
    vi.mocked(api.get).mockImplementation(async (path: string) => (path === "/api/channels" ? { platform: "discord", channels: [{ id: "c2", name: "help", kind: "channel", platform: "discord" }] } : current) as never)
    vi.mocked(api.put).mockResolvedValue(data as never)
    render(<QueryClientProvider client={new QueryClient()}><Ctx.Provider value={ask as never}><MemoryRouter><Page /></MemoryRouter></Ctx.Provider></QueryClientProvider>)
    await userEvent.click(await screen.findByRole("combobox", { name: "Channel" }))
    await userEvent.click(await screen.findByRole("option", { name: /help/ }))
    expect(ask).toHaveBeenCalledWith(expect.objectContaining({ body }))
  })

  it.each([
    ["blocked", "Can't post", "Give Daimon Send Messages and Embed Links in #support."],
    ["unverified", "Unverified", "Couldn't check. Try again."],
  ] as const)("says why a %s channel is not ready", async (state, label, detail) => {
    view({ ...data, channel: { ...data.channel, state, detail } })
    expect(await screen.findByText(label)).toBeInTheDocument()
    expect(screen.getByText(detail)).toBeInTheDocument()
    expect(screen.queryByText("Ready")).not.toBeInTheDocument()
  })

  it("shows a refused pick's fix under the section", async () => {
    view(data)
    vi.mocked(api.put).mockRejectedValueOnce(new ApiError(422, "invalid", [{ loc: ["channel_id"], msg: "Give Daimon Send Messages in #help." }]))
    await userEvent.click(await screen.findByRole("combobox", { name: "Channel" }))
    await userEvent.click(await screen.findByRole("option", { name: /help/ }))
    expect(await screen.findByRole("alert")).toHaveTextContent("Give Daimon Send Messages in #help.")
  })

  it("lists requests no channel received only when there are some", async () => {
    view({ ...data, undelivered: [lost] })
    expect(await screen.findByText("Not delivered")).toBeInTheDocument()
  })

  it("has no undelivered row by default", async () => {
    view(data)
    await screen.findByText("Open requests")
    expect(screen.queryByText("Not delivered")).not.toBeInTheDocument()
  })

  it("shows only the number of open requests", async () => {
    view({ ...data, channel: { ...data.channel, open: { count: 3, where: ["support"] } } })
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
    expect(await screen.findByText("Set by whoever runs Daimon")).toBeInTheDocument()
    expect(screen.getByText("Built-in default (support)")).toBeInTheDocument()
    expect(screen.queryByRole("combobox", { name: "Channel" })).not.toBeInTheDocument()
  })

  it("puts the feedback switch under requests go to, whatever Ask a person is set to", async () => {
    features = [feature("support_escalation", "off"), feature("support_feedback", "on")]
    view(data, "/", Support)
    expect(await screen.findByText("Send thumbs-down feedback")).toBeInTheDocument()
    expect(screen.getByRole("combobox", { name: "Thumbs-down feedback" })).toBeInTheDocument()
    expect(screen.queryByText("Where it shows")).not.toBeInTheDocument()
  })
})

describe("the steps in order", () => {
  it("shows steps 2 to 5 when asked on but a requirement is unmet, with the reason at the step that fixes it", async () => {
    features = [feature("support_escalation", "on", { value: "off", unavailable: "Pick a support channel first." }), feature("support_feedback", "on")]
    view({ ...data, channel: { ...data.channel, state: "ready" } }, "/", Support)
    expect(await screen.findByText("Where it shows")).toBeInTheDocument()
    expect(screen.getByText("Credit limits")).toBeInTheDocument()
    expect(screen.getByText("Menus")).toBeInTheDocument()
    expect(screen.getAllByText("Pick a support channel first.")).toHaveLength(2)
  })

  it("hides steps 2, 4 and 5 while Ask a person is off", async () => {
    features = [feature("support_escalation", "off"), feature("support_feedback", "on")]
    view(data, "/", Support)
    await screen.findByText("Requests go to")
    expect(screen.queryByText("Where it shows")).not.toBeInTheDocument()
    expect(screen.queryByText("Credit limits")).not.toBeInTheDocument()
    expect(screen.queryByText("Menus")).not.toBeInTheDocument()
  })

  it("opens the existing limit when a person already has one in that place", async () => {
    const mine = { ...limit, scope: "workspace" as const, scope_id: "", name: "Workspace", person_id: "1100000000000000013", person_name: "Marcus Webb" }
    view({ ...data, limits: [limit, mine] })
    await userEvent.click(await screen.findByRole("button", { name: "Add limit" }))
    const dialog = within(screen.getByRole("dialog"))
    await userEvent.click(dialog.getByRole("radio", { name: "Person" }))
    await userEvent.click(dialog.getByRole("button", { name: "Choose" }))
    await userEvent.type(screen.getByPlaceholderText("Find a person"), "mar")
    await userEvent.click(await screen.findByRole("option", { name: /Marcus Webb/ }))
    await userEvent.type(dialog.getByLabelText("Credits"), "3")
    await userEvent.click(dialog.getByRole("button", { name: "Add limit" }))
    expect(api.put).not.toHaveBeenCalled()
    await waitFor(() => expect(screen.getAllByRole("dialog")).toHaveLength(1))
    expect(within(screen.getByRole("dialog")).getByRole("button", { name: "Remove" })).toBeInTheDocument()
  })
})

describe("not delivered", () => {
  const open = (list = [lost, { ...lost, id: "r2", person: "Sam", price: { kind: "fixed" as const, min: "2", max: null } }]) =>
    view({ ...data, undelivered: list }, "/?undelivered=all", () => { const { data: d } = useSupportPage(); return d ? <UndeliveredDrawer d={d} /> : null })

  it("lists each request with who, what and the credits held, and says the person is not told", async () => {
    open()
    const d = within(await screen.findByRole("dialog"))
    expect(await d.findByText("Priya")).toBeInTheDocument()
    expect(d.getByText("Sam")).toBeInTheDocument()
    expect(d.getAllByText("Look")).toHaveLength(2)
    expect(d.getByText("Person told")).toBeInTheDocument()
    expect(d.getByText("No")).toBeInTheDocument()
  })

  it("settles with the charge typed", async () => {
    open()
    const d = within(await screen.findByRole("dialog"))
    const [charge] = await d.findAllByRole("textbox", { name: "Charge" })
    await userEvent.clear(charge!)
    await userEvent.type(charge!, "2.5")
    await userEvent.click(d.getAllByRole("button", { name: "Settle" })[0]!)
    await waitFor(() => expect(api.post).toHaveBeenCalledWith("/api/support/requests/r1/settle", { charge: "2.5" }))
  })

  it("holds a fixed price at its amount and bounds a range", async () => {
    open()
    const d = within(await screen.findByRole("dialog"))
    const [range, fixed] = await d.findAllByRole("textbox", { name: "Charge" })
    expect(fixed).toBeDisabled()
    expect(fixed).toHaveValue("2")
    await userEvent.clear(range!)
    await userEvent.type(range!, "9")
    expect(await d.findByText("Credits 1 to 3")).toBeInTheDocument()
    expect(d.getAllByRole("button", { name: "Settle" })[0]).toBeDisabled()
  })

  it("declines after asking", async () => {
    open()
    const d = within(await screen.findByRole("dialog"))
    await userEvent.click((await d.findAllByRole("button", { name: "Decline" }))[1]!)
    await waitFor(() => expect(api.post).toHaveBeenCalledWith("/api/support/requests/r2/decline", {}))
  })

  const charged = { ...lost, charged: true, credits: "2", price: { kind: "fixed" as const, min: "2", max: null } }

  it("keeps a charged request's charge on settle", async () => {
    open([charged])
    const d = within(await screen.findByRole("dialog"))
    expect(await d.findByText("Charged")).toBeInTheDocument()
    expect(d.queryByRole("textbox", { name: "Charge" })).toBeNull()
    await userEvent.click(d.getByRole("button", { name: "Settle" }))
    await waitFor(() => expect(api.post).toHaveBeenCalledWith("/api/support/requests/r1/settle", { charge: "2" }))
  })

  it("accepts a fixed price without an amount", async () => {
    open([{ ...charged, charged: false }])
    const d = within(await screen.findByRole("dialog"))
    await userEvent.click(await d.findByRole("button", { name: "Accept" }))
    await waitFor(() => expect(api.post).toHaveBeenCalledWith("/api/support/requests/r1/settle", {}))
  })

  it("asks to refund when declining a charged request", async () => {
    open([charged])
    const d = within(await screen.findByRole("dialog"))
    await userEvent.click(await d.findByRole("button", { name: "Decline" }))
    await waitFor(() => expect(confirmed).toHaveBeenLastCalledWith(expect.objectContaining({ title: "Decline and refund?" })))
    await waitFor(() => expect(api.post).toHaveBeenCalledWith("/api/support/requests/r1/decline", {}))
  })

  it("shows a refused charge under the input", async () => {
    open()
    const d = within(await screen.findByRole("dialog"))
    vi.mocked(api.post).mockRejectedValueOnce(new ApiError(422, "invalid", [{ loc: ["charge"], msg: "Charge is above the price." }]))
    await userEvent.click((await d.findAllByRole("button", { name: "Settle" }))[0]!)
    expect(await d.findByText("Charge is above the price.")).toBeInTheDocument()
  })

  it("reloads when the request is no longer pending", async () => {
    open()
    const d = within(await screen.findByRole("dialog"))
    vi.mocked(api.post).mockRejectedValueOnce(new ApiError(409, "gone"))
    await userEvent.click((await d.findAllByRole("button", { name: "Settle" }))[0]!)
    expect(await d.findByText("Changed meanwhile. Reloaded.")).toBeInTheDocument()
  })
})

describe("a channel's Ask a person", () => {
  const channel = () => <ChannelSupport id="c1" edit />
  const withMenus = { ...data, menus: [menu], menu_use: [{ scope: "workspace" as const, scope_id: "", name: "Workspace", menu: null }, { scope: "channel" as const, scope_id: "c1", name: "general", menu: null }] }

  it("shows the thumbs-down feedback switch and names the default menu like Menus", async () => {
    view(withMenus, "/", channel)
    expect(await screen.findByRole("combobox", { name: "Thumbs-down feedback" })).toBeInTheDocument()
    expect(screen.getByRole("combobox", { name: "Menu" })).toHaveTextContent("Built-in default")
  })

  it("shows a refused menu pick under the select", async () => {
    view(withMenus, "/", channel)
    vi.mocked(api.put).mockRejectedValueOnce(new ApiError(409, "stale"))
    await userEvent.click(await screen.findByRole("combobox", { name: "Menu" }))
    await userEvent.click(await screen.findByRole("option", { name: "Support" }))
    expect(await screen.findByText("Changed meanwhile. Reloaded.")).toBeInTheDocument()
  })
})
