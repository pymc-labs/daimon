import { Check, Copy } from "lucide-react"
import { useState } from "react"
import ReactMarkdown from "react-markdown"
import remarkBreaks from "remark-breaks"
import remarkGfm from "remark-gfm"

import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { absolute, ago, usd } from "@/lib/format"
import { cn } from "@/lib/utils"

export function Ago({ iso, className }: { iso?: string | null; className?: string }) {
  if (!iso) return <span className={cn("text-faint", className)}>Never</span>
  return (
    <Tooltip>
      <TooltipTrigger asChild><span className={cn("whitespace-nowrap", className)}>{ago(iso)}</span></TooltipTrigger>
      <TooltipContent>{absolute(iso)}</TooltipContent>
    </Tooltip>
  )
}

type Health = "healthy" | "failing" | "setup"

export function StatusDot({ status, label }: { status: Health; label?: string }) {
  const cls = {
    healthy: "bg-success rounded-full", failing: "bg-destructive rounded-[1px]", setup: "bg-faint rounded-full",
  }[status]
  return (
    <span className="inline-flex items-center gap-1.5 whitespace-nowrap">
      <span className={cn("inline-block size-1.5 shrink-0", cls)} />
      {label}
    </span>
  )
}

export function CopyButton({ text, className, label }: { text: string; className?: string; label?: string }) {
  const [done, setDone] = useState(false)
  return (
    <button
      type="button"
      aria-label={label ?? "Copy"}
      className={cn("inline-flex size-7 items-center justify-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground", className)}
      onClick={(e) => {
        e.stopPropagation()
        navigator.clipboard?.writeText(text)
        setDone(true)
        setTimeout(() => setDone(false), 2000)
      }}
    >
      {done ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
    </button>
  )
}

export function Kbd({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <kbd className={cn("inline-flex h-5 min-w-5 items-center justify-center rounded border bg-background px-1 font-sans text-[11px] font-medium text-muted-foreground", className)}>
      {children}
    </kbd>
  )
}

export function Mono({ children, className }: { children: React.ReactNode; className?: string }) {
  return <span className={cn("font-mono text-[12.5px]", className)}>{children}</span>
}

export function StatStrip({ stats }: { stats: { label: string; value: React.ReactNode }[] }) {
  return (
    <div className="grid grid-cols-2 gap-px overflow-hidden rounded-lg border bg-border sm:flex">
      {stats.map((s) => (
        <div key={s.label} className="min-w-0 flex-1 bg-background px-4 py-3 max-sm:last:odd:col-span-2">
          <div className="text-xs font-medium text-muted-foreground">{s.label}</div>
          <div className="mt-1 truncate text-lg font-semibold tabular">{s.value}</div>
        </div>
      ))}
    </div>
  )
}

export function Avatar({ name, className }: { name: string; className?: string }) {
  const initials = name.split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0]!.toUpperCase()).join("")
  return (
    <span className={cn("inline-flex size-6 shrink-0 items-center justify-center rounded-full bg-navy text-[10px] font-semibold text-white dark:bg-aqua dark:text-navy", className)}>
      {initials}
    </span>
  )
}

/** Source text as Markdown; `breaks` keeps single newlines, as chat and plain-text sources mean them. */
export function Prose({ text, breaks, className }: { text: string; breaks?: boolean; className?: string }) {
  return (
    <div className={cn("prose-md text-sm leading-6 break-words", className)}>
      <ReactMarkdown
        remarkPlugins={breaks ? [remarkGfm, remarkBreaks] : [remarkGfm]}
        components={{
          a: ({ href, children }) => <a href={href} target="_blank" rel="noreferrer">{children}</a>,
          table: ({ children }) => <div className="overflow-x-auto"><table>{children}</table></div>,
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  )
}

/** An agent's face: the generated avatar, square with rounded corners. */
export function AgentAvatar({ src, name, className }: { src?: string | null; name: string; className?: string }) {
  const [failed, setFailed] = useState<string | null>(null)
  if (!src || failed === src) return <Avatar name={name} className={cn("rounded-md", className)} />
  return <img src={src} alt="" onError={() => setFailed(src)} className={cn("size-6 shrink-0 rounded-md object-cover", className)} />
}

export function Money({ usd: n, className }: { usd: number | null | undefined; className?: string }) {
  return <span className={cn("tabular", className)}>{usd(n)}</span>
}
