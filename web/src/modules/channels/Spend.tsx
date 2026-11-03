import { useMemo, useState } from "react"
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts"

import { dayMonth, usd } from "@/lib/format"
import { fillDays, spendLines, type SpendLine } from "./lib"
import type { ChannelSettings } from "./types"

const tick = { fontSize: 11, fill: "var(--muted-foreground)" }

function Chart({ spend }: { spend: NonNullable<ChannelSettings["spend"]> }) {
  const [now] = useState(() => Date.now())
  const rows = useMemo(
    () => fillDays(spend.by_day, spend.days, now).map((d) => ({ ...d, label: dayMonth(new Date(`${d.day}T00:00:00`)) })),
    [spend, now],
  )
  const total = rows.reduce((t, r) => t + r.usd, 0)
  return (
    <div role="img" aria-label={`Spend by day over ${spend.days} days, ${usd(total)} in total`} className="h-36 rounded-lg border px-1 pt-3 pb-1">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={rows} barCategoryGap={2} margin={{ top: 4, right: 12, bottom: 0, left: 0 }}>
          <CartesianGrid vertical={false} stroke="var(--border)" />
          <XAxis dataKey="label" tickLine={false} axisLine={false} interval={6} tick={tick} />
          <YAxis tickLine={false} axisLine={false} width={44} tickCount={3} domain={[0, "auto"]} tickFormatter={(v: number) => `$${+v.toFixed(2)}`} tick={tick} />
          <Tooltip
            cursor={{ fill: "var(--accent)" }}
            content={({ active, payload }) => active && payload?.[0] ? (
              <div className="rounded-md border bg-popover px-2.5 py-1.5 text-xs shadow-md">
                <div className="text-muted-foreground">{payload[0].payload.label}</div>
                <div className="font-medium tabular">{usd(payload[0].value as number)}</div>
              </div>
            ) : null}
          />
          <Bar dataKey="usd" fill="var(--chart-1)" radius={[4, 4, 0, 0]} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}

function Lines({ title, lines }: { title: string; lines: SpendLine[] }) {
  if (!lines.length) return null
  return (
    <div>
      <h4 className="mb-1.5 text-xs font-medium text-muted-foreground">{title}</h4>
      <ul className="space-y-2 text-sm">
        {lines.map((l) => (
          <li key={l.key}>
            <div className="flex items-baseline justify-between gap-3">
              <span className="min-w-0 truncate">{l.label}</span>
              <span className="shrink-0 tabular">{usd(l.usd)}</span>
            </div>
            <div className="mt-1 h-1 overflow-hidden rounded-full bg-foreground/10">
              <div className="h-full rounded-full bg-chart-1" style={{ width: `${Math.max(2, Math.round(l.share * 100))}%` }} />
            </div>
          </li>
        ))}
      </ul>
    </div>
  )
}

/** Spend over the last days, by day, by agent and by person; nothing while there is none. */
export function SpendBreakdown({ spend }: { spend: NonNullable<ChannelSettings["spend"]> }) {
  if (!spend.by_day.some((d) => d.usd > 0)) return null
  return (
    <div className="mt-4 space-y-4">
      <div>
        <h4 className="mb-1.5 text-xs font-medium text-muted-foreground">By day</h4>
        <Chart spend={spend} />
      </div>
      <Lines title="By agent" lines={spendLines(spend.by_agent, "No agent")} />
      <Lines title="By person" lines={spendLines(spend.by_person, "No person")} />
    </div>
  )
}
