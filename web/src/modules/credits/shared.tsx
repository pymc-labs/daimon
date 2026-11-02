import { Label } from "@/components/ui/label"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { monthName } from "@/lib/format"
import { cn } from "@/lib/utils"
import type { Period } from "./types"

export function PeriodToggle({ value, onChange }: { value: Period; onChange: (p: Period) => void }) {
  return (
    <ToggleGroup type="single" value={value} onValueChange={(v) => v && onChange(v as Period)} variant="segmented" size="sm">
      <ToggleGroupItem value="30d" className="px-3 text-xs">Last 30 days</ToggleGroupItem>
      <ToggleGroupItem value="this" className="px-3 text-xs">{monthName()}</ToggleGroupItem>
      <ToggleGroupItem value="last" className="px-3 text-xs">{monthName(1)}</ToggleGroupItem>
    </ToggleGroup>
  )
}

export function Section({ title, aside, children, className }: { title: string; aside?: React.ReactNode; children: React.ReactNode; className?: string }) {
  return (
    <section className={cn("mt-8", className)}>
      <div className="mb-2 flex items-baseline justify-between">
        <h2 className="text-sm font-semibold">{title}</h2>
        {aside && <span className="text-xs text-muted-foreground">{aside}</span>}
      </div>
      {children}
    </section>
  )
}

export function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return <div className="space-y-1.5"><Label>{label}</Label>{children}</div>
}
