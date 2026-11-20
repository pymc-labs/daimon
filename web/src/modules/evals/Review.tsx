import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useEffect, useRef, useState } from "react"
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
import { E } from "./api"
import { Comments } from "./Comments"
import { ItemColumn } from "./Item"
import { ChatThread, fromMessages } from "./previews"
import type { Anchor, CaseView, Criterion, ReviewTask, ScoreDraft, ScoreValue, TaskColumn } from "./types"

type Option = { label: string; value: ScoreValue; keys: string[] }
const optionsOf = (c: Criterion): Option[] => {
  if (c.kind === "binary") return [{ label: "Pass", value: { pass: true }, keys: ["1", "p"] }, { label: "Fail", value: { pass: false }, keys: ["2", "f"] }]
  if (c.kind === "overall") return [{ label: "Yes", value: { pass: true }, keys: ["1", "y"] }, { label: "No", value: { pass: false }, keys: ["2", "n"] }]
  if (c.kind === "preference") return [{ label: "A", value: { preference: "a" }, keys: ["1", "a"] }, { label: "B", value: { preference: "b" }, keys: ["2", "b"] }, { label: "Tie", value: { preference: "tie" }, keys: ["3", "t"] }]
  const { min, max } = c.scale ?? { min: 1, max: 5 }
  return Array.from({ length: max - min + 1 }, (_, i) => ({ label: String(min + i), value: { score: min + i }, keys: i < 9 ? [String(i + 1)] : [] }))
}
const same = (a: ScoreValue | undefined, b: ScoreValue) => !!a && a.pass === b.pass && a.score === b.score && a.preference === b.preference

/** One question about one answer: a preference and a lone answer's questions have no column. */
type Slot = { k: Criterion; target?: string }
const slotsOf = (criteria: Criterion[], columns: TaskColumn[]): Slot[] =>
  criteria.flatMap((k) => (k.kind === "preference" || columns.length < 2 ? [{ k }] : columns.map((c) => ({ k, target: c.label }))))
const keyOf = (s: Slot) => `${s.k.id}|${s.target ?? ""}`

const SHORTCUTS: [string, string][] = [["1 to 9, or the letter on a button", "Choose an answer"], ["J, K", "Next, previous question"], ["Ctrl+Enter", "Submit and next"], ["?", "This list"]]

/** The case as a reviewer sees it: earlier messages and the reference, nothing else of the case. */
function CaseContext({ c }: { c: CaseView }) {
  return (
    <section className="space-y-2 rounded-lg border p-3 text-sm">
      <h2 className="font-medium">{c.title}</h2>
      {c.history.length > 0 && (
        <details>
          <summary className="cursor-pointer text-xs text-muted-foreground">Earlier messages</summary>
          <div className="pt-2"><ChatThread platform={c.platform} messages={fromMessages(c.history)} skin={{ people: c.participants }} /></div>
        </details>
      )}
      {c.reference?.text && <div><div className="text-xs font-medium text-muted-foreground">Reference</div><p className="whitespace-pre-wrap break-words">{c.reference.text}</p></div>}
    </section>
  )
}

function Form({ task }: { task: ReviewTask }) {
  const qc = useQueryClient()
  const slots = slotsOf(task.criteria, task.columns)
  const [answers, setAnswers] = useState<Record<string, ScoreValue>>({})
  const [active, setActive] = useState(0)
  const [note, setNote] = useState("")
  const [anchor, setAnchor] = useState<Anchor | null>(null)
  const [help, setHelp] = useState(false)
  const panel = useRef<HTMLElement>(null)
  useEffect(() => panel.current?.focus(), [])
  const complete = slots.length > 0 && slots.every((s) => answers[keyOf(s)])
  const submit = useMutation({
    mutationFn: () => api.post(`${E}/scores`, {
      task_id: task.id,
      scores: slots.map((s): ScoreDraft => ({ criterion: s.k.id, value: answers[keyOf(s)]!, target: s.target, rationale: note || undefined })),
    }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["evals", "next", task.queue] }).then(() => qc.invalidateQueries({ queryKey: ["evals", "queues"] })),
  })
  const pick = (i: number, v: ScoreValue) => { setAnswers((a) => ({ ...a, [keyOf(slots[i]!)]: v })); setActive(Math.min(i + 1, slots.length - 1)) }
  // Keys act only while focus is inside the answer panel, never in the note, and never with a modifier except to submit.
  const onKey = (e: React.KeyboardEvent) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { if (complete && !submit.isPending) submit.mutate(); return }
    if (e.ctrlKey || e.metaKey || e.altKey || (e.target as HTMLElement).closest("textarea,input")) return
    const k = e.key.toLowerCase()
    if (k === "j" || k === "k") setActive((i) => Math.max(0, Math.min(slots.length - 1, i + (k === "j" ? 1 : -1))))
    else if (k === "?") setHelp(true)
    else { const o = slots[active] && optionsOf(slots[active].k).find((x) => x.keys.includes(k)); if (o) pick(active, o.value) }
  }
  const status = slots[active] ? `Question ${active + 1} of ${slots.length}: ${slots[active]!.k.question}${slots[active]!.target ? `, answer ${slots[active]!.target}` : ""}` : ""
  return (
    <div className="flex min-h-0 flex-1 flex-col lg:flex-row">
      <div className="min-w-0 flex-1 space-y-4 overflow-y-auto p-4 lg:p-6">
        <CaseContext c={task.case} />
        <div className={cn("grid gap-4", task.layout === "side_by_side" && task.columns.length > 1 && "xl:grid-cols-2")}>
          {task.columns.map((col) => <ItemColumn key={col.label} label={col.label} title={col.arm?.label} item={col.item} blind={task.blind} platform={task.case.platform} case={task.case} anchorRef={col.label} onPin={setAnchor} />)}
        </div>
      </div>
      <aside ref={panel} tabIndex={-1} onKeyDown={onKey} aria-label="Review" className="flex w-full shrink-0 flex-col border-t outline-none lg:w-96 lg:border-l lg:border-t-0">
        <div className="flex-1 space-y-5 overflow-y-auto p-4">
          <div className="flex items-center gap-2 text-sm">
            <span className="tabular text-muted-foreground">{task.done + 1} of {task.total}</span>
            {task.blind && <Pill>Blind</Pill>}
            <Button variant="ghost" size="sm" className="ml-auto" onClick={() => setHelp(true)}>Shortcuts <Kbd>?</Kbd></Button>
          </div>
          <div role="status" className="sr-only">{status}</div>
          {!slots.length && <p role="alert" className="text-sm text-destructive">This task has no questions to answer. Ask an editor.</p>}
          {task.criteria.map((k) => (
            <fieldset key={k.id} className="space-y-2 rounded-lg border p-3">
              <legend className="sr-only">{k.question}</legend>
              <p className="text-sm">{k.question}</p>
              {slots.map((s, i) => s.k.id === k.id && (
                <div key={keyOf(s)} role="group" aria-label={s.target ? `Answer ${s.target}` : k.question} aria-current={i === active ? "true" : undefined} onFocus={() => setActive(i)}
                  className={cn("flex flex-wrap items-center gap-1.5 rounded-md p-1", i === active && "bg-accent/40")}>
                  {s.target && <span className="w-5 text-xs font-semibold text-muted-foreground">{s.target}</span>}
                  {optionsOf(k).map((o) => (
                    <Button key={o.label} size="sm" variant={same(answers[keyOf(s)], o.value) ? "default" : "outline"} aria-pressed={same(answers[keyOf(s)], o.value)} onClick={() => pick(i, o.value)}>
                      {o.label}{o.keys[0] && <Kbd className="ml-1 bg-transparent">{o.keys[0]}</Kbd>}
                    </Button>
                  ))}
                </div>
              ))}
            </fieldset>
          ))}
          <Textarea value={note} onChange={(e) => setNote(e.target.value)} rows={2} placeholder="Note" aria-label="Note" className="min-h-0" />
          <section className="space-y-2"><h3 className="text-xs font-medium text-muted-foreground">Comments</h3><Comments scope={{ task: task.id, initial: task.comments }} anchor={anchor} onClear={() => setAnchor(null)} /></section>
        </div>
        {submit.error && <p role="alert" className="px-4 pb-2 text-sm text-destructive">{submit.error.message}</p>}
        <div className="flex justify-end gap-2 border-t p-3">
          <Button size="sm" disabled={!complete || submit.isPending} onClick={() => submit.mutate()}>Submit and next</Button>
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
