import { describe, expect, it } from "vitest"

import { inAppPath } from "./format"

const ORIGIN = "https://app.example.com"
const HERE = "6f1d0c4e-0000-4000-8000-000000000001"
const OTHER = "6f1d0c4e-0000-4000-8000-000000000002"

describe("inAppPath", () => {
  it("keeps a workspace link in the app, with its query", () => {
    expect(inAppPath(`${ORIGIN}/w/${HERE}/agents/ag_1?node=skills`, ORIGIN, HERE)).toBe(`/w/${HERE}/agents/ag_1?node=skills`)
  })
  it("leaves another workspace's link, and every link before the workspace is known, as an ordinary link", () => {
    expect(inAppPath(`${ORIGIN}/w/${OTHER}/agents/ag_1`, ORIGIN, HERE)).toBeNull()
    expect(inAppPath(`${ORIGIN}/w/${HERE}x/agents/ag_1`, ORIGIN, HERE)).toBeNull()
    expect(inAppPath(`${ORIGIN}/w/${HERE}/agents/ag_1`, ORIGIN, undefined)).toBeNull()
  })
  it("leaves other origins, downloads and odd schemes as ordinary links", () => {
    expect(inAppPath("https://other.example.com/w/x/chat", ORIGIN, HERE)).toBeNull()
    expect(inAppPath(`${ORIGIN}/api/chat/attachments/abc`, ORIGIN, HERE)).toBeNull()
    expect(inAppPath("javascript:alert(1)", ORIGIN, HERE)).toBeNull()
    expect(inAppPath(undefined, ORIGIN, HERE)).toBeNull()
  })
})
