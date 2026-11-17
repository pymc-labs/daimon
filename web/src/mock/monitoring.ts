// Monitoring: kept conversations and their transcripts for two agents, built the same way on every load.
import type { MonitoredConversation, MonitoredEvent, MonitoredWhere } from "@/modules/admin/types"

const hoursAgo = (h: number) => new Date(Date.now() - h * 3_600_000).toISOString()

type Turn = { q: string; tools: [string, MonitoredEvent["outcome"]][]; a: string }
const PRICING: Turn[] = [
  { q: "What did we charge Brightwave last quarter?", tools: [["hubspot_search_crm", "completed"], ["spend_cost_report", "completed"]], a: "Brightwave was billed $18,400 for the quarter, up 6% on the one before." },
  { q: "Can you compare that with the Tidewater contract?", tools: [["hubspot_search_crm", "completed"]], a: "Tidewater paid $22,100 over the same quarter. Their usage tier is higher, which explains most of the gap." },
  { q: "Draft a note proposing a 5% discount for annual billing.", tools: [], a: "Here is a short note. It offers 5% off for annual billing, names the current plan, and asks for a reply by Friday." },
  { q: "Send it to Dana.", tools: [["gws_run", "denied"]], a: "I can't send mail from this agent, so I left the note as a draft for you to copy." },
]
const MMM: Turn[] = [
  { q: "Which channel had the best return in the last model run?", tools: [["ssr_runs", "completed"]], a: "Paid search returned the most per dollar, at 3.1 against 1.8 for display." },
  { q: "Refit with the new April spend.", tools: [["fly_task_status", "error"], ["fly_task_status", "completed"]], a: "The refit finished on the second try. Paid search still leads, and the interval is a little narrower." },
  { q: "Share the summary with Tomás.", tools: [["notion_get_page", "cancelled"]], a: "I stopped before posting. Tell me where it should go and I will send it." },
]

type Row = { id: string; agent: string; person: MonitoredConversation["person"]; where: MonitoredWhere; turns: Turn[]; at: number; span: number }
const ROWS: Row[] = [
  { id: "mc_1", agent: "ag_pricing", person: { id: "p_owen", name: "Owen Brooks" }, where: "direct_messages", turns: PRICING.slice(0, 4), at: 2, span: 1.5 },
  { id: "mc_2", agent: "ag_pricing", person: { id: "p_ines", name: "Inês Duarte" }, where: "web", turns: PRICING.slice(0, 2), at: 26, span: 0.4 },
  { id: "mc_3", agent: "ag_pricing", person: { id: "p_priya", name: "Priya Raman" }, where: "mcp", turns: PRICING.slice(1, 2), at: 31, span: 2 },
  { id: "mc_4", agent: "ag_pricing", person: { id: "p_jordan", name: "Jordan Ellis" }, where: "web", turns: PRICING.slice(2, 4), at: 52, span: 0.3 },
  { id: "mc_5", agent: "ag_pricing", person: { id: null, name: "Sam Ortiz" }, where: "direct_messages", turns: PRICING.slice(0, 1), at: 75, span: 0.1 },
  { id: "mc_6", agent: "ag_pricing", person: { id: "p_owen", name: "Owen Brooks" }, where: "mcp", turns: PRICING.slice(0, 3), at: 120, span: 5 },
  { id: "mc_7", agent: "ag_mmm", person: { id: "p_aiko", name: "Aiko Tanaka" }, where: "direct_messages", turns: MMM, at: 5, span: 1 },
  { id: "mc_8", agent: "ag_mmm", person: { id: "p_tomas", name: "Tomás Okafor" }, where: "direct_messages", turns: MMM.slice(0, 2), at: 29, span: 0.6 },
  { id: "mc_9", agent: "ag_mmm", person: { id: "p_rafael", name: "Rafael Costa" }, where: "web", turns: MMM.slice(1, 3), at: 99, span: 0.8 },
]

const toConversation = (r: Row): MonitoredConversation => ({
  id: r.id, person: r.person, where: r.where,
  questions: r.turns.length, tools: r.turns.reduce((n, t) => n + t.tools.length, 0),
  first_at: hoursAgo(r.at + r.span), last_at: hoursAgo(r.at),
})

export const conversationsOf = (agent: string) => ROWS.filter((r) => r.agent === agent).map(toConversation)

export function transcriptOf(agent: string, id: string): MonitoredEvent[] | null {
  const r = ROWS.find((x) => x.agent === agent && x.id === id)
  if (!r) return null
  const out: MonitoredEvent[] = []
  const step = r.span / (r.turns.length * 4)
  let h = r.at + r.span
  const at = () => hoursAgo((h -= step))
  r.turns.forEach((t, i) => {
    const e = (kind: MonitoredEvent["kind"], fields: Partial<MonitoredEvent>): MonitoredEvent =>
      ({ id: `${id}_${i}_${out.length}`, at: at(), kind, content: null, tool: null, outcome: null, ...fields })
    out.push(e("question", { content: t.q }))
    for (const [tool, outcome] of t.tools) out.push(e("tool", { tool, outcome }))
    out.push(e("answer", { content: t.a }))
  })
  return out
}
