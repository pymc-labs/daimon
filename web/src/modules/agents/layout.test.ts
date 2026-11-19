import { describe, expect, it } from "vitest"

import { bounds, depLine, layoutNarrow, layoutRing, spoke, type Box, type Group, type Pt, type Size } from "./layout"

const gap = (a: Box, b: Box) => Math.max(b.x - (a.x + a.w), a.x - (b.x + b.w), b.y - (a.y + a.h), a.y - (b.y + b.h))
const mid = (b: Box): Pt => ({ x: b.x + b.w / 2, y: b.y + b.h / 2 })

function randomRing(seed: number, conns: number, view = 1.6) {
  let n = seed
  const rand = () => ((n = (n * 1103515245 + 12345) % 2 ** 31) / 2 ** 31)
  const sizes: Record<string, Size> = { agent: { w: 272, h: 140 + rand() * 80 } }
  const card = (id: string, w = 240) => { sizes[id] = { w, h: 40 + Math.round(rand() * 272) }; return id }
  const groups: Group[] = [
    { ids: ["github", "keys", "helpers", "tools", "skills"].map((id) => card(id)), at: 60 },
    { ids: Array.from({ length: conns }, (_, i) => card(`c${i}`, 256)), at: 180 },
    { ids: ["people", "places", "model", "instructions"].map((id) => card(id)), at: 300 },
  ]
  const size = (id: string) => sizes[id]!
  const pos = layoutRing("agent", groups, size, view)
  const box = (id: string): Box => ({ ...pos[id]!, ...size(id) })
  return { groups, size, pos, box }
}

describe("agent editor ring", () => {
  it.each([0, 1, 3, 6, 12, 20])("never overlaps cards, or the agent, with %i connections", (conns) => {
    for (let seed = 1; seed < 30; seed++) {
      const { groups, box } = randomRing(seed, conns, [0.8, 1.6, 2.4][seed % 3])
      const all = ["agent", ...groups.flatMap((g) => g.ids)]
      for (let i = 0; i < all.length; i++) for (let j = i + 1; j < all.length; j++) {
        expect(gap(box(all[i]!), box(all[j]!)), `${all[i]} and ${all[j]} overlap (seed ${seed})`).toBeGreaterThan(0)
      }
    }
  })

  it("keeps the gap between groups far wider than any gap inside one", () => {
    for (let seed = 1; seed < 30; seed++) {
      const { groups, box } = randomRing(seed, 4, [0.8, 1.6][seed % 2])
      const order = groups.flatMap((g, k) => g.ids.map((id) => ({ id, k })))
      const inner: number[] = [], between: number[] = []
      order.forEach((a, i) => {
        const b = order[(i + 1) % order.length]!
        ;(a.k === b.k ? inner : between).push(gap(box(a.id), box(b.id)))
      })
      expect(Math.min(...between), `seed ${seed}`).toBeGreaterThan(3 * Math.max(...inner))
    }
  })

  it("leans each group to its side of the agent", () => {
    for (let seed = 1; seed < 30; seed++) {
      const { groups, box } = randomRing(seed, 4, [0.8, 1.6][seed % 2])
      const c = mid(box("agent"))
      const centre = (ids: string[]) => ids.map((id) => mid(box(id))).reduce((s, p) => ({ x: s.x + p.x / ids.length, y: s.y + p.y / ids.length }), { x: 0, y: 0 })
      expect(centre(groups[0]!.ids).x, "abilities sit right").toBeGreaterThan(c.x)
      expect(centre(groups[1]!.ids).y, "connections sit below").toBeGreaterThan(c.y)
      expect(centre(groups[2]!.ids).x, "the brief sits left").toBeLessThan(c.x)
    }
  })

  it("still closes the ring when one card grows", () => {
    const { groups, size, pos } = randomRing(5, 6)
    const grown = (id: string) => (id === "c2" ? { w: 256, h: 900 } : size(id))
    const after = layoutRing("agent", groups, grown, 1.6)
    expect(after.c2, "the grown card moves").not.toEqual(pos.c2)
    const all = ["agent", ...groups.flatMap((g) => g.ids)]
    const box = (id: string): Box => ({ ...after[id]!, ...grown(id) })
    for (let i = 0; i < all.length; i++) for (let j = i + 1; j < all.length; j++) {
      expect(gap(box(all[i]!), box(all[j]!)), `${all[i]} and ${all[j]}`).toBeGreaterThan(0)
    }
  })


  it.each([0.8, 1.2, 1.8])("takes the shape of a %f screen", (view) => {
    for (let seed = 1; seed < 10; seed++) {
      const { pos, size } = randomRing(seed, 6, view)
      const b = bounds(pos, size)
      expect(b.w / b.h / view, `seed ${seed}`).toBeCloseTo(1, 1)
    }
  })
})

describe("agent editor lines", () => {
  it("runs a spoke from the agent's border to the card's", () => {
    const a: Box = { x: 0, y: 0, w: 200, h: 100 }, b: Box = { x: 400, y: 300, w: 100, h: 100 }
    const [p, q] = spoke(a, b)
    expect(p!.x === 200 || p!.y === 100, "it leaves on the agent's border").toBe(true)
    expect(q!.x === 400 || q!.y === 300, "it lands on the card's border").toBe(true)
  })

  it("routes a skill's line round the agent, never across it", () => {
    for (let seed = 1; seed < 30; seed++) {
      const { box } = randomRing(seed, 6)
      const agent = box("agent")
      for (const [i, target] of ["c0", "c3", "c5"].entries()) {
        const pts = depLine(box("skills"), 60, box(target), agent, i)
        const along = pts.slice(1).flatMap((b, k) => [0, 0.25, 0.5, 0.75].map((f) => ({ x: pts[k]!.x + (b.x - pts[k]!.x) * f, y: pts[k]!.y + (b.y - pts[k]!.y) * f })))
        for (const p of along) {
          const inside = p.x > agent.x && p.x < agent.x + agent.w && p.y > agent.y && p.y < agent.y + agent.h
          expect(inside, `seed ${seed}: line to ${target} crosses the agent at ${p.x},${p.y}`).toBe(false)
        }
      }
    }
  })
})

describe("agent editor column", () => {
  it("stacks every card under the agent without overlap", () => {
    const size = (id: string) => ({ w: 240, h: id.length * 20 })
    const ids = ["instructions", "model", "skills", "c0"]
    const pos = layoutNarrow("agent", ids, size)
    const boxes = ["agent", ...ids].map((id): Box => ({ ...pos[id]!, ...size(id) }))
    boxes.slice(1).forEach((b, i) => expect(b.y, `${ids[i]} sits below the card before`).toBeGreaterThan(boxes[i]!.y + boxes[i]!.h))
  })
})
