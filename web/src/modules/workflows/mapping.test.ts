import { describe, expect, it } from "vitest"

import { checkInputs, mappingText, parseMapping, suggestMapping } from "./mapping"
import type { ObjectSchema } from "./types"

const payload = { event: "lead", data: { email: "a@b.co", items: [{ id: 7 }] } }
const schema: ObjectSchema = { type: "object", required: ["email", "message"], properties: { email: { type: "string" }, message: { type: "string" }, size: { type: "integer" } } }

describe("checkInputs", () => {
  it("flags missing required, wrong types and extra keys", () => {
    const r = checkInputs(schema, { email: "a@b.co", size: "40", extra: 1 })
    expect(r.rows.map((x) => x.state)).toEqual(["ok", "missing", "wrong_type"])
    expect(r.extra).toEqual(["extra"])
    expect(r.valid).toBe(false)
  })
  it("is valid when the required inputs fit", () => expect(checkInputs(schema, { email: "a", message: "b" }).valid).toBe(true))
})

describe("suggestMapping", () => {
  it("picks the level that holds the inputs", () => {
    expect(suggestMapping(schema, { email: "a" })).toBe("${{ payload }}")
    expect(suggestMapping(schema, payload)).toBe("${{ payload.data }}")
  })
})

describe("field map mappings", () => {
  it("round-trip through the one-line field", () => {
    const m = { meeting_id: "${{ payload.meeting.external }}" }
    expect(parseMapping(mappingText(m))).toEqual(m)
    expect(parseMapping("${{ payload.data }}")).toBe("${{ payload.data }}")
    expect(parseMapping("{ not json")).toBe("{ not json")
  })
})
