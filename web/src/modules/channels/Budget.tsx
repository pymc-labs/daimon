import { useState } from "react"

import { Money } from "@/components/common"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { useConfirm } from "@/lib/confirm"
import { shortDate, usd } from "@/lib/format"
import { cn } from "@/lib/utils"
import { Facts, Field, PanelSection } from "@/modules/admin/shared"
import { useSave, type Outcome } from "./api"
import { budgetStops, spentFor, WINDOWS } from "./lib"
import { Notice } from "./shared"
import type { BudgetWindow, ChannelBudget, ChannelDetail } from "./types"

const WINDOW_NAMES = Object.keys(WINDOWS) as BudgetWindow[]
const day = (iso: string | null) => (iso ? iso.slice(0, 10) : "")

function windowText(b: ChannelBudget) {
  if (b.window === "fixed") return `${shortDate(b.starts_at)} to ${shortDate(b.ends_at)}`
  return WINDOWS[b.window]
}

export function Budget({ c }: { c: ChannelDetail }) {
  const b = c.budget
  return (
    <PanelSection title="Budget and spend">
      {b && (
        <div className="mb-3">
          <div className="flex items-baseline justify-between gap-2 text-sm">
            <span className={cn("tabular font-medium", b.exceeded && "text-destructive")}>{usd(b.spent_usd)} of {usd(Number(b.limit_usd))}</span>
            <span className="text-muted-foreground">{windowText(b)}</span>
          </div>
          <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-foreground/10">
            <div className={cn("h-full rounded-full", b.exceeded ? "bg-destructive" : b.percent_used >= 80 ? "bg-warning" : "bg-teal")} style={{ width: `${Math.min(100, b.percent_used)}%` }} />
          </div>
        </div>
      )}
      <Facts rows={[
        ["Budget", b ? null : "None"],
        ["This month", <Money key="m" usd={c.spend.month_usd} />],
        ["Last 30 days", <Money key="d" usd={c.spend.last_30_days_usd} />],
      ]} />
      {c.can_manage && c.budget_editable && <BudgetForm key={JSON.stringify(b && [b.limit_usd, b.window, b.starts_at, b.ends_at])} c={c} />}
    </PanelSection>
  )
}

function BudgetForm({ c }: { c: ChannelDetail }) {
  const b = c.budget
  const save = useSave(c.id)
  const confirm = useConfirm()
  const [out, setOut] = useState<Outcome | null>(null)
  const [limit, setLimit] = useState(b?.limit_usd ?? "")
  const [window, setWindow] = useState<BudgetWindow>(b?.window ?? "monthly")
  const [starts, setStarts] = useState(day(b?.starts_at ?? null))
  const [ends, setEnds] = useState(day(b?.ends_at ?? null))
  const [more, setMore] = useState("")
  const expected = b && { limit_usd: b.limit_usd, window: b.window, starts_at: b.starts_at, ends_at: b.ends_at }
  // Keep the instant as read when its day is untouched.
  const instant = (v: string, was: string | null) => (!v ? null : v === day(was) ? was : v)

  const stop = async (amount: number) => {
    if (!budgetStops(amount, spentFor(c, window))) return true
    return confirm({ title: "Stop new turns now?", body: `${usd(amount)} is at or below ${usd(spentFor(c, window))} spent.`, action: "Save", destructive: true })
  }
  const setIt = async () => {
    if (!(await stop(Number(limit)))) return
    setOut(await save.run("put", "budget", {
      action: "set", limit_usd: limit, window, expected,
      starts_at: window === "monthly" ? null : instant(starts, b?.starts_at ?? null), ends_at: window === "fixed" ? instant(ends, b?.ends_at ?? null) : null,
    }))
  }
  const raise = async () => {
    if (!b || !(await stop(Number(b.limit_usd) + Number(more)))) return
    setOut(await save.run("put", "budget", { action: "raise", amount_usd: more, expected }))
    setMore("")
  }
  const clear = async () => {
    if (!(await confirm({ title: "Remove the budget?", action: "Remove", destructive: true }))) return
    setOut(await save.run("put", "budget", { action: "clear", expected }))
  }
  const changed = !b || limit !== b.limit_usd || window !== b.window || starts !== day(b.starts_at) || ends !== day(b.ends_at)

  return (
    <div className="mt-4 space-y-3 border-t pt-4">
      <div className="grid grid-cols-2 gap-3">
        <Field label="Limit in dollars"><Input inputMode="decimal" value={limit} onChange={(e) => setLimit(e.target.value)} placeholder="25.00" /></Field>
        <Field label="Window">
          <Select value={window} onValueChange={(v) => setWindow(v as BudgetWindow)}>
            <SelectTrigger className="w-full" aria-label="Window"><SelectValue /></SelectTrigger>
            <SelectContent>{WINDOW_NAMES.map((w) => <SelectItem key={w} value={w}>{WINDOWS[w]}</SelectItem>)}</SelectContent>
          </Select>
        </Field>
        {window !== "monthly" && <Field label={window === "fixed" ? "From" : "Counting from"}><Input type="date" value={starts} onChange={(e) => setStarts(e.target.value)} /></Field>}
        {window === "fixed" && <Field label="Until"><Input type="date" value={ends} onChange={(e) => setEnds(e.target.value)} /></Field>}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <Button size="sm" disabled={save.pending || !limit || !changed} onClick={() => void setIt()}>Save</Button>
        {b && <Button size="sm" variant="outline" className="text-destructive" disabled={save.pending} onClick={() => void clear()}>Remove</Button>}
      </div>
      {b && (
        <div className="flex items-end gap-2 border-t pt-3">
          <Field label="Raise by"><Input className="w-28" inputMode="decimal" value={more} onChange={(e) => setMore(e.target.value)} placeholder="5.00" /></Field>
          <Button size="sm" variant="outline" className="mb-0.5" disabled={save.pending || !(Number(more) > 0)} onClick={() => void raise()}>Raise</Button>
        </div>
      )}
      <Notice out={out} />
    </div>
  )
}
