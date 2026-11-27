import { CircleAlert, TriangleAlert, X } from "lucide-react"

import { Button } from "@/components/ui/button"
import { useKey } from "@/lib/hooks"
import { cn } from "@/lib/utils"
import { PANEL } from "../Canvas"
import { problemsAt, titleOf } from "../spec"
import type { Validation } from "../types"
import { KIND, kindLabel } from "../kinds"
import { ActionEditor, AgentEditor, ApprovalEditor, BasicEditor, BranchEditor, FinishEditor, ForEachEditor, PostEditor, WaitEditor } from "./editors"
import { type EditorProps } from "./parts"
import { useLocal } from "./useLocal"

const EDITOR = {
  agent: AgentEditor, approval: ApprovalEditor, branch: BranchEditor, action: ActionEditor, wait: WaitEditor, for_each: ForEachEditor, finish: FinishEditor,
  transform: BasicEditor, code: BasicEditor, workflow: BasicEditor, post: PostEditor,
}

export function Inspector({ close, validation, ...p }: EditorProps & { close: () => void; validation?: Validation }) {
  useKey((e) => { if (e.key === "Escape") close() }, [close])
  const K = KIND[p.n.kind]
  const Editor = EDITOR[p.n.kind]
  const [title, setTitle] = useLocal(titleOf(p.id, p.n), (v) => p.set((x) => { x.title = v || undefined }, 500))
  const probs = problemsAt(validation, p.id)
  return (
    <aside aria-label="Step inspector" className={PANEL}>
      <div className="flex h-11 shrink-0 items-center gap-2 border-b px-3">
        <span className={cn("flex size-6 items-center justify-center rounded-md border bg-background", K.text)}><K.icon className="size-3.5" /></span>
        <span className="text-xs font-medium text-muted-foreground">{kindLabel(p.n)}</span>
        <Button variant="ghost" size="icon-sm" className="ml-auto" onClick={close} aria-label="Close"><X /></Button>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="border-b px-5 pt-4 pb-4">
          <input value={title} readOnly={p.ro} onChange={(e) => setTitle(e.target.value)} aria-label="Title"
            className="-mx-1.5 h-8 w-[calc(100%+12px)] rounded-md bg-transparent px-1.5 text-base font-semibold outline-none hover:bg-accent/60 focus:bg-background focus:ring-3 focus:ring-ring/15" />
          <span className="font-mono text-[11.5px] text-muted-foreground">{p.id}</span>
          {probs.length > 0 && (
            <ul className="mt-3 space-y-1.5">
              {probs.map((x, i) => {
                const Icon = x.severity === "error" ? CircleAlert : TriangleAlert
                return <li key={i} className={cn("flex items-start gap-2 text-sm", x.severity === "error" ? "text-destructive" : "text-warning")}><Icon className="mt-0.5 size-3.5 shrink-0" /><span className="text-foreground">{x.message}</span></li>
              })}
            </ul>
          )}
        </div>
        <Editor key={p.id} {...p} />
      </div>
    </aside>
  )
}
