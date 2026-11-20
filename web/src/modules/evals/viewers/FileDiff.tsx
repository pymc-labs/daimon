import { createTwoFilesPatch } from "diff"
import { useMemo, useState } from "react"
import { Decoration, Diff, Hunk, parseDiff } from "react-diff-view"
import "react-diff-view/style/index.css"
import "./viewers.css"

import { Button } from "@/components/ui/button"

/** A unified or split line diff of two texts, or of a ready patch. */
export function FileDiff({ before, after, patch, name = "file" }: { before?: string; after?: string; patch?: string; name?: string }) {
  const [split, setSplit] = useState(false)
  const files = useMemo(() => {
    const text = patch ?? createTwoFilesPatch(name, name, before ?? "", after ?? "", "", "", { context: 3 })
    // The diff library prints an "Index:" banner that the parser does not take.
    return parseDiff(text.replace(/^Index:.*\n=+\n/, ""), { nearbySequences: "zip" })
  }, [before, after, patch, name])
  if (!files.length || files.every((f) => !f.hunks.length)) return <div className="py-6 text-center text-sm text-muted-foreground">No differences</div>
  return (
    <div className="space-y-2">
      <div className="flex gap-1">
        <Button size="xs" variant={split ? "secondary" : "ghost"} onClick={() => setSplit(true)}>Split</Button>
        <Button size="xs" variant={split ? "ghost" : "secondary"} onClick={() => setSplit(false)}>Unified</Button>
      </div>
      {files.map((f, i) => (
        <div key={i} className="overflow-x-auto rounded-md border">
          {(patch || files.length > 1) && <div className="border-b bg-muted px-2 py-1 font-mono text-xs">{f.newPath || f.oldPath}</div>}
          <Diff viewType={split ? "split" : "unified"} diffType={f.type} hunks={f.hunks}>
            {(hunks) => hunks.flatMap((h) => [<Decoration key={`d${h.content}`}><span className="font-mono text-xs text-faint">{h.content}</span></Decoration>, <Hunk key={h.content + "h"} hunk={h} />])}
          </Diff>
        </div>
      ))}
    </div>
  )
}
