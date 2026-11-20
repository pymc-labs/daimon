import { useState } from "react"

import { Prose } from "@/components/common"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"
import { Rows } from "@/components/QueryState"
import type { Output, Pin } from "../types"
import { useText } from "./blob"

/** Text, markdown and code: rendered or raw; a click on a raw line anchors a comment to it. */
export function Text({ o, pin }: { o: Output; pin?: Pin }) {
  const text = useText(o.blob.sha256)
  const markdown = o.media_type.includes("markdown") || o.name.endsWith(".md")
  const [raw, setRaw] = useState(!markdown)
  if (text.data === undefined) return <Rows n={3} />
  let offset = 0
  return (
    <div className="space-y-2">
      {markdown && (
        <div className="flex gap-1">
          <Button size="xs" variant={raw ? "ghost" : "secondary"} onClick={() => setRaw(false)}>Rendered</Button>
          <Button size="xs" variant={raw ? "secondary" : "ghost"} onClick={() => setRaw(true)}>Raw</Button>
        </div>
      )}
      {raw ? (
        <pre className="max-h-96 overflow-auto rounded-md border bg-muted/40 py-1 font-mono text-xs leading-5">
          {text.data.split("\n").map((line, i) => {
            const start = offset
            offset += line.length + 1
            return (
              <div key={i} className={cn("flex gap-3 px-2", pin && "cursor-pointer hover:bg-accent")} onClick={pin && (() => pin({ target: "text_range", ref: o.id, start, end: start + line.length, quote: line.slice(0, 80) }))}>
                <span className="w-6 shrink-0 select-none text-right text-faint tabular">{i + 1}</span>
                <span className="whitespace-pre-wrap break-words">{line || " "}</span>
              </div>
            )
          })}
        </pre>
      ) : <Prose text={text.data} />}
    </div>
  )
}
