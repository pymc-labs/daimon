import { Globe, Lock, MessageCircle } from "lucide-react"

import { BrandIcon } from "@/components/brand"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { cn } from "@/lib/utils"
import { EFFECT, lineDiff, RULE } from "./lib"
import type { Effect, Place, ToolRule } from "./types"

const pill = "inline-flex h-5 shrink-0 items-center rounded px-1.5 text-[11px] font-medium whitespace-nowrap"

export function EffectTag({ effect, className }: { effect: Effect; className?: string }) {
  const e = EFFECT[effect]
  return (
    <Tooltip>
      <TooltipTrigger asChild><span className={cn(pill, e.cls, className)}>{e.label}</span></TooltipTrigger>
      <TooltipContent>{e.fact}</TooltipContent>
    </Tooltip>
  )
}

export function RuleChip({ rule, className }: { rule: ToolRule; className?: string }) {
  return <span className={cn(pill, RULE[rule].cls, className)}>{RULE[rule].label}</span>
}

export function Pill({ children, className }: { children: React.ReactNode; className?: string }) {
  return <span className={cn(pill, "border bg-background text-muted-foreground", className)}>{children}</span>
}

export function PlaceIcon({ place, className }: { place: Pick<Place, "kind" | "platform">; className?: string }) {
  if (place.kind === "web") return <Globe className={cn("size-3.5 shrink-0 text-muted-foreground", className)} />
  if (place.kind === "dm") return <MessageCircle className={cn("size-3.5 shrink-0 text-muted-foreground", className)} />
  return <BrandIcon brand={place.platform} className={cn("size-3.5", className)} />
}

/** A changed-in-draft marker, Figma style. */
export function Dot({ className }: { className?: string }) {
  return <span className={cn("inline-block size-1.5 shrink-0 rounded-full bg-link", className)} role="img" aria-label="Changed" />
}

export function Locked({ who, className }: { who: string; className?: string }) {
  return (
    <div className={cn("flex items-center gap-1.5 text-xs text-muted-foreground", className)}>
      <Lock className="size-3.5 shrink-0" />Ask {who}
    </div>
  )
}

/** Unified diff, unchanged runs folded to two lines of context. */
export function Diff({ before, after, className }: { before: string; after: string; className?: string }) {
  const ops = lineDiff(before, after)
  const keep = ops.map((o, k) => o.t !== " " || ops.slice(Math.max(0, k - 2), k + 3).some((p) => p.t !== " "))
  const rows: React.ReactNode[] = []
  for (let k = 0; k < ops.length; k++) {
    if (!keep[k]) {
      let e = k
      while (e < ops.length && !keep[e]) e++
      rows.push(<div key={k} className="bg-muted/60 px-3 py-0.5 text-faint">{e - k} unchanged {e - k === 1 ? "line" : "lines"}</div>)
      k = e - 1
      continue
    }
    const o = ops[k]!
    rows.push(
      <div key={k} className={cn("flex", o.t === "+" && "bg-success/10", o.t === "-" && "bg-destructive/10")}>
        <span className={cn("w-6 shrink-0 text-center select-none", o.t === "+" ? "text-success" : o.t === "-" ? "text-destructive" : "text-faint")}>{o.t}</span>
        <span className="min-w-0 pr-3 break-words whitespace-pre-wrap">{o.s || " "}</span>
      </div>,
    )
  }
  return <div className={cn("overflow-hidden rounded-md border font-mono text-[11.5px] leading-5", className)}>{rows}</div>
}
