import { describe, expect, it } from "vitest"
import { channelLabel, usd } from "./format"

describe("channelLabel", () => {
  it("prefixes a named channel and leaves the server's plain labels alone", () => {
    expect(channelLabel("general")).toBe("#general")
    expect(channelLabel("Deleted channel")).toBe("Deleted channel")
    expect(channelLabel("Direct message")).toBe("Direct message")
    expect(channelLabel(null)).toBe("Unnamed channel")
  })
})

describe("usd", () => {
  it("never shows spend as $0.00", () => {
    expect(usd(0)).toBe("$0.00")
    expect(usd(0.0013)).toBe("<$0.01")
    expect(usd(0.005)).toBe("$0.01")
    expect(usd(4.87)).toBe("$4.87")
    expect(usd(null)).toBe("–")
  })
})
