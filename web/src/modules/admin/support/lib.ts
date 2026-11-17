import { cleanKey } from "./menuDraft"
import type { Limit, LimitValue, Place, Scope } from "./types"

export const SCOPES: Record<Scope, string> = { workspace: "Workspace", agent: "Agent", channel: "Channel" }

export const day = (iso: string | null) => (iso ? iso.slice(0, 10) : "")
const DAY_MS = 86_400_000
/** The end is exclusive: the last day is the date of the instant just before it. */
export const lastDay = (iso: string | null) => (iso ? new Date(Date.parse(iso) - 1).toISOString().slice(0, 10) : "")
/** A bare date is read as 00:00 UTC, so the exclusive end that includes `d` is the next day. */
export const dayAfter = (d: string) => new Date(Date.parse(`${d}T00:00:00Z`) + DAY_MS).toISOString().slice(0, 10)
/** The start to send: the instant as read when its day is untouched. */
export const startOf = (v: string, was: string | null) => (!v ? null : v === day(was) ? was : v)
/** The end to send: the instant as read when its last day is untouched. */
export const endOf = (v: string, was: string | null) => (!v ? null : v === lastDay(was) ? was : dayAfter(v))

export const keyOf = (l: Pick<Limit, "scope" | "scope_id" | "person_id">) => `${l.scope}|${l.scope_id}|${l.person_id ?? ""}`
export const valueOf = ({ shared, each_person, window, starts_at, ends_at }: LimitValue): LimitValue => ({ shared, each_person, window, starts_at, ends_at })

/** Credits as people read them: `5`, `0.5`. */
export const credits = (n: number) => String(Math.round(n * 100) / 100)

/** Where a limit applies, from workspace down to a channel. */
export const placeName = (l: Pick<Limit, "scope" | "name">) => (l.scope === "workspace" ? "Workspace" : (l.name ?? ""))
export const samePlace = (a: Pick<Place, "scope" | "id">, scope: Scope, id: string) => a.scope === scope && a.id === id

/** The feature keys the Support page owns; the generic feature lists leave them out. */
export const SUPPORT_KEYS = ["support_escalation", "support_feedback", "support_own_channel"]

/** The menu key made from its name; "use" is the path of the places' choice, so it is not one. */
export function menuKey(name: string, taken: string[]): string {
  const base = cleanKey(name, "-").replace(/^-+|-+$/g, "").slice(0, 28) || "menu"
  let key = base === "use" ? "use-menu" : base
  for (let n = 2; taken.includes(key); n++) key = `${base}-${n}`
  return key
}

const MENTION = /^<@!?([^>|]+)(?:\|[^>]*)?>$/
const ID_SHAPE: Record<string, RegExp> = { discord: /^\d{17,20}$/, slack: /^[UW][A-Z0-9]{8,}$/, teams: /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i }
/** The user id a typed id or mention (`<@123>`) names on `platform`, or null when the text is neither. */
export function platformUserId(platform: string, text: string): string | null {
  const t = text.trim()
  const id = (MENTION.exec(t)?.[1] ?? t).trim()
  return ID_SHAPE[platform]?.test(id) ? (platform === "teams" ? id.toLowerCase() : id) : null
}
