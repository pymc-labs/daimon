import { creditTone } from "@/lib/credit"
import { usd } from "@/lib/format"
import type { CreditMeter as Meter } from "@/lib/types"
import { cn } from "@/lib/utils"

/** Credit left as a bar. `compact` is the sidebar form. */
export function CreditMeter({ meter, compact, className }: { meter: Meter; compact?: boolean; className?: string }) {
  const pct = meter.total_usd ? Math.max(0, Math.min(1, meter.left_usd / meter.total_usd)) : 0
  const tone = creditTone(meter)
  return (
    <div className={cn("space-y-1.5", className)}>
      <div className={cn("flex items-baseline justify-between gap-2 whitespace-nowrap", compact ? "text-xs" : "text-sm")}>
        <span className={cn("tabular font-medium", tone === "out" && "text-destructive", tone === "low" && "text-warning")}>
          {usd(meter.left_usd)} left
        </span>
        <span className="tabular text-muted-foreground">of {usd(meter.total_usd)}{!compact && meter.window === "month" ? " this month" : ""}</span>
      </div>
      <div className={cn("overflow-hidden rounded-full bg-foreground/10", compact ? "h-1" : "h-1.5")}>
        <div
          className={cn("h-full rounded-full", tone === "out" ? "bg-destructive" : tone === "low" ? "bg-warning" : "bg-teal")}
          style={{ width: `${pct * 100}%` }}
        />
      </div>
    </div>
  )
}
