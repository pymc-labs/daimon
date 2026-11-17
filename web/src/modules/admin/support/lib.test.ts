import { describe, expect, it } from "vitest"

import type { FeatureView, TierValue } from "@/modules/features/types"
import { askedOn, chargeRange, openIn } from "./lib"

const tier = (t: TierValue["tier"], value: string, locked = false): TierValue => ({ tier: t, value, locked, text: value })
const view = (...tiers: TierValue[]) => ({ key: "support_escalation", tiers, value: "off", source: "default", locked: false, unavailable: "unmet", summary: "" }) as FeatureView

describe("askedOn", () => {
  it("reads what the admin set, not the resolved value", () => {
    expect(askedOn(view(tier("workspace", "on")))).toBe(true)
    expect(askedOn(view(tier("workspace", "off")))).toBe(false)
    expect(askedOn(view(tier("workspace", "default")))).toBe(false)
    expect(askedOn(undefined)).toBe(false)
  })

  it("lets a locked deployment value win over the workspace", () => {
    expect(askedOn(view(tier("deployment", "off", true), tier("workspace", "on")))).toBe(false)
    expect(askedOn(view(tier("deployment", "on", true), tier("workspace", "off")))).toBe(true)
  })

  it("ignores channel and agent tiers", () => {
    expect(askedOn(view(tier("workspace", "off"), tier("channel", "on")))).toBe(false)
  })
})

describe("openIn", () => {
  it("lists distinct channels, and none for no channel", () => {
    expect(openIn(null)).toEqual([])
    expect(openIn("a")).toEqual(["a"])
    expect(openIn(["a", "b", "a"])).toEqual(["a", "b"])
  })
})

describe("chargeRange", () => {
  it("bounds a fixed price at its amount, a range by its ends and a quote only below", () => {
    expect(chargeRange({ kind: "fixed", min: "2", max: null })).toEqual({ min: 2, max: 2 })
    expect(chargeRange({ kind: "range", min: "1", max: "3" })).toEqual({ min: 1, max: 3 })
    expect(chargeRange({ kind: "quoted", min: "0", max: "5" })).toEqual({ min: 0, max: 5 })
    expect(chargeRange({ kind: "quoted", min: "0", max: null })).toEqual({ min: 0, max: null })
  })
})
