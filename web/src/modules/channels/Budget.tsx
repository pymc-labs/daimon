import { useState } from "react"

import { PanelSection, SettingRow } from "@/components/panel"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { useConfirm } from "@/lib/confirm"
import { shortDate, usd } from "@/lib/format"
import { cn } from "@/lib/utils"
import { Field } from "@/modules/admin/shared"
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

/** The budget as one row: amount and period, with the form opening under it. */
export function Budget({ c }: { c: ChannelDetail }) {
  const b = c.budget
  const [open, setOpen] = useState(false)
  if (!c.spend) return null
  const editable = c.can_manage && c.budget_editable
  return (
    <PanelSection title="Budget">
      <SettingRow label="Limit" overridden={!!b}>
        <div className="flex items-center gap-3">
          <div className="min-w-0 flex-1">
            {b ? (
              <>
                <div className="flex items-baseline gap-2 text-sm">
                  <span className={cn("tabular font-medium", b.exceeded && "text-destructive")}>{usd(b.spent_usd)} of {usd(Number(b.limit_usd))}</span>
                  <span className="truncate text-muted-foreground">{windowText(b)}</span>
                </div>
                <div className="mt-1.5 h-1.5 max-w-64 overflow-hidden rounded-full bg-foreground/10">
                  <div className={cn("h-full rounded-full", b.exceeded ? "bg-destructive" : b.percent_used >= 80 ? "bg-warning" : "bg-teal")} style={{ width: `${Math.min(100, b.percent_used)}%` }} />
                </div>
              </>
            ) : <span className="text-muted-foreground">No budget</span>}
          </div>
          {editable && !open && <Button size="xs" variant="outline" onClick={() => setOpen(true)}>{b ? "Edit" : "Set"}</Button>}
        </div>
      </SettingRow>
      {editable && open && <BudgetForm key={JSON.stringify(b && [b.limit_usd, b.window, b.starts_at, b.ends_at])} c={c} onDone={() => setOpen(false)} />}
    </PanelSection>
  )
}

function BudgetForm({ c, onDone }: { c: ChannelDetail; onDone: () => void }) {
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
    return confirm({ title: "Stop replies now?", body: `${usd(amount)} is at or below ${usd(spentFor(c, window))} spent.`, action: "Save", destructive: true })
  }
  const finish = (r: Outcome) => { setOut(r); if (!r.error) onDone() }
  const setIt = async () => {
    if (!(await stop(Number(limit)))) return
    finish(await save.run("put", "budget", {
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
    finish(await save.run("put", "budget", { action: "clear", expected }))
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
        <Button size="sm" variant="ghost" onClick={onDone}>Cancel</Button>
        {b && <Button size="sm" variant="ghost" className="text-destructive" disabled={save.pending} onClick={() => void clear()}>Remove</Button>}
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
