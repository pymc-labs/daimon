import { useQuery } from "@tanstack/react-query"
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts"
import { Money, StatStrip } from "@/components/common"
import { QueryState } from "@/components/QueryState"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { api } from "@/lib/api"
import { dayMonth, monthName, shortDate, usd } from "@/lib/format"
import type { Overview } from "./types"
import { Section } from "./shared"

export function OverviewTab() {
  const q = useQuery({ queryKey: ["credits", "overview"], queryFn: () => api.get<Overview>("/api/credits/overview") })
  return <QueryState query={q}>{(data) => <OverviewBody data={data} />}</QueryState>
}

function OverviewBody({ data }: { data: Overview }) {
  const month = monthName()
  return (
    <div className="mx-auto max-w-[1040px]">
      <StatStrip stats={[
        { label: "Balance", value: <Money usd={data.balance_usd} /> },
        { label: `Spent in ${month}`, value: <Money usd={data.spent_month_usd} /> },
        { label: "Projected month", value: <Money usd={data.projected_month_usd} /> },
        { label: "Replies", value: data.turns_month.toLocaleString("en") },
      ]} />
      <Section title="Daily spend" aside={month}>
        <DailyChart data={data.daily} />
      </Section>
      <Section title="Balance history">
        <Table>
          <TableHeader>
            <TableRow className="hover:bg-transparent">
              <TableHead>Date</TableHead><TableHead>Kind</TableHead><TableHead className="max-sm:hidden">Note</TableHead>
              <TableHead className="max-md:hidden">By</TableHead><TableHead className="text-right">Amount</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {data.ledger.map((l) => (
              <TableRow key={l.id}>
                <TableCell className="text-muted-foreground">{shortDate(l.at)}</TableCell>
                <TableCell>{{ topup: "Top-up", promo: "Promo code", adjustment: "Adjustment" }[l.kind]}</TableCell>
                <TableCell className="text-muted-foreground max-sm:hidden">{l.note}</TableCell>
                <TableCell className="text-muted-foreground max-md:hidden">{l.by}</TableCell>
                <TableCell className="text-right font-medium tabular text-success">+{usd(l.amount_usd)}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </Section>
    </div>
  )
}

function DailyChart({ data }: { data: Overview["daily"] }) {
  const rows = data.map((d) => ({ ...d, label: dayMonth(new Date(d.day)) }))
  return (
    <div className="h-56 rounded-lg border px-2 pt-4 pb-1">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={rows} barCategoryGap={2} margin={{ top: 4, right: 20, bottom: 0, left: 0 }}>
          <CartesianGrid vertical={false} stroke="var(--border)" />
          <XAxis dataKey="label" tickLine={false} axisLine={false} interval={4} tick={{ fontSize: 11, fill: "var(--muted-foreground)" }} />
          <YAxis tickLine={false} axisLine={false} width={44} tickFormatter={(v) => `$${v}`} tick={{ fontSize: 11, fill: "var(--muted-foreground)" }} />
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
