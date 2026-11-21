import { Download, FileText, ShieldAlert } from "lucide-react"

import { cn } from "@/lib/utils"
import { STEP } from "../kinds"
import type { FileRef, StepStatus } from "../types"

export function StepIcon({ status, className }: { status: StepStatus; className?: string }) {
  const S = STEP[status]
  return <S.icon className={cn("size-3.5 shrink-0", S.cls, status === "running" && "animate-spin", className)} aria-label={S.label} />
}

/** Label and value, inline: the run header's facts. */
export function Fact({ label, children, className }: { label: string; children: React.ReactNode; className?: string }) {
  return <span className={cn("flex min-w-0 items-center gap-1.5 whitespace-nowrap", className)}><span className="text-muted-foreground">{label}</span>{children}</span>
}

/** JSON as an indented tree; paths in `untrusted` (such as `input.lead.message`) carry a shield. */
export function JsonTree({ value, root, untrusted = [] }: { value: unknown; root: string; untrusted?: string[] }) {
  const marked = new Set(untrusted)
  const row = (k: string | null, v: unknown, path: string, depth: number): React.ReactNode => {
    const flag = marked.has(path) && <ShieldAlert className="size-3 shrink-0 text-warning" aria-label="Untrusted" />
    const key = k != null && <span className="shrink-0 text-muted-foreground">{k}</span>
    if (v && typeof v === "object") {
      const entries = Array.isArray(v) ? v.map((x, i) => [String(i), x] as const) : Object.entries(v)
      return (
        <div key={path}>
          {k != null && <div className="flex h-6 items-center gap-1.5">{key}{flag}<span className="text-faint">{Array.isArray(v) ? `[${entries.length}]` : `{${entries.length}}`}</span></div>}
          <div className={cn(k != null && "ml-1.5 border-l pl-3")}>{entries.map(([ck, cv]) => row(ck, cv, `${path}.${ck}`, depth + 1))}</div>
        </div>
      )
    }
    const text = typeof v === "string" ? `"${v}"` : String(v)
    return (
      <div key={path} className="flex min-h-6 items-start gap-1.5 py-0.5">
        {key}{flag}
        <span className={cn("min-w-0 break-words", typeof v === "string" ? "text-foreground" : "text-link")}>{text}</span>
      </div>
    )
  }
  if (value == null) return <p className="text-sm text-faint">None</p>
  return <div className="font-mono text-xs">{row(null, value, root, 0)}</div>
}


const kb = (n: number) => (n < 1024 ? `${n} B` : `${(n / 1024).toFixed(1)} KB`)

/** Run files as rows that download from the run's file route. */
export function FileList({ files }: { files: FileRef[] }) {
  return (
    <>
      {files.map((f) => (
        <a key={f.url} href={f.url} download={f.name} className="group flex h-8 items-center gap-2 text-sm hover:text-link">
          <FileText className="size-4 text-muted-foreground" /><span className="flex-1 truncate">{f.name}</span>
          <span className="text-xs text-muted-foreground tabular">{kb(f.size)}</span><Download className="size-3.5 text-faint group-hover:text-link" aria-hidden />
        </a>
      ))}
    </>
  )
}
