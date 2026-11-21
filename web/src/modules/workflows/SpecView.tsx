import { CircleAlert, CircleCheck } from "lucide-react"
import { useEffect, useMemo, useRef, useState } from "react"

import { cn } from "@/lib/utils"
import type { Spec, Validation } from "./types"
import { fromYaml, toYaml } from "./yaml"

/** The spec as YAML; valid edits save, parse errors stay local and point at their line. */
export function SpecView({ spec, validation, ro, onChange }: { spec: Spec; validation?: Validation; ro: boolean; onChange: (s: Spec) => void }) {
  const yaml = useMemo(() => toYaml(spec), [spec])
  const [text, setText] = useState(yaml)
  const [error, setError] = useState<{ error: string; line: number } | null>(null)
  const mine = useRef(yaml)
  const gutter = useRef<HTMLDivElement>(null)
  useEffect(() => { if (yaml !== mine.current) { setText(yaml); mine.current = yaml; setError(null) } }, [yaml])
  useEffect(() => {
    if (text === mine.current) return
    const t = setTimeout(() => {
      const r = fromYaml(text)
      if (r.error != null) return setError(r)
      setError(null)
      mine.current = toYaml(r.spec)
      onChange(r.spec)
    }, 400)
    return () => clearTimeout(t)
  }, [text]) // eslint-disable-line react-hooks/exhaustive-deps
  const lines = text.split("\n")
  // Lines that open a node with a problem: `  <id>:` directly under `nodes:`.
  const bad = useMemo(() => {
    const ids = new Set([...(validation?.errors ?? []), ...(validation?.warnings ?? [])].map((p) => p.node).filter(Boolean))
    const out = new Set<number>()
    lines.forEach((l, i) => { const m = /^ {2}([\w-]+):\s*$/.exec(l); if (m && ids.has(m[1])) out.add(i + 1) })
    return out
  }, [text, validation]) // eslint-disable-line react-hooks/exhaustive-deps
  const count = (validation?.errors.length ?? 0) + (validation?.warnings.length ?? 0)
  return (
    <div className="absolute inset-0 px-4 pt-16 pb-20 lg:px-6">
      <div className="mx-auto flex h-full max-w-4xl flex-col overflow-hidden rounded-xl border bg-background">
        <div className="flex min-h-0 flex-1 font-mono text-[12.5px] leading-5">
          <div ref={gutter} className="w-12 shrink-0 overflow-hidden border-r bg-muted/40 py-3 text-right text-faint select-none">
            {lines.map((_, i) => (
              <div key={i} className={cn("pr-3", error?.line === i + 1 && "bg-destructive/10 text-destructive", bad.has(i + 1) && "text-warning")}>{i + 1}</div>
            ))}
          </div>
          <textarea
            value={text} readOnly={ro} spellCheck={false} onChange={(e) => setText(e.target.value)}
            onScroll={(e) => { if (gutter.current) gutter.current.scrollTop = e.currentTarget.scrollTop }}
            className="min-w-0 flex-1 resize-none overflow-auto bg-transparent px-4 py-3 whitespace-pre outline-none"
          />
        </div>
        <div className="flex h-8 shrink-0 items-center gap-2 border-t px-3 text-xs">
          {error ? (
            <span className="flex items-center gap-1.5 text-destructive"><CircleAlert className="size-3.5" />Line {error.line}: {error.error}</span>
          ) : count ? (
            <span className="flex items-center gap-1.5 text-warning"><CircleAlert className="size-3.5" />{count} {count === 1 ? "problem" : "problems"}</span>
          ) : (
            <span className="flex items-center gap-1.5 text-muted-foreground"><CircleCheck className="size-3.5 text-success" />Valid</span>
          )}
          <span className="ml-auto text-faint tabular">{lines.length} lines</span>
        </div>
      </div>
    </div>
  )
}
