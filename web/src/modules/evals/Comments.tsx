import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { X } from "lucide-react"
import { useState } from "react"

import { Ago } from "@/components/common"
import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/textarea"
import { api } from "@/lib/api"
import { E } from "./api"
import type { Anchor, Comment } from "./types"

/** Where a comment points, in words. */
export function anchorLabel(a: Anchor) {
  switch (a.target) {
    case "page": return `Page ${a.page}`
    case "image_region": return "Image region"
    case "table_range": return `${a.sheet ? `${a.sheet} ` : ""}${a.cells}`
    case "text_range": return "Text"
    case "card_element": return "Card"
    case "tool_call": case "world_call": return "Call"
    default: return "Message"
  }
}

export const AnchorChip = ({ a, onClear }: { a: Anchor; onClear?: () => void }) => (
  <span className="inline-flex h-6 max-w-full items-center gap-1 rounded-md border bg-muted px-1.5 text-xs">
    <span className="shrink-0 font-medium">{anchorLabel(a)}</span>{a.quote && <span className="truncate text-muted-foreground">{a.quote}</span>}
    {onClear && <button aria-label="Remove anchor" onClick={onClear}><X className="size-3" /></button>}
  </span>
)

/** Where comments live: a review task (blind readers see only these) or one case of a run. */
export type Scope = { task: string; initial: Comment[] } | { run: string; case: string }

/** Comments, newest last. The composer takes the anchor the reviewer pointed at; without one the comment is on the case. */
export function Comments({ scope, anchor, onClear, anonymous }: { scope: Scope; anchor: Anchor | null; onClear: () => void; anonymous?: boolean }) {
  const qc = useQueryClient()
  const [text, setText] = useState("")
  const inTask = "task" in scope
  const key = ["evals", "comments", inTask ? scope.task : scope.run]
  const list = useQuery({
    queryKey: key,
    queryFn: () => api.get<Comment[]>(inTask ? `${E}/tasks/${scope.task}/comments` : `${E}/comments?run_id=${encodeURIComponent(scope.run)}`),
    initialData: inTask ? scope.initial : undefined, staleTime: inTask ? Infinity : 0,
  })
  const shown = inTask ? list.data : list.data?.filter((c) => c.case_id === scope.case)
  const post = useMutation({
    // Anchors in a task name their column by label, which is also the comment's target.
    mutationFn: () => api.post<Comment>(`${E}/comments`, {
      text, anchors: anchor ? [anchor] : undefined,
      ...(inTask ? { task_id: scope.task, target: anchor?.item_id } : { run_id: scope.run, case_id: scope.case, item_id: anchor?.item_id }),
    }),
    onSuccess: () => { setText(""); onClear(); void qc.invalidateQueries({ queryKey: key }) },
  })
  return (
    <div className="space-y-3">
      {shown?.map((c) => (
        <div key={c.id} className="space-y-1 rounded-lg border p-2.5 text-sm">
          <div className="flex items-center gap-2 text-xs text-muted-foreground"><span className="font-medium text-foreground">{anonymous ? "Reviewer" : c.author.display_name ?? "Reviewer"}</span><Ago iso={c.at} /></div>
          {c.anchors?.map((a, i) => <AnchorChip key={i} a={a} />)}
          <p className="whitespace-pre-wrap break-words">{c.text}</p>
        </div>
      ))}
      <div className="space-y-2">
        {anchor && <AnchorChip a={anchor} onClear={onClear} />}
        <Textarea value={text} onChange={(e) => setText(e.target.value)} rows={2} placeholder="Comment" aria-label="Comment" className="min-h-0" />
        {post.error && <p role="alert" className="text-sm text-destructive">{post.error.message}</p>}
        <div className="flex justify-end"><Button size="sm" disabled={!text.trim() || post.isPending} onClick={() => post.mutate()}>Comment</Button></div>
      </div>
    </div>
  )
}
