import { describe, expect, it } from "vitest"

import { boxes, layoutNarrow, layoutWide, pack, type Box, type Plan, type Size } from "./layout"

const overlaps = (a: Box, b: Box) => a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h

function randomPlan(seed: number, conns: number): { plan: Plan; size: (id: string) => Size } {
  let n = seed
  const rand = () => ((n = (n * 1103515245 + 12345) % 2 ** 31) / 2 ** 31)
  const sizes: Record<string, Size> = { agent: { w: 272, h: 140 + rand() * 80 } }
  const card = (id: string, w = 240) => { sizes[id] = { w, h: 40 + Math.round(rand() * 400) }; return id }
  const plan: Plan = {
    left: ["instructions", "model", "places"].map((id) => card(id)),
    right: ["skills", "tools"].map((id) => card(id)),
    hub: card("hub", 150),
    conns: Array.from({ length: conns }, (_, i) => card(`c${i}`, 256)),
    skills: "skills",
    deps: conns ? [{ key: "s1", target: "c0", y: 60 }] : [],
  }
  return { plan, size: (id) => sizes[id]! }
}

describe("agent editor layout", () => {
  it.each([0, 1, 2, 3, 5, 9, 14])("never overlaps cards with %i connections", (conns) => {
    for (let seed = 1; seed < 40; seed++) {
      const { plan, size } = randomPlan(seed, conns)
      for (const laid of [layoutWide("agent", plan, size), layoutNarrow("agent", plan, size)]) {
        const all = boxes(laid, size)
        for (let i = 0; i < all.length; i++) for (let j = i + 1; j < all.length; j++) {
          expect(overlaps(all[i]!, all[j]!), `cards ${i} and ${j} overlap (seed ${seed})`).toBe(false)
        }
      }
    }
  })

  it("puts connections below both columns, centred on the agent", () => {
    const { plan, size } = randomPlan(7, 2)
    const { pos } = layoutWide("agent", plan, size)
    const lowest = Math.max(...[...plan.left, ...plan.right, "agent"].map((id) => pos[id]!.y + size(id).h))
    const cx = pos.agent!.x + size("agent").w / 2
    const row = plan.conns.map((id) => pos[id]!)
    expect(Math.min(...row.map((p) => p.y)), "the branch starts below the lowest card").toBeGreaterThan(lowest)
    const mid = (row[0]!.x + row[1]!.x + size("c1").w) / 2
    expect(Math.abs(mid - cx), "the row is centred on the agent").toBeLessThan(1)
  })

  it("wraps wide branches into rows", () => {
    const rows = pack(["a", "b", "c", "d"], () => ({ w: 100, h: 10 }), 230)
    expect(rows, "two cards fit a row").toEqual([["a", "b"], ["c", "d"]])
  })
})
