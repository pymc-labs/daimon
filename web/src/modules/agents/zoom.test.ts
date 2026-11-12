import { describe, expect, it } from "vitest"

import { wheelZoom } from "./zoom"

const wheel = (deltaY: number, timeStamp: number, mods: { ctrlKey?: boolean; metaKey?: boolean }) =>
  ({ deltaY, deltaMode: 0, ctrlKey: false, metaKey: false, timeStamp, ...mods })

describe("wheel zoom", () => {
  it("zooms one mouse notch as the stock handler does", () => {
    const zoom = wheelZoom(false)
    expect(zoom(wheel(100, 1000, { ctrlKey: true })), "a Windows notch is x1.15").toBeCloseTo(-0.2)
    expect(zoom(wheel(100, 1400, { ctrlKey: true })), "the next notch is too").toBeCloseTo(-0.2)
    expect(zoom(wheel(-4, 2000, { metaKey: true })), "a slow Mac notch is untouched").toBeCloseTo(0.008)
  })

  it("caps the burst a smooth-scrolling tool makes of one notch", () => {
    const zoom = wheelZoom(true)
    let total = 0
    for (let i = 0; i < 30; i++) total += zoom(wheel(-28, 1000 + i * 10, { metaKey: true }))
    expect(2 ** total, "840px in 300ms zooms 3.2x uncapped").toBeLessThan(1.5)
  })

  it("lets a trackpad pinch follow the fingers", () => {
    const zoom = wheelZoom(true)
    let total = 0
    for (let i = 0; i < 30; i++) total += zoom(wheel(-3, 1000 + i * 10, { ctrlKey: true }))
    expect(total, "no cap on a pinch").toBeCloseTo(1.8)
  })
})
