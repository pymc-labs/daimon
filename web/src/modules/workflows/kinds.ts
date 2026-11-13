import {
  Bot, CircleCheck, CircleDashed, CircleHelp, CircleMinus, CircleSlash, CircleX, Clock, Code, Flag, Hand, Hourglass, LoaderCircle, Repeat, Shuffle, Split, TimerOff, UserCheck, Webhook,
  Minus, Pencil, Plus, Workflow as WorkflowIcon, Wrench, Zap, type LucideIcon,
} from "lucide-react"

import type { Effect } from "./review"
import type { Catalog, GrantKind, Kind, Node, RunStatus, Spec, StepStatus, Trigger, TriggerKind } from "./types"

/** Each step kind: its icon, name and color (a strip on the card, the icon tint). */
export const KIND: Record<Kind, { icon: LucideIcon; label: string; strip: string; text: string }> = {
  agent: { icon: Bot, label: "Agent", strip: "bg-chart-1", text: "text-chart-1" },
  branch: { icon: Split, label: "Branch", strip: "bg-chart-2", text: "text-chart-2" },
  action: { icon: Wrench, label: "Action", strip: "bg-chart-3", text: "text-chart-3" },
  approval: { icon: UserCheck, label: "Approval", strip: "bg-chart-4", text: "text-chart-4" },
  transform: { icon: Shuffle, label: "Transform", strip: "bg-chart-5", text: "text-chart-5" },
  code: { icon: Code, label: "Code", strip: "bg-chart-5", text: "text-chart-5" },
  wait: { icon: Hourglass, label: "Wait", strip: "bg-warning", text: "text-warning" },
  for_each: { icon: Repeat, label: "For each", strip: "bg-chart-2", text: "text-chart-2" },
  workflow: { icon: WorkflowIcon, label: "Workflow", strip: "bg-faint", text: "text-muted-foreground" },
  finish: { icon: Flag, label: "Finish", strip: "bg-success", text: "text-success" },
}

export const TRIGGER: Record<TriggerKind, { icon: LucideIcon; label: string }> = {
  manual: { icon: Hand, label: "Manual" }, schedule: { icon: Clock, label: "Schedule" }, webhook: { icon: Webhook, label: "Webhook" },
  event: { icon: Zap, label: "Event" }, call: { icon: WorkflowIcon, label: "Called" },
}

const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
/** "Mon 09:00", "Weekdays 08:30", "Day 1 07:00", or the cron itself. */
export function cronLabel(cron = "") {
  const [m, h, dom, , dow] = cron.split(" ")
  if (!m || !h || !/^\d+$/.test(m) || !/^\d+$/.test(h)) return cron
  const at = `${h.padStart(2, "0")}:${m.padStart(2, "0")}`
  if (dow === "1-5") return `Weekdays ${at}`
  if (dow && /^\d$/.test(dow)) return `${DAYS[Number(dow)]} ${at}`
  if (dom && /^\d+$/.test(dom)) return `Day ${dom} ${at}`
  return `Daily ${at}`
}
export const triggerText = (t: Trigger) =>
  t.kind === "schedule" ? cronLabel(t.cron) : t.kind === "webhook" ? "Webhook" : t.kind === "event" ? t.source ?? "Event" : TRIGGER[t.kind].label

export const RUN: Record<RunStatus, { icon: LucideIcon; label: string; cls: string }> = {
  queued: { icon: CircleDashed, label: "Queued", cls: "text-muted-foreground" },
  running: { icon: LoaderCircle, label: "Running", cls: "text-link [&_svg]:animate-spin" },
  waiting: { icon: Hourglass, label: "Waiting", cls: "text-warning" },
  succeeded: { icon: CircleCheck, label: "Succeeded", cls: "text-success" },
  failed: { icon: CircleX, label: "Failed", cls: "text-destructive" },
  cancelled: { icon: CircleSlash, label: "Cancelled", cls: "text-muted-foreground" },
  timed_out: { icon: TimerOff, label: "Timed out", cls: "text-destructive" },
}

export const STEP: Record<StepStatus, { icon: LucideIcon; label: string; cls: string }> = {
  pending: { icon: CircleDashed, label: "Pending", cls: "text-faint" },
  ready: { icon: CircleDashed, label: "Ready", cls: "text-muted-foreground" },
  running: RUN.running,
  waiting: RUN.waiting,
  succeeded: RUN.succeeded,
  failed: RUN.failed,
  skipped: { icon: CircleMinus, label: "Skipped", cls: "text-faint" },
  cancelled: RUN.cancelled,
  unknown: { icon: CircleHelp, label: "Unknown", cls: "text-destructive" },
}

/** The facts on the line under a card's title: what the step does, in a few words. */
export function factsOf(n: Node, spec: Spec, catalog?: Catalog): string[] {
  switch (n.kind) {
    case "agent": {
      const key = n.agent ?? spec.defaults?.agent ?? "daimon"
      const fields = typeof n.output === "object" ? Object.keys(n.output).length : 0
      return [catalog?.agents.find((a) => a.key === key)?.name ?? key, ...(fields ? [`${fields} ${fields === 1 ? "field" : "fields"}`] : [])]
    }
    case "action": return [catalog?.tools.find((t) => t.id === n.tool)?.label ?? (n.tool || "No tool")]
    case "approval": return [[n.approvers ?? []].flat().map((p) => catalog?.people.find((x) => x.id === p)?.name ?? p.charAt(0).toUpperCase() + p.slice(1)).join(", ")]
    case "branch": return [`${n.cases?.length ?? 0} ${n.cases?.length === 1 ? "case" : "cases"}`, ...(n.else ? ["else"] : [])]
    case "wait": return [n.event ? `Until ${n.event.source}` : n.until ? "Until a time" : `For ${n.for ?? "?"}`]
    case "for_each": return [`${n.concurrency ?? 1} at a time`]
    case "transform": { const k = Object.keys(n.set ?? {}).length; return [`${k} ${k === 1 ? "field" : "fields"}`] }
    case "code": return ["Python", `Network ${n.network ?? "off"}`]
    case "workflow": return [n.workflow || "No workflow"]
    case "finish": {
      const to = n.deliver?.to === "home" ? spec.home?.channel : n.deliver?.to
      const channel = catalog?.channels.find((c) => c.id === to)?.name
      return [n.outcome === "failed" ? "Fail" : "Succeed", ...(to ? [channel ? `#${channel}` : to] : [])]
    }
  }
}

export const GRANT: Record<GrantKind, string> = {
  unattended_write: "Writes without approval", builtins: "Built-in tools", code_network: "Code with network", unverified_webhook: "Unverified webhook",
  budget_over_default: "Budget over default", untrusted_waiver: "Untrusted input waiver",
}
export const EFFECT: Record<Effect, { icon: typeof Plus; cls: string; label: string }> = {
  added: { icon: Plus, cls: "text-success", label: "Added" }, changed: { icon: Pencil, cls: "text-muted-foreground", label: "Changed" }, removed: { icon: Minus, cls: "text-destructive", label: "Removed" },
}
