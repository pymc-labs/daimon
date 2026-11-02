import type { AgentRef } from "@/lib/types"

type SubjectKind = "person" | "group" | "invite"
export type Subject = { kind: SubjectKind; id: string; name: string }
export type Window = "month" | "total"

export type Allowance = {
  id: string
  subject: Subject
  agent: AgentRef | null
  amount_usd: number
  used_usd: number
  /** Group allowances apply to each member. */
  members?: number
  window: Window
  expires_at: string | null
}

export type LedgerEntry = { id: string; at: string; kind: "topup" | "promo" | "adjustment"; amount_usd: number; by: string; note: string }

export type Overview = {
  balance_usd: number
  spent_month_usd: number
  projected_month_usd: number
  turns_month: number
  daily: { day: string; usd: number }[]
  ledger: LedgerEntry[]
  /** How admins add credit: card checkout, a recorded grant, or not at all. */
  topup: "checkout" | "manual" | null
}

export type SpendRow = { id: string; name: string; avatar?: string; kind?: string; turns: number; usd: number }
export type SpendBy = "person" | "agent" | "channel"

export type Turn = { id: string; at: string; agent: AgentRef; title: string; platform: string; usd: number }
export type Mine = { by_agent: SpendRow[]; turns: Turn[]; spent_usd: number; turn_count: number }
