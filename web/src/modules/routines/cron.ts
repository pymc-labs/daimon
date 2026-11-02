export type Kind = "hourly" | "daily" | "weekdays" | "weekly" | "monthly" | "custom"

/** A schedule as the picker shows it. `time` is HH:MM; for hourly only the minutes count. */
export type Spec = { kind: Kind; time: string; weekday: number; day: number; cron: string }

export const KINDS: { value: Kind; label: string }[] = [
  { value: "hourly", label: "Every hour" },
  { value: "daily", label: "Every day" },
  { value: "weekdays", label: "Weekdays" },
  { value: "weekly", label: "Every week" },
  { value: "monthly", label: "Every month" },
  { value: "custom", label: "Custom" },
]
export const WEEKDAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]

const pad = (n: number) => String(n).padStart(2, "0")
const split = (time: string) => {
  const [h = "9", m = "0"] = time.split(":")
  return { h: Number(h) || 0, m: Number(m) || 0 }
}

export const DEFAULT_SPEC: Spec = { kind: "daily", time: "09:00", weekday: 1, day: 1, cron: "0 9 * * *" }

/** The cron expression for a picked schedule. */
export function toCron(s: Spec): string {
  const { h, m } = split(s.time)
  switch (s.kind) {
    case "hourly": return `${m} * * * *`
    case "daily": return `${m} ${h} * * *`
    case "weekdays": return `${m} ${h} * * 1-5`
    case "weekly": return `${m} ${h} * * ${s.weekday}`
    case "monthly": return `${m} ${h} ${s.day} * *`
    case "custom": return s.cron.trim()
  }
}

const num = /^\d+$/
/** The picker state for a cron expression; anything the picker cannot draw is custom. */
export function fromCron(cron: string): Spec {
  const custom: Spec = { ...DEFAULT_SPEC, kind: "custom", cron }
  const f = cron.trim().split(/\s+/)
  if (f.length !== 5) return custom
  const [m, h, dom, mon, dow] = f as [string, string, string, string, string]
  if (!num.test(m) || Number(m) > 59 || mon !== "*") return custom
  const base = { ...DEFAULT_SPEC, cron }
  if (h === "*" && dom === "*" && dow === "*") return { ...base, kind: "hourly", time: `00:${pad(Number(m))}` }
  if (!num.test(h) || Number(h) > 23) return custom
  const time = `${pad(Number(h))}:${pad(Number(m))}`
  if (dom === "*" && dow === "*") return { ...base, kind: "daily", time }
  if (dom === "*" && dow === "1-5") return { ...base, kind: "weekdays", time }
  if (dom === "*" && num.test(dow) && Number(dow) <= 6) return { ...base, kind: "weekly", time, weekday: Number(dow) }
  if (dow === "*" && num.test(dom) && Number(dom) >= 1 && Number(dom) <= 31) return { ...base, kind: "monthly", time, day: Number(dom) }
  return custom
}
