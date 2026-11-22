import { CircleCheck, CircleX, TriangleAlert } from "lucide-react"
import { useEffect, useState } from "react"

import { Tag } from "@/components/panel"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { ago } from "@/lib/format"
import { cn } from "@/lib/utils"
import { usePeriod } from "./ops"
import type { Issue, OpsPeriod, OpsSeverity, OpsStatus } from "./types"

export function PeriodToggle() {
  const [period, setPeriod] = usePeriod()
  return (
    <ToggleGroup type="single" value={period} onValueChange={(v) => v && setPeriod(v as OpsPeriod)} variant="segmented" size="sm" aria-label="Period">
      <ToggleGroupItem value="24h" className="px-3 text-xs">24h</ToggleGroupItem>
      <ToggleGroupItem value="7d" className="px-3 text-xs">7 days</ToggleGroupItem>
      <ToggleGroupItem value="30d" className="px-3 text-xs">30 days</ToggleGroupItem>
    </ToggleGroup>
  )
}

/** "Updated 1m ago", kept current between refetches. */
export function Updated({ at }: { at: string | number | undefined }) {
  const [, tick] = useState(0)
  useEffect(() => { const t = setInterval(() => tick((n) => n + 1), 15_000); return () => clearInterval(t) }, [])
  if (!at) return null
  return <span className="ml-auto text-xs text-muted-foreground tabular">Updated {ago(new Date(at).toISOString())}</span>
}

const SEVERITY = {
  error: { icon: CircleX, label: "Error", cls: "text-destructive" },
  warning: { icon: TriangleAlert, label: "Warning", cls: "text-warning" },
} as const

export function SeverityIcon({ severity, className }: { severity: OpsSeverity; className?: string }) {
  const { icon: Icon, label, cls } = SEVERITY[severity]
  return <Icon role="img" aria-label={label} className={cn("size-4 shrink-0", cls, className)} />
}

const STATUS = {
  healthy: { icon: CircleCheck, label: "Healthy", cls: "text-success" },
  degraded: { icon: TriangleAlert, label: "Degraded", cls: "text-warning" },
  failing: { icon: CircleX, label: "Failing", cls: "text-destructive" },
} as const

export function StatusLabel({ status }: { status: OpsStatus }) {
  const { icon: Icon, label, cls } = STATUS[status]
  return <span className={cn("inline-flex items-center gap-1.5", cls)}><Icon className="size-4" />{label}</span>
}

/** Severity, title and the New or Regressed tag of an issue row. */
export function IssueName({ issue }: { issue: Issue }) {
  return (
    <span className="flex min-w-0 items-center gap-2">
      <SeverityIcon severity={issue.severity} />
      <span className="truncate font-medium">{issue.title}</span>
      {issue.regressed ? <Tag>Regressed</Tag> : issue.new && <Tag>New</Tag>}
    </span>
  )
}
