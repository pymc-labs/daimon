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

/** The schedule in words. */
export function cronWords(cron: string): string {
  const s = fromCron(cron)
  const [h, m] = s.time.split(":")
  switch (s.kind) {
    case "hourly": return `Every hour at minute ${Number(m)}`
    case "daily": return `Every day at ${s.time}`
    case "weekdays": return `Every weekday at ${s.time}`
    case "weekly": return `Every ${WEEKDAYS[s.weekday]} at ${s.time}`
    case "monthly": return `On day ${s.day} of every month at ${s.time}`
    case "custom": return h ? "A custom schedule" : "A custom schedule"
  }
}

// ---------- Next fire times ----------

/** Values a cron field allows: `*`, numbers, lists, ranges and steps. Null when it cannot be read. */
function field(f: string, lo: number, hi: number): Set<number> | null {
  const out = new Set<number>()
  for (const part of f.split(",")) {
    const m = part.match(/^(\*|\d+(?:-\d+)?)(?:\/(\d+))?$/)
    if (!m) return null
    const [a, b] = m[1] === "*" ? [lo, hi] : m[1]!.split("-").map(Number) as [number, number | undefined]
    const end = b ?? (m[2] ? hi : a)
    if (a < lo || end > hi || a > end) return null
    for (let v = a; v <= end; v += Number(m[2] ?? 1)) out.add(v)
  }
  return out
}

/** Milliseconds the zone is ahead of UTC at an instant. */
function offset(ts: number, zone: string): number {
  const p = Object.fromEntries(new Intl.DateTimeFormat("en-US", {
    timeZone: zone, hourCycle: "h23", year: "numeric", month: "numeric", day: "numeric", hour: "numeric", minute: "numeric", second: "numeric",
  }).formatToParts(new Date(ts)).map((x) => [x.type, Number(x.value)]))
  return Date.UTC(p.year!, p.month! - 1, p.day!, p.hour!, p.minute!, p.second!) - Math.floor(ts / 1000) * 1000
}

/** The next `count` times a cron expression fires in a time zone, as ISO strings; empty when it cannot be read. */
export function nextFires(cron: string, zone: string, from: number, count = 3): string[] {
  const f = cron.trim().split(/\s+/)
  if (f.length !== 5) return []
  const sets = [field(f[0]!, 0, 59), field(f[1]!, 0, 23), field(f[2]!, 1, 31), field(f[3]!, 1, 12), field(f[4]!.replace(/\b7\b/g, "0"), 0, 6)]
  if (sets.some((s) => !s)) return []
  const [mins, hours, doms, mons, dows] = sets as Set<number>[]
  const domAny = f[2] === "*", dowAny = f[4] === "*"
  const out: string[] = []
  const start = new Date(from + offset(from, zone))
  for (let d = 0; d < 800 && out.length < count; d++) {
    const day = new Date(Date.UTC(start.getUTCFullYear(), start.getUTCMonth(), start.getUTCDate() + d))
    const domOk = doms!.has(day.getUTCDate()), dowOk = dows!.has(day.getUTCDay())
    // Cron fires when either day field matches if both are restricted.
    const dayOk = domAny || dowAny ? domOk && dowOk : domOk || dowOk
    if (!mons!.has(day.getUTCMonth() + 1) || !dayOk) continue
    for (const h of [...hours!].sort((a, b) => a - b)) for (const m of [...mins!].sort((a, b) => a - b)) {
      const guess = Date.UTC(day.getUTCFullYear(), day.getUTCMonth(), day.getUTCDate(), h, m)
      const at = guess - offset(guess - offset(guess, zone), zone)
      if (at > from && out.length < count) out.push(new Date(at).toISOString())
    }
  }
  return out
}
