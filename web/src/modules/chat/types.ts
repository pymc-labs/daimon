// Chat module shapes: the JSON the mock `/api/chat` serves.
import type { ChatSession } from "@/lib/types"

export type Step = {
  id: string
  label: string
  detail?: string
  status: "running" | "done" | "error"
  ms?: number
}

export type FileOut = { id: string; name: string; size: number; kind: "csv" | "xlsx" | "pdf" | "png" }

export type Card =
  | { id: string; kind: "approval"; state: "pending" | "approved" | "always" | "denied"; title: string; consequence?: string; details: string[]; footer?: string; always_allowed: boolean }
  | { id: string; kind: "credential"; state: "pending" | "saved" | "skipped"; headline: string; fact: string; key: string }
  | { id: string; kind: "refusal"; title: string; body: string; help?: string }

export type CardAnswer = { answer: "approve" | "always" | "deny" | "save" | "skip"; value?: string }

export type Msg = {
  id: string
  role: "user" | "assistant"
  content: string
  created_at: string
  steps?: Step[]
  files?: FileOut[]
  attachments?: { name: string; size: number }[]
  card?: Card
  cost_usd?: number
  ms?: number
  feedback?: "up" | "down" | null
  stopped?: boolean
  streaming?: boolean
}

/**
 * The newest messages, oldest first. `next` is the `before` of the page of earlier ones, absent when none.
 * While a turn runs, the last message is its live reply as of event `resume_from`; re-attach from there.
 */
export type SessionDetail = { session: ChatSession; messages: Msg[]; next?: number; resume_from?: string }

export type StreamEvent =
  | { type: "step"; step: Step }
  | { type: "delta"; text: string }
  | { type: "text"; text: string }
  | { type: "card"; card: Card }
  | { type: "done"; message: Msg }
  | { type: "settled"; message: Msg | null }
