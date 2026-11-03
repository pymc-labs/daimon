import { useCallback, useState } from "react"

export type TreeRow<R> = { id: string; parentId: string | null; kind: "group" | "item"; row: R }
export type Flat<R> = { node: TreeRow<R>; depth: number; children: number; open: boolean }

/** Per-row open state; a row without an entry is open when it is a group. */
export type Expanded = Record<string, boolean>
export const isOpen = (node: Pick<TreeRow<unknown>, "id" | "kind">, expanded: Expanded) => expanded[node.id] ?? node.kind === "group"

/**
 * The rows to draw, parents before children, in the order given. A row whose parent is missing is a root.
 * With a `filter`, only rows that match and their ancestors are kept, and every kept ancestor is open.
 */
export function flatten<R>(rows: TreeRow<R>[], expanded: Expanded, filter?: ((row: R) => boolean) | null): Flat<R>[] {
  const ids = new Set(rows.map((r) => r.id))
  const kids = new Map<string | null, TreeRow<R>[]>()
  for (const r of rows) {
    const key = r.parentId !== null && ids.has(r.parentId) && r.parentId !== r.id ? r.parentId : null
    kids.set(key, [...(kids.get(key) ?? []), r])
  }
  // Keep a row when it matches or any descendant does; a matching group keeps everything under it.
  const keep = new Set<string>()
  if (filter) {
    const mark = (r: TreeRow<R>, seen: Set<string>, forced: boolean): boolean => {
      if (seen.has(r.id)) return false
      seen.add(r.id)
      const self = filter(r.row)
      const all = forced || (r.kind === "group" && self)
      let hit = forced || self
      for (const k of kids.get(r.id) ?? []) hit = mark(k, seen, all) || hit
      if (hit) keep.add(r.id)
      return hit
    }
    const seen = new Set<string>()
    for (const r of kids.get(null) ?? []) mark(r, seen, false)
  }
  const out: Flat<R>[] = []
  const walk = (r: TreeRow<R>, depth: number, seen: Set<string>) => {
    if (seen.has(r.id) || (filter && !keep.has(r.id))) return
    seen.add(r.id)
    const own = (kids.get(r.id) ?? []).filter((k) => !filter || keep.has(k.id))
    const open = filter ? own.length > 0 : isOpen(r, expanded)
    out.push({ node: r, depth, children: own.length, open })
    if (open) for (const k of own) walk(k, depth + 1, seen)
  }
  const seen = new Set<string>()
  for (const r of kids.get(null) ?? []) walk(r, 0, seen)
  return out
}

const keyOf = (key: string) => `daimon:tree:${key}`

/** Open state kept in localStorage under `daimon:tree:<key>`; storage that is missing or blocked just does not persist. */
export function useTreeExpanded(key: string) {
  const [expanded, set] = useState<Expanded>(() => {
    try { return JSON.parse(localStorage.getItem(keyOf(key)) ?? "{}") as Expanded } catch { return {} }
  })
  const toggle = useCallback((id: string, open: boolean) => set((prev) => {
    const next = { ...prev, [id]: open }
    try { localStorage.setItem(keyOf(key), JSON.stringify(next)) } catch { /* not persisted */ }
    return next
  }), [key])
  return [expanded, toggle] as const
}
