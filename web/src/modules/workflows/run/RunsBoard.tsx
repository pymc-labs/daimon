import { ChevronLeft, ChevronRight, CircleHelp, Hourglass, MessageCircleQuestion, UserX, type LucideIcon } from "lucide-react"
import { useNavigate } from "react-router"

import { Button } from "@/components/ui/button"
import { QueryState } from "@/components/QueryState"
import { useBoard, useCalendar } from "../api"
import type { Board, Calendar, RunCard, WaitingReason } from "../types"
import { RunBadge } from "../ui"
import { monthOf, monthWeeks, shiftMonth } from "./lib"

const ATTENTION: Partial<Record<WaitingReason, { icon: LucideIcon; label: string }>> = {
  approval: { icon: Hourglass, label: "Approval" },
  question: { icon: MessageCircleQuestion, label: "Question" },
  unknown: { icon: CircleHelp, label: "Unknown outcome" },
  blocked: { icon: UserX, label: "Needs owner" },
}

const shown = (c: RunCard) => Object.entries(c.card).filter(([, v]) => v !== null && v !== "")

/** One run as a card: the first card field as its title, the rest below, an attention badge when a person is needed. */
function Card({ c }: { c: RunCard }) {
  const navigate = useNavigate()
  const [title, ...rest] = shown(c)
  const a = c.attention && c.run.waiting_reason ? ATTENTION[c.run.waiting_reason] : undefined
  const ended = c.run.status === "failed" || c.run.status === "cancelled" || c.run.status === "timed_out"
  return (
    <button type="button" onClick={() => navigate(`/workflows/${c.run.workflow.id}/runs/${c.run.number}`)} className="w-full rounded-lg border bg-background p-3 text-left text-sm hover:bg-accent/40">
      <span className="flex items-start justify-between gap-2">
        <span className="min-w-0 truncate font-medium">{title ? String(title[1]) : `Run ${c.run.number}`}</span>
        <span className="tabular text-xs text-muted-foreground">#{c.run.number}</span>
      </span>
      {rest.map(([k, v]) => <span key={k} className="mt-1 flex gap-1.5 text-xs text-muted-foreground"><span className="text-faint">{k}</span><span className="truncate">{String(v)}</span></span>)}
      {(a || ended) && (
        <span className="mt-2 flex items-center gap-2 text-xs">
          {a && <span className="inline-flex h-5 items-center gap-1 rounded border bg-background px-1.5 text-[11px] font-medium text-warning"><a.icon className="size-3" />{a.label}</span>}
          {ended && <RunBadge status={c.run.status} />}
        </span>
      )}
    </button>
  )
}

/** Stage columns, then Done and Failed. */
function Columns({ board }: { board: Board }) {
  return (
    <div className="flex items-start gap-3 overflow-x-auto px-4 py-4 lg:px-6">
      {board.columns.map((col) => (
        <section key={col.stage} aria-label={col.stage} className="w-72 shrink-0 space-y-2 rounded-lg bg-muted/50 p-2">
          <h3 className="flex items-center justify-between px-1 text-xs font-medium"><span>{col.stage}</span><span className="tabular text-muted-foreground">{col.cards.length}</span></h3>
          {col.cards.map((c) => <Card key={c.run.id} c={c} />)}
        </section>
      ))}
    </div>
  )
}

export function BoardView({ id }: { id: string }) {
  return <QueryState query={useBoard(id)} className="px-6 pt-6">{(board) => <Columns board={board} />}</QueryState>
}

const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
const MONTH_NAME = new Intl.DateTimeFormat("en-GB", { month: "long", year: "numeric", timeZone: "UTC" })

/** A month grid by each card's date; on a phone, a list of the days that have runs. */
function Grid({ cal, onMonth }: { cal: Calendar; onMonth: (m: string) => void }) {
  const by = new Map<string, RunCard[]>()
  for (const c of cal.cards) by.set(c.date ?? "", [...(by.get(c.date ?? "") ?? []), c])
  return (
    <div className="px-4 py-4 lg:px-6">
      <div className="mb-3 flex items-center gap-1">
        <Button variant="ghost" size="icon-sm" aria-label="Previous month" onClick={() => onMonth(shiftMonth(cal.month, -1))}><ChevronLeft /></Button>
        <h3 className="w-40 text-center text-sm font-medium">{MONTH_NAME.format(new Date(`${cal.month}-01T00:00:00Z`))}</h3>
        <Button variant="ghost" size="icon-sm" aria-label="Next month" onClick={() => onMonth(shiftMonth(cal.month, 1))}><ChevronRight /></Button>
        <Button variant="ghost" size="sm" className="ml-auto text-xs" onClick={() => onMonth(monthOf(Date.now()))}>Today</Button>
      </div>
      <div className="grid grid-cols-7 gap-px overflow-hidden rounded-lg border bg-border max-sm:grid-cols-1 max-sm:gap-2 max-sm:border-0 max-sm:bg-transparent">
        {WEEKDAYS.map((d) => <div key={d} className="bg-muted/50 px-2 py-1 text-xs text-muted-foreground max-sm:hidden">{d}</div>)}
        {monthWeeks(cal.month).flat().map((day, i) => {
          const cards = day ? by.get(day) ?? [] : []
          return (
            <div key={day ?? `x${i}`} className={`min-h-24 space-y-1.5 bg-background p-1.5 ${cards.length ? "" : "max-sm:hidden"}`}>
              {day && <div className="tabular px-0.5 text-xs text-muted-foreground"><span className="sm:hidden">{WEEKDAYS[(i % 7)]} </span>{Number(day.slice(8))}</div>}
              {cards.map((c) => <Card key={c.run.id} c={c} />)}
            </div>
          )
        })}
      </div>
    </div>
  )
}

export function CalendarView({ id, month, onMonth }: { id: string; month: string; onMonth: (m: string) => void }) {
  return <QueryState query={useCalendar(id, month)} className="px-6 pt-6">{(cal) => <Grid cal={cal} onMonth={onMonth} />}</QueryState>
}
