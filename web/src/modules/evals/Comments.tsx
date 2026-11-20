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

/** Comments on a case, newest last; the composer takes the anchor the reviewer pointed at. */
export function Comments({ caseId, itemId, anchor, onClear }: { caseId: string; itemId?: string; anchor: Anchor | null; onClear: () => void }) {
  const qc = useQueryClient()
  const [text, setText] = useState("")
  const key = ["evals", "comments", caseId]
  const list = useQuery({ queryKey: key, queryFn: () => api.get<Comment[]>(`${E}/comments?case=${encodeURIComponent(caseId)}`) })
  const post = useMutation({
    mutationFn: () => api.post<Comment>(`${E}/comments`, { case_id: caseId, item_id: anchor?.item_id ?? itemId, text, anchors: anchor ? [anchor] : undefined }),
    onSuccess: () => { setText(""); onClear(); void qc.invalidateQueries({ queryKey: key }) },
  })
  return (
    <div className="space-y-3">
      {list.data?.map((c) => (
        <div key={c.id} className="space-y-1 rounded-lg border p-2.5 text-sm">
          <div className="flex items-center gap-2 text-xs text-muted-foreground"><span className="font-medium text-foreground">{c.author.display_name ?? "Reviewer"}</span><Ago iso={c.at} /></div>
          {c.anchors?.map((a, i) => <AnchorChip key={i} a={a} />)}
          <p className="whitespace-pre-wrap break-words">{c.text}</p>
        </div>
      ))}
      <div className="space-y-2">
        {anchor && <AnchorChip a={anchor} onClear={onClear} />}
        <Textarea value={text} onChange={(e) => setText(e.target.value)} rows={2} placeholder="Comment" aria-label="Comment" className="min-h-0" />
        <div className="flex justify-end"><Button size="sm" disabled={!text.trim() || post.isPending} onClick={() => post.mutate()}>Comment</Button></div>
      </div>
    </div>
  )
}
