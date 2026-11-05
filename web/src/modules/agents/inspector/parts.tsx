import { Label } from "@/components/ui/label"
import { cn } from "@/lib/utils"
import { Dot } from "../bits"
import type { Agent, AgentConfig, Catalog } from "../types"

export type Props = {
  node: string
  agent: Agent
  config: AgentConfig
  catalog?: Catalog
  ro: boolean
  changed: Set<string>
  update: (fn: (c: AgentConfig) => void, delay?: number) => void
  select: (id: string) => void
}
export function Section({ title, action, children, className, flush }: { title?: string; action?: React.ReactNode; children: React.ReactNode; className?: string; flush?: boolean }) {
  return (
    <section className={cn("border-b py-4 last:border-b-0", flush ? "pb-0" : "px-5", className)}>
      {(title || action) && (
        <div className={cn("mb-3 flex min-h-6 items-center gap-2", flush && "px-5")}>
          <h3 className="text-xs font-medium text-muted-foreground">{title}</h3>
          <div className="ml-auto">{action}</div>
        </div>
      )}
      {children}
    </section>
  )
}

export function Field({ label, children, changed, top }: { label: string; children: React.ReactNode; changed?: boolean; /** Align the label with the top of a tall value. */ top?: boolean }) {
  return (
    <div className={cn("grid grid-cols-[112px_1fr] gap-3 text-sm", top ? "items-start" : "items-center")}>
      <Label className={cn("flex items-center gap-1.5 font-normal text-muted-foreground", top && "min-h-8")}>{label}{changed && <Dot />}</Label>
      {children}
    </div>
  )
}
