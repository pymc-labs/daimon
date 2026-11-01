const nf = new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 })
const full = new Intl.NumberFormat("en")
export const compact = (n: number | null | undefined) => (n == null ? "–" : n < 1000 ? String(n) : nf.format(n))
export const exact = (n: number | null | undefined) => (n == null ? "–" : full.format(n))

export function ago(iso?: string | null, now = Date.now()) {
  if (!iso) return "Never"
  const s = Math.round((now - new Date(iso).getTime()) / 1000)
  if (s < 0) return inFuture(-s)
  if (s < 60) return "just now"
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  if (s < 86400 * 30) return `${Math.floor(s / 86400)}d ago`
  return shortDate(iso)
}

function inFuture(s: number) {
  if (s < 3600) return `in ${Math.max(1, Math.floor(s / 60))}m`
  if (s < 86400) return `in ${Math.floor(s / 3600)}h`
  return `in ${Math.floor(s / 86400)}d`
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
/** "7 Sep", day first; avoids en-GB's "Sept". */
export const dayMonth = (d: Date) => `${d.getDate()} ${MONTHS[d.getMonth()]}`

/** Name of the month `back` months before this one, e.g. "October". */
export const monthName = (back = 0, now = new Date()) =>
  new Date(now.getFullYear(), now.getMonth() - back, 1).toLocaleString("en", { month: "long" })

export const absolute = (iso?: string | null, seconds = false) => {
  if (!iso) return ""
  const d = new Date(iso)
  return `${dayMonth(d)} ${d.getFullYear()}, ${seconds ? clock(d) : timeOnly(iso)}`
}

const clock = (d: Date) => d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit" })

export function shortDate(iso?: string | null) {
  if (!iso) return ""
  const d = new Date(iso)
  return d.getFullYear() === new Date().getFullYear() ? dayMonth(d) : `${dayMonth(d)} ${d.getFullYear()}`
}

const timeOnly = (iso: string) =>
  new Date(iso).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })

export const initials = (name: string) =>
  name.split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0]!.toUpperCase()).join("")

export const plural = (n: number, word: string, many = `${word}s`) => `${exact(n)} ${n === 1 ? word : many}`

/** "850 ms", "12.3 s", "4m 05s", "1h 02m". */
export const duration = (ms: number) => {
  if (ms < 1000) return `${Math.round(ms)} ms`
  const s = ms / 1000
  if (s < 60) return `${s.toFixed(1)} s`
  const m = Math.floor(s / 60)
  return m < 60 ? `${m}m ${String(Math.floor(s % 60)).padStart(2, "0")}s` : `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`
}

const money = new Intl.NumberFormat("en", { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 2 })
export const usd = (n: number | null | undefined) => (n == null ? "–" : money.format(n))

const UNNAMED_CHANNEL = "Unnamed channel"
/** A channel as people write it; a channel the platform never named stays unnamed. */
export const channelLabel = (name: string | null | undefined) => (!name || name === UNNAMED_CHANNEL ? UNNAMED_CHANNEL : `#${name}`)
