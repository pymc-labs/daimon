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
// Spend under half a cent would round to $0.00 and read as unused.
export const usd = (n: number | null | undefined) => (n == null ? "–" : n > 0 && n < 0.005 ? "<$0.01" : money.format(n))

export type ChannelKind = "channel" | "category" | "thread" | "forum_post" | "team"

const UNNAMED_CHANNEL = "Unnamed channel"
// Labels the server sends for a channel with no name of its own; they never get a "#".
const PLAIN_CHANNEL_LABELS = new Set([UNNAMED_CHANNEL, "Deleted channel", "Direct message"])
// Threads, forum posts, categories and teams are named as they are; only a channel gets a "#".
const HASHLESS = new Set<string>(["thread", "forum_post", "category", "team"])
/** A channel as people write it; a channel the platform never named stays unnamed. */
export const channelLabel = (name: string | null | undefined, kind: string | null = "channel") =>
  !name ? UNNAMED_CHANNEL : PLAIN_CHANNEL_LABELS.has(name) || (kind && HASHLESS.has(kind)) ? name : `#${name}`

/** What a cross-reference reads as when its target has no name left. */
export const ENTITY_FALLBACK = { agent: "Deleted agent", channel: "Deleted channel", person: "Unknown person" } as const

const upper = new Set(["gpt", "ai", "llm"])
const titled = (w: string) => (upper.has(w) ? w.toUpperCase() : w.charAt(0).toUpperCase() + w.slice(1))

/** A model id as people say it: "gpt-5" is "GPT-5", "claude-sonnet-4-5" is "Claude Sonnet 4.5". A known `label` wins. */
export function modelLabel(id: string, label?: string | null) {
  if (label) return label
  const parts: string[] = []
  for (const w of id.replace(/-\d{8}$/, "").split(/[-_]/).filter(Boolean)) {
    const last = parts.length - 1
    if (/^\d+$/.test(w) && last >= 0 && /^\d+(\.\d+)*$/.test(parts[last]!)) parts[last] += `.${w}`
    else parts.push(w)
  }
  const [first, second, ...rest] = parts
  if (first && upper.has(first) && second && /^\d/.test(second)) return [`${first.toUpperCase()}-${second}`, ...rest.map(titled)].join(" ")
  return parts.map(titled).join(" ")
}

const humanise = (key: string) => { const w = key.replaceAll(/[_-]+/g, " ").trim(); return w.charAt(0).toUpperCase() + w.slice(1) }

/** A tool as people read it: server and tool, "Search: Web search", never `search/web_search`. */
export const toolLabel = (server: string, tool: string) => `${humanise(server)}: ${humanise(tool)}`
