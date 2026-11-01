import { useState } from "react"
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

export function Field({ label, children, changed }: { label: string; children: React.ReactNode; changed?: boolean }) {
  return (
    <div className="grid grid-cols-[112px_1fr] items-center gap-3 text-sm">
      <Label className="flex items-center gap-1.5 font-normal text-muted-foreground">{label}{changed && <Dot />}</Label>
      {children}
    </div>
  )
}

/** Text that writes to the draft as you type, debounced. */
export function useLocal(value: string, write: (v: string) => void) {
  const [v, setV] = useState(value)
  const [seen, setSeen] = useState(value)
  if (seen !== value) { setSeen(value); setV(value) }
  return [v, (x: string) => { setV(x); write(x) }] as const
}
