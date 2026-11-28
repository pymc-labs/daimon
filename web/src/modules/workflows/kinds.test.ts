import { describe, expect, it } from "vitest"

import { triggerText } from "./kinds"

describe("triggerText", () => {
  it("names schedules that repeat every few weeks or run once", () => {
    expect(triggerText({ id: "t", kind: "schedule", cron: "0 9 * * 1" })).toBe("Mon 09:00")
    expect(triggerText({ id: "t", kind: "schedule", cron: "0 9 * * 1", every_weeks: 2, anchor: "2026-11-02" })).toBe("Mon 09:00, every 2 weeks")
    expect(triggerText({ id: "t", kind: "schedule", at: "2026-12-01T09:00:00Z" })).toMatch(/^Once 1 Dec 2026/)
  })
})
