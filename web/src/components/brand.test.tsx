import { render } from "@testing-library/react"
import { describe, expect, it } from "vitest"

import { BrandIcon } from "./brand"

const rects = (brand: string) => render(<BrandIcon brand={brand} />).container.querySelectorAll("rect").length

describe("BrandIcon", () => {
  it("draws the Slack and Microsoft marks, not the generic globe", () => {
    expect(rects("slack")).toBe(4)
    expect(rects("microsoft")).toBe(4)
  })

  it("falls back to the globe for a method it has no mark for", () => {
    expect(rects("unknown")).toBe(0)
  })
})
