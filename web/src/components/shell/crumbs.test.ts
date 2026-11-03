import { describe, expect, it } from "vitest"

import { crumbsWithTab } from "./crumbs"

const tabs = [{ label: "Channels", to: "/channels", end: true }, { label: "Defaults", to: "/channels/defaults" }]

describe("crumbsWithTab", () => {
  it("appends the active tab's label to a lone crumb", () => {
    expect(crumbsWithTab([{ label: "Channels" }], tabs, "/channels/defaults")).toEqual([{ label: "Channels" }, { label: "Defaults" }])
  })
  it("skips a tab named like the module", () => {
    expect(crumbsWithTab([{ label: "Channels" }], tabs, "/channels")).toEqual([{ label: "Channels" }])
  })
  it("leaves longer trails and untabbed pages alone", () => {
    const trail = [{ label: "Agents", to: "/agents" }, { label: "Skills" }]
    expect(crumbsWithTab(trail, tabs, "/channels/defaults")).toBe(trail)
    expect(crumbsWithTab([{ label: "Channels" }], undefined, "/channels")).toEqual([{ label: "Channels" }])
  })
})
