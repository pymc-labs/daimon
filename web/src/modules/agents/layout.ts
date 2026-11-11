// Agent editor layout: pure, from measured card sizes. Wide: groups of cards on a ring around the agent.
// Narrow: one column on a spine. Lines are drawn live from card boxes, so cards can glide between layouts.

export type Pt = { x: number; y: number }
export type Size = { w: number; h: number }
export type Box = Pt & Size
/** Cards that sit together, clockwise, and the angle the group leans to (degrees clockwise from the top). */
export type Group = { ids: string[]; at: number }

const GAP = 24 // between cards of a group
const GROUP_GAP = 3 * GAP // between groups
const CLEAR = 56 // from the ellipse round the agent card to the ring
const ASPECT = 1.3 // ring width over height, for landscape screens
const SPREAD = 2 // most a ring with room to spare widens its gaps
const GAP_Y = 20 // narrow column
const HEAD = 20 // y of a card's connector, mid header
const SPINE = 18
const TURN = 2 * Math.PI
const STEP = Math.PI / 180

const ring = (t: number, r: number): Pt => ({ x: ASPECT * r * Math.sin(t), y: -r * Math.cos(t) })
const mid = (b: Box): Pt => ({ x: b.x + b.w / 2, y: b.y + b.h / 2 })
/** Gap between two boxes given by centre and size: the larger of the x and y gaps, negative when they overlap. */
const gapOf = (a: Pt, as: Size, b: Pt, bs: Size) =>
  Math.max(Math.abs(a.x - b.x) - (as.w + bs.w) / 2, Math.abs(a.y - b.y) - (as.h + bs.h) / 2)
/** A box (centre `c`) clears the ellipse of radii `e` round the origin: its nearest point lies outside. */
const clears = (c: Pt, s: Size, e: Size) =>
  (Math.max(0, Math.abs(c.x) - s.w / 2) / e.w) ** 2 + (Math.max(0, Math.abs(c.y) - s.h / 2) / e.h) ** 2 >= 1

type Item = Size & { id: string; g: number }

/** Smallest ring on which the card clears the hub wherever it lands. */
function hubRadius(s: Size, hub: Size) {
  const ok = (r: number) => { for (let t = 0; t < TURN; t += 5 * STEP) if (!clears(ring(t, r), s, hub)) return false; return true }
  let lo = 0, hi = 4096
  for (let k = 0; k < 20; k++) { const m = (lo + hi) / 2; if (ok(m)) hi = m; else lo = m }
  return hi
}

/** Gaps to keep: `s` widens every gap alike, `extra` adds to the gaps between groups. */
type Gaps = { s: number; extra: number }

/** Cards placed clockwise from `start`, each as close as its gap allows; null when they do not close the ring. */
function sweep(items: Item[], r: number, gaps: Gaps, start: number): number[] | null {
  const t: number[] = []
  const at = (i: number, a: number) => { // item i at angle a keeps its gap from the eight cards before it
    const p = ring(a, r)
    for (let j = Math.max(0, t.length - 8); j < t.length; j++) {
      if (j === i) continue
      const need = items[i]!.g === items[j]!.g ? GAP * gaps.s : GROUP_GAP * gaps.s + gaps.extra
      if (gapOf(p, items[i]!, ring(t[j]!, r), items[j]!) < need) return false
    }
    return true
  }
  const next = (i: number, from: number) => {
    let a = from
    while (!at(i, a)) { a += STEP; if (a > from + Math.PI) return null }
    for (let lo = a - STEP, k = 0; k < 12 && a > from; k++) { const m = (lo + a) / 2; if (at(i, m)) a = m; else lo = m }
    return a
  }
  for (let i = 0; i < items.length; i++) {
    const a = i ? next(i, t[i - 1]!) : start
    if (a == null) return null
    t.push(a)
  }
  // The first card again, one turn on, must clear the last ones.
  const close = items.length > 1 ? next(0, t.at(-1)!) : start + TURN
  return close != null && close <= start + TURN ? t : null
}

type Fit = { r: number; t: number[] }

/** Largest `v` in [lo, hi] whose ring still closes, starting from the fit at `lo`. */
function most(lo: number, hi: number, fits: (v: number) => number[] | null, first: number[]) {
  const top = fits(hi)
  if (top) return { v: hi, t: top }
  let best = { v: lo, t: first }
  for (let k = 0; k < 12; k++) { const m = (lo + hi) / 2, t = fits(m); if (t) { lo = m; best = { v: m, t } } else hi = m }
  return best
}

/** The smallest ring, no smaller than `floor`, the cards close round from `start`; room to spare goes to the gaps. */
function solve(items: Item[], floor: number, start: number): Fit {
  const plain = { s: 1, extra: 0 }
  const t = sweep(items, floor, plain, start)
  if (t) {
    // Room to spare: widen every gap alike, then give what is left to the gaps between groups.
    const even = most(1, SPREAD, (s) => sweep(items, floor, { s, extra: 0 }, start), t)
    return { r: floor, t: most(0, 4 * floor, (extra) => sweep(items, floor, { s: even.v, extra }, start), even.t).t }
  }
  // Grow the ring until the cards fit round it, then shrink it back to the smallest that does.
  let lo = floor, hi = floor * 1.5, best: number[] | null
  while (!(best = sweep(items, hi, plain, start))) { lo = hi; hi *= 1.5 }
  for (let k = 0; k < 14; k++) { const m = (lo + hi) / 2, f = sweep(items, m, plain, start); if (f) { hi = m; best = f } else lo = m }
  return { r: hi, t: best }
}

/** Cards on a ring round the agent: groups apart, cards within a group evenly close, no overlaps. */
export function layoutRing(agent: string, groups: Group[], size: (id: string) => Size): Record<string, Pt> {
  const a = size(agent)
  const pos: Record<string, Pt> = { [agent]: { x: -a.w / 2, y: -a.h / 2 } }
  const gs = groups.filter((g) => g.ids.length)
  const items: Item[] = gs.flatMap((g, i) => g.ids.map((id) => ({ id, g: i, ...size(id) })))
  if (!items.length) return pos
  const hub = { w: (a.w / 2) * Math.SQRT2 + CLEAR, h: (a.h / 2) * Math.SQRT2 + CLEAR }
  const floor = Math.max(...items.map((it) => hubRadius(it, hub)))
  // Lay out once from the top, then again from where each group sits as near its own angle as the others allow.
  // The ellipse is not round, so turning a laid-out ring would change its gaps.
  const first = solve(items, floor, 0)
  const wrap = (v: number) => Math.atan2(Math.sin(v), Math.cos(v))
  const turn = gs.reduce((sum, g, i) => {
    const own = first.t.filter((_, k) => items[k]!.g === i)
    return sum + wrap((g.at * Math.PI) / 180 - (own[0]! + own.at(-1)!) / 2)
  }, 0) / gs.length
  const { r, t } = solve(items, floor, turn)
  items.forEach((it, i) => {
    const c = ring(t[i]!, r)
    pos[it.id] = { x: Math.round(c.x - it.w / 2), y: Math.round(c.y - it.h / 2) }
  })
  return pos
}

/** One column: the agent, then each card on a spine down its left. */
export function layoutNarrow(agent: string, ids: string[], size: (id: string) => Size): Record<string, Pt> {
  const pos: Record<string, Pt> = { [agent]: { x: SPINE, y: 0 } }
  let y = size(agent).h + GAP_Y
  for (const id of ids) { pos[id] = { x: SPINE, y }; y += size(id).h + GAP_Y }
  return pos
}

/** Where the line from a box's centre toward `p` leaves the box. */
function exit(b: Box, p: Pt): Pt {
  const c = mid(b), dx = p.x - c.x, dy = p.y - c.y
  const k = 1 / Math.max(Math.abs(dx) / (b.w / 2), Math.abs(dy) / (b.h / 2), 1e-9)
  return { x: c.x + dx * k, y: c.y + dy * k }
}

/** A straight line from the agent to a card, border to border. */
export const spoke = (agent: Box, b: Box): Pt[] => [exit(agent, mid(b)), exit(b, mid(agent))]

/** The narrow column's line: out of the agent's left side, down the spine, into the card's header. */
export function spine(agent: Box, b: Box): Pt[] {
  const x = agent.x - SPINE, y = agent.y + agent.h / 2
  return [{ x: agent.x, y }, { x, y }, { x, y: b.y + HEAD }, { x: b.x, y: b.y + HEAD }]
}

/**
 * A skill's line to the connection it calls: out of the skill's row on the side facing the connection, into
 * the connection, bent round the agent when the straight way crosses it. Points to draw a smooth curve through.
 */
export function depLine(from: Box, rowY: number, to: Box, agent: Box, i: number): Pt[] {
  const c = mid(agent), lane = 28 + (i % 5) * 10
  const rx = (agent.w / 2) * Math.SQRT2 + lane, ry = (agent.h / 2) * Math.SQRT2 + lane
  const reach = (p: Pt) => Math.hypot((p.x - c.x) / rx, (p.y - c.y) / ry)
  const lerp = (a: Pt, b: Pt, k: number) => ({ x: a.x + (b.x - a.x) * k, y: a.y + (b.y - a.y) * k })
  const bend = (a: Pt, b: Pt, depth: number): Pt[] => {
    if (depth > 3 || ![0.2, 0.4, 0.6, 0.8].some((k) => reach(lerp(a, b, k)) < 1)) return []
    // Push the middle out to the track round the agent; straight through its centre, go round one side.
    let m = lerp(a, b, 0.5)
    if (reach(m) < 0.05) m = { x: c.x - (b.y - a.y), y: c.y + (b.x - a.x) }
    const k = 1 / reach(m), p = { x: c.x + (m.x - c.x) * k, y: c.y + (m.y - c.y) * k }
    return [...bend(a, p, depth + 1), p, ...bend(p, b, depth + 1)]
  }
  const left = mid(to).x < mid(from).x
  const y = from.y + Math.min(rowY, from.h - 16)
  const s = { x: left ? from.x : from.x + from.w, y }
  const out = { x: s.x + (left ? -24 : 24), y }
  const end = exit(to, out)
  return [s, out, ...bend(out, end, 0), end]
}
