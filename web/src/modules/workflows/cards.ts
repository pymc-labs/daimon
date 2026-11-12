// What a step card shows besides its title: badges, and the card height they need.
import { problemsAt, toolWrites } from "./spec"
import type { Node, Validation } from "./types"

export type BadgeKind = "problems" | "writes" | "grant" | "untrusted" | "waiver"
export type CardBadge = { kind: BadgeKind; count?: number }

export const CARD_H = 64
export const CARD_H_BADGES = 90
/** Badges past this many fold into a "+N" badge, so the row stays on one line. */
export const MAX_BADGES = 3

const hasWrites = (n: Node) => (n.kind === "action" && toolWrites(n.tool)) || (n.kind === "agent" && !!n.writes && n.writes !== "refuse")

/**
 * Problems first, then writes, grant and the untrusted shield. The shield only marks a step where untrusted data
 * reaches a write (Rule of Two), so it does not sit on every step of a webhook workflow.
 */
export function badgesOf(id: string, n: Node, validation?: Validation): CardBadge[] {
  const out: CardBadge[] = []
  const problems = problemsAt(validation, id).length
  if (problems) out.push({ kind: "problems", count: problems })
  const writes = hasWrites(n)
  if (writes) out.push({ kind: "writes" })
  if (validation?.grants.some((g) => g.node === id)) out.push({ kind: "grant" })
  if (n.untrusted_waiver) out.push({ kind: "waiver" })
  else if (writes && validation?.classes[id]?.includes("U")) out.push({ kind: "untrusted" })
  return out
}

export const cardHeight = (badges: CardBadge[]) => (badges.length ? CARD_H_BADGES : CARD_H)
