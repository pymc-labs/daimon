import { describe, expect, it } from "vitest"

import { DEFAULT_SPEC, fromCron, toCron, type Spec } from "./cron"

const spec = (over: Partial<Spec>): Spec => ({ ...DEFAULT_SPEC, ...over })

describe("schedule picker", () => {
  it("writes each kind as a cron expression", () => {
    expect(toCron(spec({ kind: "hourly", time: "00:15" }))).toBe("15 * * * *")
    expect(toCron(spec({ kind: "daily", time: "08:30" }))).toBe("30 8 * * *")
    expect(toCron(spec({ kind: "weekdays" }))).toBe("0 9 * * 1-5")
    expect(toCron(spec({ kind: "weekly", weekday: 4 }))).toBe("0 9 * * 4")
    expect(toCron(spec({ kind: "monthly", day: 15 }))).toBe("0 9 15 * *")
    expect(toCron(spec({ kind: "custom", cron: " */5 * * * * " }))).toBe("*/5 * * * *")
  })

  it("reads back what it wrote", () => {
    for (const s of [spec({ kind: "daily", time: "08:30" }), spec({ kind: "weekdays" }), spec({ kind: "weekly", weekday: 0 }), spec({ kind: "monthly", day: 28 })]) {
      const back = fromCron(toCron(s))
      expect([back.kind, back.time, back.weekday, back.day]).toEqual([s.kind, s.time, s.weekday, s.day])
    }
    expect(fromCron("5 * * * *")).toMatchObject({ kind: "hourly", time: "00:05" })
  })

  it("keeps anything else as custom", () => {
    for (const c of ["*/5 * * * *", "0 9 * 6 *", "0 9 1,15 * *", "0 9 * * 1,3", "bad"]) expect(fromCron(c)).toMatchObject({ kind: "custom", cron: c })
  })
})
