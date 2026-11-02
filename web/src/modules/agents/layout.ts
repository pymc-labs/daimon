// Agent editor layout: pure, from measured card sizes. Wide: two columns around the agent, connections
// branching down from it in rows. Narrow: one column on a spine.

export type Pt = { x: number; y: number }
export type Size = { w: number; h: number }
export type Box = Pt & Size
/** A dashed line from a skill row (`y` inside the skills card) to the connection it calls. */
export type Dep = { key: string; target: string; y: number }
/** Card ids by place; `skills` is the card deps start from. */
export type Plan = { left: string[]; right: string[]; hub: string | null; conns: string[]; skills: string | null; deps: Dep[] }
export type Line = { source: string; target: string; points: Pt[]; dep?: boolean }
export type Laid = { pos: Record<string, Pt>; edges: Record<string, Line> }

const line = (source: string, target: string, points: Pt[], dep = false): [string, Line] =>
  [dep ? `dep:${source}>${target}` : `${source}>${target}`, { source, target, points, dep }]

const GAP_X = 72
const GAP_Y = 20
const HEAD = 20 // y of a card's connector, mid header
const HUB_GAP = 32 // below the columns to the hub
const BUS = 24 // bus line above a row of connections
const ROW_GAP = 48
const GUTTER = 48 // gap the trunk passes through in rows above the last
const SPINE = 18

const stack = (ids: string[], size: (id: string) => Size, gap: number) =>
  ids.reduce((h, id, i) => h + size(id).h + (i ? gap : 0), 0)

/** Greedy rows no wider than `cap`; at least one card a row. */
export function pack(ids: string[], size: (id: string) => Size, cap: number, gap = GAP_Y): string[][] {
  const rows: string[][] = []
  let width = 0
  for (const id of ids) {
    const w = size(id).w
    const row = rows.at(-1)
    if (row && width + gap + w <= cap) { row.push(id); width += gap + w }
    else { rows.push([id]); width = w }
  }
  return rows
}

export function layoutWide(agent: string, plan: Plan, size: (id: string) => Size): Laid {
  const pos: Record<string, Pt> = {}
  const edges: Record<string, Line> = {}
  const link = (...a: Parameters<typeof line>) => { const [k, v] = line(...a); edges[k] = v }
  const colW = (ids: string[]) => Math.max(0, ...ids.map((id) => size(id).w))
  const a = size(agent)
  const wL = colW(plan.left), wR = colW(plan.right)
  const H = Math.max(stack(plan.left, size, GAP_Y), stack(plan.right, size, GAP_Y), a.h)
  const xA = wL ? wL + GAP_X : 0
  const xR = xA + a.w + GAP_X
  pos[agent] = { x: xA, y: Math.round((H - a.h) / 2) }
  const aMid = pos[agent].y + a.h / 2
  const cx = xA + a.w / 2
  const column = (ids: string[], x: (w: number) => number, side: "left" | "right") => {
    let y = Math.round((H - stack(ids, size, GAP_Y)) / 2)
    for (const id of ids) {
      const { w, h } = size(id)
      pos[id] = { x: x(w), y }
      if (side === "left") {
        const mx = (wL + xA) / 2
        link(id, agent, [{ x: x(w) + w, y: y + HEAD }, { x: mx, y: y + HEAD }, { x: mx, y: aMid }, { x: xA, y: aMid }])
      } else {
        const mx = (xA + a.w + xR) / 2
        link(agent, id, [{ x: xA + a.w, y: aMid }, { x: mx, y: aMid }, { x: mx, y: y + HEAD }, { x: x(w), y: y + HEAD }])
      }
      y += h + GAP_Y
    }
  }
  column(plan.left, (w) => wL - w, "left")
  column(plan.right, () => xR, "right")
  if (!plan.hub) return { pos, edges }

  const hub = size(plan.hub)
  pos[plan.hub] = { x: Math.round(cx - hub.w / 2), y: H + HUB_GAP }
  link(agent, plan.hub, [{ x: cx, y: pos[agent].y + a.h }, { x: cx, y: H + HUB_GAP }])
  const hubBottom = H + HUB_GAP + hub.h
  const topW = (plan.right.length ? xR + wR : xA + a.w)
  const rows = pack(plan.conns, size, Math.max(topW, colW(plan.conns) * 2 + GAP_Y + GUTTER) - GUTTER)
  let top = hubBottom + BUS + 12
  const right: number[] = []
  rows.forEach((row, r) => {
    const last = r === rows.length - 1
    const ws = row.map((id) => size(id).w)
    const place = (ids: string[], x0: number) => {
      let x = x0
      for (const id of ids) { pos[id] = { x: Math.round(x), y: top }; x += size(id).w + GAP_Y }
    }
    if (last) {
      place(row, cx - (ws.reduce((s, w) => s + w, 0) + GAP_Y * (row.length - 1)) / 2)
    } else {
      // Rows above the last split around the trunk so it reaches the rows below.
      const k = Math.ceil(row.length / 2)
      const leftW = ws.slice(0, k).reduce((s, w) => s + w, 0) + GAP_Y * (k - 1)
      place(row.slice(0, k), cx - GUTTER / 2 - leftW)
      place(row.slice(k), cx + GUTTER / 2)
    }
    const bus = top - BUS
    for (const id of row) {
      const c = pos[id]!.x + size(id).w / 2
      link(plan.hub!, id, [{ x: cx, y: hubBottom }, { x: cx, y: bus }, { x: c, y: bus }, { x: c, y: top }])
      right.push(pos[id]!.x + size(id).w)
    }
    top += Math.max(...row.map((id) => size(id).h)) + ROW_GAP
  })
  // Skill dependencies run right of everything, then in above their connection.
  const skills = plan.skills
  if (skills && pos[skills]) {
    const edge = Math.max(xR + wR, ...right) + 20
    plan.deps.forEach((d, i) => {
      const t = pos[d.target]
      if (!t) return
      const x = edge + i * 6
      const y = t.y - BUS / 2 + (i % 3) * 3 - 3
      const tx = t.x + size(d.target).w / 2 + 14 + (i % 3) * 6
      const s = pos[skills]!
      edges[`dep:${d.key}`] = { source: skills, target: d.target, dep: true, points: [{ x: s.x + size(skills).w, y: s.y + d.y }, { x, y: s.y + d.y }, { x, y }, { x: tx, y }, { x: tx, y: t.y }] }
    })
  }
  return { pos, edges }
}

/** One column: the agent, then each card on a spine down its left. */
export function layoutNarrow(agent: string, plan: Plan, size: (id: string) => Size): Laid {
  const pos: Record<string, Pt> = { [agent]: { x: SPINE, y: 0 } }
  const edges: Record<string, Line> = {}
  const link = (...a: Parameters<typeof line>) => { const [k, v] = line(...a); edges[k] = v }
  const aMid = size(agent).h / 2
  let y = size(agent).h + GAP_Y
  for (const id of [...plan.left, ...plan.right, ...(plan.hub ? [plan.hub] : []), ...plan.conns]) {
    pos[id] = { x: SPINE, y }
    link(agent, id, [{ x: SPINE, y: aMid }, { x: 0, y: aMid }, { x: 0, y: y + HEAD }, { x: SPINE, y: y + HEAD }])
    y += size(id).h + GAP_Y
  }
  return { pos, edges }
}

/** Every card's box, for overlap checks. */
export const boxes = (laid: Laid, size: (id: string) => Size): Box[] =>
  Object.entries(laid.pos).map(([id, p]) => ({ ...p, ...size(id) }))
