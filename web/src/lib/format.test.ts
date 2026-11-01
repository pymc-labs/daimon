import { describe, expect, it } from "vitest"
import { channelLabel } from "./format"

describe("channelLabel", () => {
  it("prefixes a named channel and leaves the server's plain labels alone", () => {
    expect(channelLabel("general")).toBe("#general")
    expect(channelLabel("Deleted channel")).toBe("Deleted channel")
    expect(channelLabel("Direct message")).toBe("Direct message")
    expect(channelLabel(null)).toBe("Unnamed channel")
  })
})
