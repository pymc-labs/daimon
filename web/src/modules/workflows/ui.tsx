import { CirclePause, CircleCheck, PencilLine, ShieldAlert, UserX, type LucideIcon } from "lucide-react"

import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { absolute } from "@/lib/format"
import { cn } from "@/lib/utils"
import { RUN, TRIGGER, triggerText } from "./kinds"
import type { RunStatus, Status, Tick, Trigger } from "./types"


export function TriggerIcons({ triggers }: { triggers: Trigger[] }) {
  if (!triggers.length) return <span className="text-faint">–</span>
  return (
    <span className="flex items-center gap-2">
      {triggers.map((t) => {
        const Icon = TRIGGER[t.kind].icon
        return (
          <Tooltip key={t.id}>
            <TooltipTrigger asChild><span className="flex text-muted-foreground"><Icon className="size-3.5" /></span></TooltipTrigger>
            <TooltipContent>{triggerText(t)}</TooltipContent>
          </Tooltip>
        )
      })}
    </span>
  )
}

const STATUS: Record<Status, { icon: LucideIcon; label: string; cls: string }> = {
  live: { icon: CircleCheck, label: "Live", cls: "text-success" },
  paused: { icon: CirclePause, label: "Paused", cls: "text-muted-foreground" },
  draft: { icon: PencilLine, label: "Draft only", cls: "text-link" },
  needs_owner: { icon: UserX, label: "Needs owner", cls: "text-warning" },
  needs_review: { icon: ShieldAlert, label: "Needs review", cls: "text-warning" },
}
export function StatusBadge({ status, className }: { status: Status; className?: string }) {
  const s = STATUS[status]
  return (
    <span className={cn("inline-flex h-5 shrink-0 items-center gap-1 rounded border bg-background px-1.5 text-[11px] font-medium whitespace-nowrap", s.cls, className)}>
      <s.icon className="size-3" />{s.label}
    </span>
  )
}

export function RunBadge({ status }: { status: RunStatus }) {
  const r = RUN[status]
  return <span className={cn("inline-flex items-center gap-1.5 whitespace-nowrap", r.cls)}><r.icon className="size-3.5" /><span className="text-foreground">{r.label}</span></span>
}

/** Tick shapes, so failures read without color: full bar, hollow bar, half bar, dash. */
const TICK: Record<RunStatus, string> = {
  succeeded: "h-3.5 bg-success/70", failed: "h-3.5 border-[1.5px] border-destructive", timed_out: "h-3.5 border-[1.5px] border-destructive",
  waiting: "h-2 bg-warning", running: "h-2 bg-link animate-pulse", queued: "h-1 bg-faint", cancelled: "h-1 bg-faint",
}
export function Ticks({ ticks, className }: { ticks: Tick[]; className?: string }) {
  if (!ticks.length) return <span className="text-faint">–</span>
  return (
    <span className={cn("flex h-3.5 items-end gap-[3px]", className)}>
      {ticks.map((t) => (
        <Tooltip key={t.number}>
          <TooltipTrigger asChild><span className={cn("w-1.5 rounded-[1.5px]", TICK[t.status])} /></TooltipTrigger>
          <TooltipContent>#{t.number} {RUN[t.status].label}, {absolute(t.at)}</TooltipContent>
        </Tooltip>
      ))}
    </span>
  )
}

/** Marks a changed step; the draft bar's "N changes" carries the same dot as its legend (the agent editor's pattern). */
export function Dot({ className }: { className?: string }) {
  return <span className={cn("inline-block size-1.5 shrink-0 rounded-full bg-link", className)} role="img" aria-label="Changed" title="Changed" />
}
