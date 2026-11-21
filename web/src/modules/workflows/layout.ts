// Top-to-bottom layered layout with dagre. Loop edges stay out of the ranking and are drawn as side arcs.
import { Graph, layout as dagre } from "@dagrejs/dagre"

import { CARD_H } from "./cards"
import { allEdges, backEdges, edgeKey, type FlowEdge } from "./spec"
import type { Spec } from "./types"

export const NODE_W = 248
export const TRIGGERS = "__triggers"
const CHIP_H = 30
const LABEL_H = 20

export type Pt = { x: number; y: number }
export type Box = { id: string; x: number; y: number; w: number; h: number }
export type LaidEdge = FlowEdge & { id: string; back: boolean; points: Pt[]; labelAt: Pt | null; plusAt: Pt }
export type Layout = { nodes: Box[]; edges: LaidEdge[]; leaves: { id: string; at: Pt }[]; width: number; height: number }

/** Width of a trigger chip row, or of an edge label, in pixels at 11-12px text. */
export const textW = (s: string, px = 6.4) => Math.ceil(s.length * px)
export const chipsW = (labels: string[]) => labels.reduce((w, l) => w + textW(l, 6.8) + 36, 0) + (labels.length - 1) * 6

/** `heights` gives each card its real height (cards with badges are taller); the rest are CARD_H. */
export function layout(spec: Spec, chips: string[] = [], heights: Record<string, number> = {}): Layout {
  const g = new Graph({ multigraph: true })
  g.setGraph({ rankdir: "TB", nodesep: 40, ranksep: 56, edgesep: 24, marginx: 0, marginy: 0 })
  g.setDefaultEdgeLabel(() => ({}))
  const ids = Object.keys(spec.nodes)
  for (const id of ids) g.setNode(id, { width: NODE_W, height: heights[id] ?? CARD_H })
  const hasChips = chips.length > 0 && !!spec.nodes[spec.start]
  if (hasChips) {
    g.setNode(TRIGGERS, { width: chipsW(chips), height: CHIP_H })
    g.setEdge(TRIGGERS, spec.start, { minlen: 1 }, "start")
  }
  const back = backEdges(spec)
  const edges = allEdges(spec).filter((e) => spec.nodes[e.to])
  for (const e of edges) {
    if (back.has(edgeKey(e))) continue
    g.setEdge(e.from, e.to, e.label ? { width: textW(e.label) + 16, height: LABEL_H, labelpos: "c" } : {}, edgeKey(e))
  }
  dagre(g)

  const box = (id: string): Box => {
    const n = g.node(id)
    return { id, x: n.x - n.width / 2, y: n.y - n.height / 2, w: n.width, h: n.height }
  }
  const nodes = [...ids, ...(hasChips ? [TRIGGERS] : [])].map(box)
  const at = Object.fromEntries(nodes.map((b) => [b.id, b]))
  const bottom = (b: Box) => ({ x: b.x + b.w / 2, y: b.y + b.h })
  const top = (b: Box) => ({ x: b.x + b.w / 2, y: b.y })

  const laid: LaidEdge[] = []
  if (hasChips) {
    const s = at[TRIGGERS]!, t = at[spec.start]!
    laid.push(forward({ from: TRIGGERS, to: spec.start, kind: "next", slot: "start" }, [bottom(s), top(t)], null))
  }
  const arcs: number[] = []
  for (const e of edges) {
    const s = at[e.from]!, t = at[e.to]!
    if (back.has(edgeKey(e))) {
      // A side arc right of everything between the two nodes; stacked loops step further out.
      const y0 = t.y, y1 = s.y + s.h
      const right = Math.max(...nodes.filter((b) => b.y < y1 && b.y + b.h > y0).map((b) => b.x + b.w))
      const x = right + 36 + arcs.filter((a) => a >= right).length * 28
      arcs.push(x)
      const a = { x: s.x + s.w, y: s.y + s.h / 2 }, b = { x: t.x + t.w, y: t.y + t.h / 2 }
      const points = [a, { x, y: a.y }, { x, y: b.y }, b]
      laid.push({ ...e, id: edgeKey(e), back: true, points, labelAt: { x, y: (a.y + b.y) / 2 }, plusAt: { x, y: (a.y + b.y) / 2 } })
      continue
    }
    const d = g.edge({ v: e.from, w: e.to, name: edgeKey(e) }) as { points?: Pt[]; x?: number; y?: number }
    const mid = (d.points ?? []).slice(1, -1)
    const lane = mid.length ? mid[Math.floor(mid.length / 2)]!.x : null
    laid.push(forward(e, orthogonal(bottom(s), top(t), lane), e.label && d.x != null ? { x: lane ?? d.x, y: d.y! } : null))
  }
  const out = new Set(edges.map((e) => e.from))
  const leaves = ids.filter((id) => !out.has(id) && spec.nodes[id]!.kind !== "finish").map((id) => ({ id, at: { x: bottom(at[id]!).x, y: bottom(at[id]!).y + 24 } }))
  const width = Math.max(0, ...nodes.map((b) => b.x + b.w), ...arcs.map((x) => x + 40))
  const height = Math.max(0, ...nodes.map((b) => b.y + b.h)) + (leaves.length ? 48 : 0)
  return { nodes, edges: laid, leaves, width, height }
}

function forward(e: FlowEdge, points: Pt[], labelAt: Pt | null): LaidEdge {
  return { ...e, id: edgeKey(e), back: false, points, labelAt, plusAt: labelAt ? along(points, 0.82) : along(points, 0.5) }
}

/** Down from the source, across to the lane (if any), down, across to the target, down: flowchart elbows. */
export function orthogonal(a: Pt, b: Pt, lane: number | null, stub = 18): Pt[] {
  const y1 = a.y + stub, y2 = b.y - stub
  const pts = lane == null || Math.abs(lane - a.x) < 1 && Math.abs(lane - b.x) < 1
    ? [a, { x: a.x, y: (a.y + b.y) / 2 }, { x: b.x, y: (a.y + b.y) / 2 }, b]
    : [a, { x: a.x, y: y1 }, { x: lane, y: y1 }, { x: lane, y: y2 }, { x: b.x, y: y2 }, b]
  return pts.filter((p, i) => i === 0 || Math.hypot(p.x - pts[i - 1]!.x, p.y - pts[i - 1]!.y) > 0.5)
    .filter((p, i, xs) => i === 0 || i === xs.length - 1 || !collinear(xs[i - 1]!, p, xs[i + 1]!))
}
const collinear = (a: Pt, b: Pt, c: Pt) => Math.abs((b.x - a.x) * (c.y - a.y) - (b.y - a.y) * (c.x - a.x)) < 0.5

/** The point a fraction `f` of the way along a polyline. */
export function along(pts: Pt[], f: number): Pt {
  const seg = pts.slice(1).map((p, i) => Math.hypot(p.x - pts[i]!.x, p.y - pts[i]!.y))
  let left = seg.reduce((a, b) => a + b, 0) * f
  for (let i = 0; i < seg.length; i++) {
    if (left <= seg[i]! || i === seg.length - 1) {
      const t = seg[i] ? Math.min(1, left / seg[i]!) : 0
      const a = pts[i]!, b = pts[i + 1]!
      return { x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t }
    }
    left -= seg[i]!
  }
  return pts[0]!
}

/** A smooth path through the points: straight runs joined by rounded corners. */
export function pathOf(pts: Pt[], r = 14) {
  if (pts.length < 3) return `M${pts[0]!.x},${pts[0]!.y} L${pts.at(-1)!.x},${pts.at(-1)!.y}`
  let d = `M${pts[0]!.x},${pts[0]!.y}`
  for (let i = 1; i < pts.length - 1; i++) {
    const p = pts[i - 1]!, c = pts[i]!, n = pts[i + 1]!
    const k1 = Math.min(r, Math.hypot(c.x - p.x, c.y - p.y) / 2), k2 = Math.min(r, Math.hypot(n.x - c.x, n.y - c.y) / 2)
    const u = unit(p, c), v = unit(c, n)
    d += ` L${c.x - u.x * k1},${c.y - u.y * k1} Q${c.x},${c.y} ${c.x + v.x * k2},${c.y + v.y * k2}`
  }
  const l = pts.at(-1)!
  return `${d} L${l.x},${l.y}`
}
const unit = (a: Pt, b: Pt) => {
  const len = Math.hypot(b.x - a.x, b.y - a.y) || 1
  return { x: (b.x - a.x) / len, y: (b.y - a.y) / len }
}
