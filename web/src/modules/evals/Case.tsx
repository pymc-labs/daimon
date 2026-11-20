import { useMutation, useQueryClient } from "@tanstack/react-query"
import { Flag } from "lucide-react"
import { useEffect, useState } from "react"
import { useNavigate, useParams } from "react-router"

import { QueryState } from "@/components/QueryState"
import { Page } from "@/components/shell/Page"
import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/textarea"
import { api } from "@/lib/api"
import { useParam } from "@/lib/filters"
import { E, useCase, useSuites } from "./api"
import { canEdit } from "./bits"
import { Expectations, Preview, Provenance, Script, World } from "./CaseParts"
import type { Case, CaseCheck } from "./types"

const TABS = [["preview", "Preview"], ["world", "World"], ["script", "Script"], ["expect", "Expectations"], ["provenance", "Provenance"], ["json", "JSON"]] as const

/** The smallest case that passes validation. */
const blank = (id: string): Case => ({
  id, revision: 1, title: "New case", fidelity: { level: "authored" }, provenance: { kind: "authored" },
  surface: { platform: "web", conversation: "dm", participants: [{ id: "u1", display_name: "Requester", role: "member", is_requester: true }], bot: { id: "bot", display_name: "Assistant", is_bot: true } },
  clock: { now: new Date().toISOString() }, history: [],
  turn: { messages: [{ id: "m1", author: "u1", at: new Date().toISOString(), text: "" }], trigger: "dm" }, world: {}, expect: {},
})

function JsonEditor({ suite, c, editable }: { suite: string; c: Case; editable: boolean }) {
  const qc = useQueryClient()
  const go = useNavigate()
  const original = JSON.stringify(c, null, 2)
  const [text, setText] = useState(original)
  const [parsed, problem] = (() => { try { return [JSON.parse(text) as Case, null] as const } catch (e) { return [null, (e as Error).message] as const } })()
  const [checked, setChecked] = useState<CaseCheck | null>(null)
  useEffect(() => {
    setChecked(null)
    if (!parsed) return
    const t = setTimeout(() => api.post<CaseCheck>(`${E}/cases/validate`, parsed).then(setChecked, () => setChecked(null)), 400)
    return () => clearTimeout(t)
  }, [text])
  const save = useMutation({
    mutationFn: () => api.put(`${E}/suites/${suite}/cases/${parsed!.id}`, parsed),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: ["evals"] }); go(`/evals/suites/${suite}/cases/${parsed!.id}?tab=json`) },
  })
  const issues = problem ? [problem] : checked?.problems ?? []
  return (
    <div className="mx-auto max-w-[1040px] space-y-3">
      <Textarea value={text} onChange={(e) => setText(e.target.value)} readOnly={!editable} spellCheck={false} aria-label="Case JSON" className="min-h-[28rem] font-mono text-xs leading-5" />
      <div role="status" className="space-y-0.5 text-sm">
        {issues.map((p) => <p key={p} className="text-destructive">{p}</p>)}
        {checked?.warnings.map((w) => <p key={w} className="text-warning">{w}</p>)}
        {!issues.length && checked && <p className="text-success">Valid</p>}
      </div>
      {save.error && <p role="alert" className="text-sm text-destructive">{save.error.message}</p>}
      {editable && (
        <div className="flex justify-end gap-2">
          <Button variant="outline" size="sm" disabled={text === original} onClick={() => setText(original)}>Revert</Button>
          <Button size="sm" disabled={!!issues.length || text === original || save.isPending} onClick={() => save.mutate()}>Save</Button>
        </div>
      )}
    </div>
  )
}

function Flags({ suite, c }: { suite: string; c: Case }) {
  const qc = useQueryClient()
  const open = c.review_flags?.filter((f) => !f.cleared_at) ?? []
  const clear = useMutation({
    mutationFn: (reason: string) => api.put(`${E}/suites/${suite}/cases/${c.id}`, { ...c, review_flags: c.review_flags?.map((f) => (f.reason === reason ? { ...f, cleared_at: new Date().toISOString() } : f)) }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["evals"] }),
  })
  if (!open.length) return null
  return (
    <ul className="mx-auto mb-4 max-w-[1040px] space-y-1">
      {open.map((f) => (
        <li key={f.reason} className="flex min-h-9 items-center gap-2 rounded-lg border border-warning/40 bg-warning/5 px-3 py-1 text-sm">
          <Flag className="size-4 shrink-0 text-warning" /><span className="min-w-0 flex-1">{f.reason.replace(/_/g, " ").replace(/^./, (x) => x.toUpperCase())}{f.detail && <span className="text-muted-foreground"> · {f.detail}</span>}</span>
          {clear.error && <span className="text-xs text-destructive">{clear.error.message}</span>}
          <Button variant="outline" size="sm" disabled={clear.isPending} onClick={() => clear.mutate(f.reason)}>Clear</Button>
        </li>
      ))}
    </ul>
  )
}

export function CasePage() {
  const { suite = "", case: id = "" } = useParams()
  const [tab] = useParam("tab")
  const fresh = id === "new"
  const query = useCase(suite, fresh ? "" : id)
  const mine = useSuites().data?.find((s) => s.id === suite)
  const role = mine?.role ?? "viewer"
  const active = fresh ? "json" : tab || "preview"
  const body = (c: Case) => {
    switch (active) {
      case "world": return <World c={c} />
      case "script": return <Script c={c} />
      case "expect": return <Expectations c={c} />
      case "provenance": return <Provenance c={c} />
      case "json": return <JsonEditor key={c.hash ?? c.id} suite={suite} c={c} editable={canEdit(role)} />
      default: return <Preview c={c} />
    }
  }
  const crumbs = (title: string) => [{ label: "Evals", to: "/evals" }, { label: mine?.name ?? suite, to: `/evals/suites/${suite}` }, { label: title }]
  if (fresh) return <Page crumbs={crumbs("New case")}><JsonEditor suite={suite} c={blank("case-1")} editable /></Page>
  return (
    <Page
      crumbs={crumbs(query.data?.title ?? id)}
      tabs={TABS.map(([key, label]) => ({ label, to: `?tab=${key}`, active: active === key }))}
    >
      <QueryState query={query}>
        {(c) => <><Flags suite={suite} c={c} />{body(c)}</>}
      </QueryState>
    </Page>
  )
}
