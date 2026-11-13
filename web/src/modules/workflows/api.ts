import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { toast } from "sonner"

import { ApiError, api, stream } from "@/lib/api"
import { ACTIVE_RUN, project } from "./runs"
import { diff, validate } from "./spec"
import type {
  AskReply, AttentionItem, Catalog, Draft, NewWorkflow, RunDetail, RunEvent, RunSummary, RunTest, Spec, StepDetail, Template, Version, Workflow,
  WorkflowPatch, WorkflowSummary,
} from "./types"

const base = (id: string) => `/api/workflows/${id}`
export const useWorkflows = () => useQuery({ queryKey: ["workflows"], queryFn: () => api.get<WorkflowSummary[]>("/api/workflows") })
export const useWorkflow = (id: string) => useQuery({ queryKey: ["workflows", id], queryFn: () => api.get<Workflow>(base(id)), enabled: !!id })
export const useDraft = (id: string, enabled = true) => useQuery({ queryKey: ["workflows", id, "draft"], queryFn: () => api.get<Draft>(`${base(id)}/draft`), enabled })
export const useVersions = (id: string) => useQuery({ queryKey: ["workflows", id, "versions"], queryFn: () => api.get<Version[]>(`${base(id)}/versions`) })
export const useRuns = (id: string) => useQuery({ queryKey: ["workflows", id, "runs"], queryFn: () => api.get<RunSummary[]>(`${base(id)}/runs`) })
export const useCatalog = () => useQuery({ queryKey: ["workflows", "catalog"], queryFn: () => api.get<Catalog>("/api/workflows/catalog"), staleTime: Infinity })
export const useTemplates = () => useQuery({ queryKey: ["workflows", "templates"], queryFn: () => api.get<Template[]>("/api/workflows/templates"), staleTime: Infinity })

export type Created = Workflow & { reply: string | null }
export function useCreate() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (b: NewWorkflow) => api.post<Created>("/api/workflows", b),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["workflows"], exact: true }),
  })
}

export function usePatch(id: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (b: WorkflowPatch) => api.patch<Workflow>(base(id), b),
    onSuccess: (w) => { qc.setQueryData(["workflows", id], w); void qc.invalidateQueries({ queryKey: ["workflows"], exact: true }) },
    onError: (e) => toast.error(e.message),
  })
}

export function useDelete() {
  const qc = useQueryClient()
  return useMutation({ mutationFn: (id: string) => api.del(base(id)), onSuccess: () => void qc.invalidateQueries({ queryKey: ["workflows"], exact: true }) })
}

export function useStartRun(id: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (b: { inputs: Record<string, unknown>; test?: RunTest | null }) => api.post<RunSummary>(`${base(id)}/runs`, b),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: ["workflows", id, "runs"] }); void qc.invalidateQueries({ queryKey: ["workflow-runs"] }) },
  })
}

// ---------- runs ----------

const runBase = (id: string) => `/api/workflows/runs/${id}`
export const useAllRuns = () => useQuery({ queryKey: ["workflow-runs"], queryFn: () => api.get<RunSummary[]>("/api/workflows/runs"), refetchInterval: 5000 })
export const useAttention = () => useQuery({ queryKey: ["workflow-runs", "attention"], queryFn: () => api.get<AttentionItem[]>("/api/workflows/attention") })
export const useRun = (wf: string, n: string) => useQuery({ queryKey: ["workflow-run", wf, n], queryFn: () => api.get<RunDetail>(`${base(wf)}/runs/${n}`), enabled: !!n })
export const useRunById = (id: string | null) => useQuery({ queryKey: ["workflow-run", id], queryFn: () => api.get<RunDetail>(runBase(id!)), enabled: !!id })
export function useStep(run: string | undefined, key: string | null, attempt: number | null, live: boolean) {
  return useQuery({
    queryKey: ["workflow-run", run, "step", key, attempt], enabled: !!run && !!key, refetchInterval: live ? 1500 : false, placeholderData: (p) => p,
    queryFn: () => api.get<StepDetail>(`${runBase(run!)}/steps/${encodeURIComponent(key!)}${attempt ? `?attempt=${attempt}` : ""}`),
  })
}

/**
 * The run as its ledger says, kept live: loads the rows, then follows the stream from the last seq.
 * `fresh` is the seq after which rows arrived live (they animate in). Call `follow` after an action reopens the run.
 */
export function useLiveRun(run: RunDetail | undefined) {
  const [log, setLog] = useState<{ id: string; rows: RunEvent[] } | null>(null)
  const [fresh, setFresh] = useState(Infinity)
  const [epoch, setEpoch] = useState(0)
  const rows = useRef<{ id?: string; list: RunEvent[] }>({ list: [] })
  const id = run?.id
  const events = log && log.id === id ? log.rows : null
  useEffect(() => {
    if (!id) return
    if (rows.current.id !== id) rows.current = { id, list: [] }
    const r = rows.current
    const ac = new AbortController()
    const add = (e: RunEvent) => {
      if (e.seq <= (r.list.at(-1)?.seq ?? 0)) return
      r.list = [...r.list, e]
      setLog({ id, rows: r.list })
    }
    void (async () => {
      if (!r.list.length) {
        r.list = await api.get<RunEvent[]>(`${runBase(id)}/events`)
        if (ac.signal.aborted) return
        setLog({ id, rows: r.list })
        setFresh(r.list.at(-1)?.seq ?? 0)
      }
      // A dropped stream resumes from the last seq it delivered.
      for (let tries = 0; tries < 5 && !ac.signal.aborted; tries++) {
        try {
          await stream(`${runBase(id)}/stream`, add, { signal: ac.signal, lastEventId: String(r.list.at(-1)?.seq ?? 0) })
          break
        } catch { await new Promise((r) => setTimeout(r, 1000 * (tries + 1))) }
      }
    })()
    return () => ac.abort()
  }, [id, epoch])
  const state = useMemo(() => (events ? project(events) : null), [events])
  const merged = useMemo(() => run && (state ? { ...run, ...state, events: events!.length, head_hash: events!.at(-1)?.hash ?? run.head_hash } : run), [run, state, events])
  const follow = useCallback(() => setEpoch((n) => n + 1), [])
  return { run: merged, events: events ?? [], fresh, follow }
}

/** Cancel, Retry from failure, decide an unknown step, approve, Run again and Run from here. */
export function useRunAction(id: string | undefined, after?: (r: RunSummary) => void) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ op, body }: { op: "cancel" | "redrive" | "decide" | "approve" | "rerun" | "fork"; body?: Record<string, unknown> }) =>
      api.post<RunSummary>(`${runBase(id!)}/${op}`, body ?? {}),
    onSuccess: (r) => {
      void qc.invalidateQueries({ queryKey: ["workflow-run"] })
      void qc.invalidateQueries({ queryKey: ["workflow-runs"] })
      void qc.invalidateQueries({ queryKey: ["workflows", r.workflow.id, "runs"] })
      after?.(r)
    },
    onError: (e) => toast.error(e.message),
  })
}

export function useVerify(run: RunDetail) {
  const ended = !ACTIVE_RUN.includes(run.status)
  return useQuery({
    queryKey: ["workflow-run", run.id, "verify", ended ? run.events : "live"], staleTime: Infinity,
    queryFn: () => api.get<{ ok: boolean; events: number }>(`${runBase(run.id)}/verify`),
  })
}

export const isActive = (r: { status: RunSummary["status"] }) => ACTIVE_RUN.includes(r.status)

/** A clock for live durations; ticks only while `on`. */
export function useNow(on: boolean, every = 1000) {
  const [now, setNow] = useState(Date.now)
  useEffect(() => {
    if (!on) return
    const t = setInterval(() => setNow(Date.now()), every)
    return () => clearInterval(t)
  }, [on, every])
  return now
}

/**
 * The draft as the canvas edits it. Edits apply at once (validated locally), then save in order;
 * each PUT carries the revision the last one returned, so a stale one gets a 409 and the draft reloads.
 */
export function useDraftEditor(wf: Workflow | undefined, draft: Draft | undefined) {
  const qc = useQueryClient()
  const key = ["workflows", wf?.id, "draft"]
  const rev = useRef<number | null>(null)
  const chain = useRef(Promise.resolve())
  const timer = useRef<ReturnType<typeof setTimeout>>(undefined)
  const pending = useRef<Spec | null>(null)
  useEffect(() => () => clearTimeout(timer.current), [])
  useEffect(() => { if (draft && rev.current == null) rev.current = draft.revision }, [draft])
  const live = wf?.live_version ? wf.spec : null

  const local = (spec: Spec): Draft => ({
    revision: rev.current ?? 0, base_version: wf?.live_version ?? null, spec, changes: diff(live, spec), validation: validate(spec),
    updated_at: new Date().toISOString(), updated_by: draft?.updated_by ?? { id: "", name: "" },
  })
  const flush = () => {
    const spec = pending.current
    if (!spec || !wf) return
    pending.current = null
    chain.current = chain.current.then(() => api.put<Draft>(`${base(wf.id)}/draft`, { base_revision: rev.current, spec }).then((d) => {
      rev.current = d.revision
      if (!pending.current) qc.setQueryData(key, d)
      void qc.invalidateQueries({ queryKey: ["workflows"], exact: true })
    }, (e) => {
      if (e instanceof ApiError && e.status === 409) {
        rev.current = null
        toast.error("Someone else changed this draft")
        void qc.invalidateQueries({ queryKey: key })
      } else toast.error(e.message)
    }))
  }
  const save = (spec: Spec, delay = 300) => {
    qc.setQueryData(key, local(spec))
    pending.current = spec
    clearTimeout(timer.current)
    timer.current = setTimeout(flush, delay)
  }
  /** Take a draft the server already saved (Ask Daimon, discard). */
  const adopt = (d: Draft) => { rev.current = d.revision; pending.current = null; clearTimeout(timer.current); qc.setQueryData(key, d) }
  return { save, adopt }
}

export const askDaimon = (id: string, text: string) => api.post<AskReply>(`${base(id)}/draft/ask`, { text })
export const discardDraft = (id: string) => api.del<Draft>(`${base(id)}/draft`)

export function usePublish(id: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (note: string) => api.post<Workflow>(`${base(id)}/draft/publish`, { note }),
    onSuccess: (w) => {
      qc.setQueryData(["workflows", id], w)
      void qc.invalidateQueries({ queryKey: ["workflows"] })
      toast(`Published v${w.live_version}`)
    },
    onError: (e) => toast.error(e.message),
  })
}
