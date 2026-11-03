import { describe, expect, it } from "vitest"
import { channelLabel, modelLabel, toolLabel, usd } from "./format"

describe("channelLabel", () => {
  it("prefixes a named channel and leaves the server's plain labels alone", () => {
    expect(channelLabel("general")).toBe("#general")
    expect(channelLabel("Deleted channel")).toBe("Deleted channel")
    expect(channelLabel("Direct message")).toBe("Direct message")
    expect(channelLabel(null)).toBe("Unnamed channel")
  })

  it("gives threads, forum posts, categories and teams no hash", () => {
    expect(channelLabel("Launch", "thread")).toBe("Launch")
    expect(channelLabel("How to read ROAS?", "forum_post")).toBe("How to read ROAS?")
    expect(channelLabel("Projects", "category")).toBe("Projects")
    expect(channelLabel("Ops team", "team")).toBe("Ops team")
    expect(channelLabel("questions", "channel")).toBe("#questions")
    expect(channelLabel("general", null)).toBe("#general")
  })
})

describe("modelLabel", () => {
  it("reads a model id as a name, and prefers a known label", () => {
    expect(modelLabel("gpt-5")).toBe("GPT-5")
    expect(modelLabel("gpt-5-mini")).toBe("GPT-5 Mini")
    expect(modelLabel("claude-sonnet-4-5")).toBe("Claude Sonnet 4.5")
    expect(modelLabel("claude-opus-4-1-20250805")).toBe("Claude Opus 4.1")
    expect(modelLabel("gpt-5", "GPT 5")).toBe("GPT 5")
  })
})

describe("toolLabel", () => {
  it("names the server and the tool, never the key", () => {
    expect(toolLabel("search", "web_search")).toBe("Search: Web search")
    expect(toolLabel("mail", "send_email")).toBe("Mail: Send email")
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
