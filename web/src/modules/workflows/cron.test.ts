import { describe, expect, it } from "vitest"

import { cronWords, nextFires } from "./cron"

const from = Date.parse("2026-11-12T10:00:00Z")

describe("cronWords", () => {
  it("reads the picker's schedules", () => {
    expect(cronWords("0 9 * * 1")).toBe("Every Monday at 09:00")
    expect(cronWords("30 8 * * 1-5")).toBe("Every weekday at 08:30")
    expect(cronWords("0 7 1 * *")).toBe("On day 1 of every month at 07:00")
    expect(cronWords("*/5 * * * *")).toBe("A custom schedule")
  })
})

describe("nextFires", () => {
  it("lists the next three Mondays in the zone", () => {
    expect(nextFires("0 9 * * 1", "Europe/Stockholm", from)).toEqual(["2026-11-16T08:00:00.000Z", "2026-11-23T08:00:00.000Z", "2026-11-30T08:00:00.000Z"])
  })
  it("steps through hours and rolls into the next day", () => {
    expect(nextFires("0 */8 * * *", "UTC", from)).toEqual(["2026-11-12T16:00:00.000Z", "2026-11-13T00:00:00.000Z", "2026-11-13T08:00:00.000Z"])
  })
  it("finds monthly dates and ignores what it cannot read", () => {
    expect(nextFires("0 7 1 * *", "UTC", from, 2)).toEqual(["2026-12-01T07:00:00.000Z", "2027-01-01T07:00:00.000Z"])
    expect(nextFires("nope", "UTC", from)).toEqual([])
  })
})
