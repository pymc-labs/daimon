import { describe, expect, it } from "vitest"

import { inAppPath } from "./format"

const ORIGIN = "https://app.example.com"

describe("inAppPath", () => {
  it("keeps a workspace link in the app, with its query", () => {
    expect(inAppPath(`${ORIGIN}/w/6f1d0c4e-0000-4000-8000-000000000001/agents/ag_1?node=skills`, ORIGIN)).toBe("/w/6f1d0c4e-0000-4000-8000-000000000001/agents/ag_1?node=skills")
  })
  it("leaves other origins, downloads and odd schemes as ordinary links", () => {
    expect(inAppPath("https://other.example.com/w/x/chat", ORIGIN)).toBeNull()
    expect(inAppPath(`${ORIGIN}/api/chat/attachments/abc`, ORIGIN)).toBeNull()
    expect(inAppPath("javascript:alert(1)", ORIGIN)).toBeNull()
    expect(inAppPath(undefined, ORIGIN)).toBeNull()
  })
})
