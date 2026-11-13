import { describe, expect, it } from "vitest"

import { checkInputs, evalMapping, suggestMapping } from "./mapping"
import type { ObjectSchema } from "./types"

const d = { headers: { "x-id": "d1" }, payload: { event: "lead", data: { email: "a@b.co", items: [{ id: 7 }] } } }
const schema: ObjectSchema = { type: "object", required: ["email", "message"], properties: { email: { type: "string" }, message: { type: "string" }, size: { type: "integer" } } }

describe("evalMapping", () => {
  it("follows paths and indexes", () => {
    expect(evalMapping("${{ payload.data.email }}", d)).toEqual({ ok: true, value: "a@b.co" })
    expect(evalMapping("${{payload.data.items[0].id}}", d)).toEqual({ ok: true, value: 7 })
    expect(evalMapping("${{ headers.x-id }}", d)).toEqual({ ok: true, value: "d1" })
  })
  it("names what is wrong", () => {
    expect(evalMapping("${{ payload.nope.x }}", d)).toEqual({ ok: false, error: "No payload.nope in this delivery" })
    expect(evalMapping("${{ steps.a }}", d)).toMatchObject({ ok: false })
    expect(evalMapping("payload", d)).toMatchObject({ ok: false })
  })
})

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
    expect(suggestMapping(schema, d.payload)).toBe("${{ payload.data }}")
  })
})
