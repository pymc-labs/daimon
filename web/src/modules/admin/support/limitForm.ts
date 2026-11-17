import type { Problem } from "@/lib/api"
import { day, endOf, lastDay, startOf } from "./lib"
import type { LimitValue, Window } from "./types"

/** A limit as the form holds it: text for each input. */
export type LimitForm = { shared: string; each: string; window: Window; from: string; last: string }

export const emptyForm = (): LimitForm => ({ shared: "", each: "", window: "monthly", from: "", last: "" })
export const formOf = (v: LimitValue): LimitForm =>
  ({ shared: v.shared ?? "", each: v.each_person ?? "", window: v.window, from: day(v.starts_at), last: lastDay(v.ends_at) })

/** What a save sends; a date left alone keeps the instant as read (`was`), a new last day sends the day after. */
export const toValue = (f: LimitForm, was: LimitValue | null, person: boolean): LimitValue => ({
  shared: person ? null : f.shared.trim() || null,
  each_person: f.each.trim() || null,
  window: f.window,
  starts_at: startOf(f.from, was?.starts_at ?? null),
  ends_at: endOf(f.last, was?.ends_at ?? null),
})

export type LimitErrors = { at: Record<string, string>; rest: string | null }

/** A refusal as the message for each input it names (`limit.<field>`), and what names none. */
export function limitErrors(error: string | null, problems: Problem[]): LimitErrors {
  const at: Record<string, string> = {}
  if (!problems.length) return { at, rest: error }
  const rest: string[] = []
  for (const { loc, msg } of problems) {
    const key = String(loc[1] ?? "")
    if (["shared", "each_person", "starts_at", "ends_at", "window"].includes(key)) at[key] = at[key] ? `${at[key]}. ${msg}` : msg
    else rest.push(msg)
  }
  return { at, rest: rest.length ? rest.join(". ") : null }
}
