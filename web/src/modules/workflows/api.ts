import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useEffect, useRef } from "react"
import { toast } from "sonner"

import { ApiError, api } from "@/lib/api"
import { diff, validate } from "./spec"
import type {
  AskReply, Catalog, Draft, NewWorkflow, RunSummary, Spec, Template, Version, Workflow, WorkflowPatch, WorkflowSummary,
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
    mutationFn: (b: { inputs: Record<string, unknown>; test?: boolean }) => api.post<RunSummary>(`${base(id)}/runs`, b),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["workflows", id, "runs"] }),
  })
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
