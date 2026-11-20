import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useState } from "react"
import { useHotkeys } from "react-hotkeys-hook"
import { Link, useParams } from "react-router"

import { Kbd } from "@/components/common"
import { QueryState } from "@/components/QueryState"
import { Page } from "@/components/shell/Page"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Skeleton } from "@/components/ui/skeleton"
import { Textarea } from "@/components/ui/textarea"
import { api } from "@/lib/api"
import { cn } from "@/lib/utils"
import { Pill } from "../agents/bits"
import { E, useCase } from "./api"
import { Comments } from "./Comments"
import { ItemColumn } from "./Item"
import type { Anchor, Criterion, ReviewTask, ScoreValue } from "./types"

type Option = { label: string; value: ScoreValue; keys: string[] }
const optionsOf = (c: Criterion): Option[] => {
  if (c.kind === "pass") return [{ label: "Pass", value: { pass: true }, keys: ["1", "p"] }, { label: "Fail", value: { pass: false }, keys: ["2", "f"] }]
  if (c.kind === "preference") return [{ label: "A", value: { preference: "a" }, keys: ["1", "a"] }, { label: "B", value: { preference: "b" }, keys: ["2", "b"] }, { label: "Tie", value: { preference: "tie" }, keys: ["3", "e"] }]
  return Array.from({ length: (c.max ?? 5) - (c.min ?? 1) + 1 }, (_, i) => ({ label: String((c.min ?? 1) + i), value: { score: (c.min ?? 1) + i }, keys: [String(i + 1)] }))
}
const same = (a: ScoreValue | undefined, b: ScoreValue) => !!a && a.pass === b.pass && a.score === b.score && a.preference === b.preference

const SHORTCUTS: [string, string][] = [["1 to 5, A, B, E, P, F", "Choose an answer"], ["Up, Down", "Move between criteria"], ["Ctrl+Enter", "Submit and next"], ["S", "Skip"], ["?", "This list"]]

function Form({ task }: { task: ReviewTask }) {
  const qc = useQueryClient()
  const [answers, setAnswers] = useState<Record<string, ScoreValue>>({})
  const [active, setActive] = useState(0)
  const [note, setNote] = useState("")
  const [anchor, setAnchor] = useState<Anchor | null>(null)
  const [help, setHelp] = useState(false)
  const c = useCase(task.suite_id, task.case_id)
  const complete = task.criteria.every((k) => answers[k.id])
  const done = () => void qc.invalidateQueries({ queryKey: ["evals", "next", task.queue_id] }).then(() => qc.invalidateQueries({ queryKey: ["evals", "queues"] }))
  const submit = useMutation({
    mutationFn: () => api.post(`${E}/scores`, { task_id: task.id, scores: task.criteria.map((k) => ({ criterion: k.id, value: answers[k.id], rationale: note || undefined })) }),
    onSuccess: done,
  })
  const skip = useMutation({ mutationFn: () => api.post(`${E}/scores`, { task_id: task.id, skip: true }), onSuccess: done })
  const pick = (i: number, v: ScoreValue) => { setAnswers((a) => ({ ...a, [task.criteria[i]!.id]: v })); setActive(Math.min(i + 1, task.criteria.length - 1)) }
  const key = (e: KeyboardEvent) => { const o = optionsOf(task.criteria[active]!).find((x) => x.keys.includes(e.key.toLowerCase())); if (o) { e.preventDefault(); pick(active, o.value) } }
  useHotkeys("1,2,3,4,5,6,7,8,9,a,b,e,p,f", key, { useKey: true }, [active, task])
  useHotkeys("up", (e) => { e.preventDefault(); setActive((i) => Math.max(0, i - 1)) }, [])
  useHotkeys("down", (e) => { e.preventDefault(); setActive((i) => Math.min(task.criteria.length - 1, i + 1)) }, [task])
  useHotkeys("s", () => skip.mutate(), [task])
  useHotkeys("shift+slash", () => setHelp((h) => !h), [])
  useHotkeys("mod+enter", () => { if (complete && !submit.isPending) submit.mutate() }, { enableOnFormTags: ["textarea"] }, [complete, answers, note])
  const busy = submit.isPending || skip.isPending
  return (
    <div className="flex min-h-0 flex-1 flex-col lg:flex-row">
      <div className="min-w-0 flex-1 overflow-y-auto p-4 lg:p-6">
        <div className={cn("grid gap-4", task.layout === "side_by_side" && task.columns.length > 1 && "xl:grid-cols-2")}>
          {task.columns.map((col) => <ItemColumn key={col.item.id} col={col} platform={c.data?.surface.platform ?? "web"} case={c.data} onPin={setAnchor} />)}
        </div>
      </div>
      <aside className="flex w-full shrink-0 flex-col border-t lg:w-96 lg:border-l lg:border-t-0">
        <div className="flex-1 space-y-5 overflow-y-auto p-4">
          <div className="flex items-center gap-2 text-sm">
            <span className="tabular text-muted-foreground">{task.progress.done + 1} of {task.progress.total}</span>
            {task.blind && <Pill>Blind</Pill>}
            <Button variant="ghost" size="sm" className="ml-auto" onClick={() => setHelp(true)}>Shortcuts <Kbd>?</Kbd></Button>
          </div>
          {task.criteria.map((k, i) => (
            <fieldset key={k.id} onFocus={() => setActive(i)} className={cn("space-y-2 rounded-lg border p-3", i === active && "border-border-strong bg-accent/30")}>
              <legend className="sr-only">{k.text}</legend>
              <p className="text-sm">{k.text}</p>
              <div className="flex flex-wrap gap-1.5">
                {optionsOf(k).map((o) => (
                  <Button key={o.label} size="sm" variant={same(answers[k.id], o.value) ? "default" : "outline"} aria-pressed={same(answers[k.id], o.value)} onClick={() => pick(i, o.value)}>
                    {o.label}<Kbd className="ml-1 bg-transparent">{o.keys[0]}</Kbd>
                  </Button>
                ))}
              </div>
            </fieldset>
          ))}
          <Textarea value={note} onChange={(e) => setNote(e.target.value)} rows={2} placeholder="Note" aria-label="Note" className="min-h-0" />
          <section className="space-y-2"><h3 className="text-xs font-medium text-muted-foreground">Comments</h3><Comments caseId={task.case_id} itemId={task.columns[0]?.item.id} anchor={anchor} onClear={() => setAnchor(null)} /></section>
        </div>
        {(submit.error ?? skip.error) && <p role="alert" className="px-4 pb-2 text-sm text-destructive">{(submit.error ?? skip.error)!.message}</p>}
        <div className="flex justify-end gap-2 border-t p-3">
          <Button variant="outline" size="sm" disabled={busy} onClick={() => skip.mutate()}>Skip</Button>
          <Button size="sm" disabled={!complete || busy} onClick={() => submit.mutate()}>Submit and next</Button>
        </div>
      </aside>
      <Dialog open={help} onOpenChange={setHelp}>
        <DialogContent className="sm:max-w-sm">
          <DialogHeader><DialogTitle>Shortcuts</DialogTitle></DialogHeader>
          <dl className="space-y-2 text-sm">{SHORTCUTS.map(([k, d]) => <div key={k} className="flex items-center justify-between gap-4"><dt className="text-muted-foreground">{d}</dt><dd><Kbd>{k}</Kbd></dd></div>)}</dl>
        </DialogContent>
      </Dialog>
    </div>
  )
}

export function Review() {
  const { queue = "" } = useParams()
  const next = useQuery({ queryKey: ["evals", "next", queue], queryFn: () => api.get<ReviewTask | null>(`${E}/queues/${queue}/next`), refetchOnWindowFocus: false })
  return (
    <Page crumbs={[{ label: "Evals", to: "/evals" }, { label: "Review" }]} flush className="flex flex-col">
      <QueryState query={next} skeleton={<Skeleton className="m-6 h-64" />} empty={<>Queue complete<br /><Link className="text-link hover:underline" to="/evals">Back to evals</Link></>} isEmpty={(t) => !t}>
        {(task) => task && <Form key={task.id} task={task} />}
      </QueryState>
    </Page>
  )
}
