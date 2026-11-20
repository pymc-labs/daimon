import { useQuery } from "@tanstack/react-query"

import { api, qs } from "@/lib/api"
import { useMe } from "@/lib/hooks"
import type { Arm, BlindRun, Case, CaseSummary, CaseView, Compare, QueueView, Run, RunItem, RunItemSummary, RunSummary, Suite, SuiteRole, SuiteSummary } from "./types"

const E = "/api/evals"
const LIVE: Run["status"][] = ["approved", "running"]

export const blobUrl = (sha256: string) => `${E}/blobs/${sha256}`
export const blobText = (sha256: string) => fetch(blobUrl(sha256), { credentials: "include" }).then((r) => r.text())
export const isLive = (r?: Run | BlindRun) => !!r && LIVE.includes(r.status)

export const useSuites = () => useQuery({ queryKey: ["evals", "suites"], queryFn: () => api.get<SuiteSummary[]>(`${E}/suites`) })
/** The caller's role on a suite; the least until the list has loaded. */
export const useRole = (suite: string): SuiteRole => useSuites().data?.find((s) => s.id === suite)?.role ?? "viewer"
/** Owners and admins of the workspace: who may approve spend above the limit and see identities. */
export const useIsAdmin = () => { const role = useMe().data?.role; return role === "owner" || role === "admin" }
export const useSuite = (id: string, version?: number) =>
  useQuery({ queryKey: ["evals", "suite", id, version ?? "current"], queryFn: () => api.get<Suite>(`${E}/suites/${id}${qs({ version })}`) })
export const useCases = (id: string, version?: number) =>
  useQuery({ queryKey: ["evals", "cases", id, version ?? "current"], queryFn: () => api.get<CaseSummary[]>(`${E}/suites/${id}/cases${qs({ version })}`) })
/** The case, or for reviewers only the part of it they may see (`CaseView`). */
export const useCase = (suite: string, id: string) =>
  useQuery({ queryKey: ["evals", "case", suite, id], queryFn: () => api.get<Case | CaseView>(`${E}/suites/${suite}/cases/${id}`), enabled: !!id })
/** Only admins, editors and owners may list arms; the server refuses everyone else. */
export const useArms = (enabled: boolean) => useQuery({ queryKey: ["evals", "arms"], queryFn: () => api.get<Arm[]>(`${E}/arms`), enabled })
export const useRuns = (suite?: string) => useQuery({ queryKey: ["evals", "runs", suite ?? "all"], queryFn: () => api.get<Run[]>(`${E}/runs${qs({ suite })}`) })
/** A run: the full document, or the blind one for reviewers. */
export const useRun = (id: string) =>
  useQuery({ queryKey: ["evals", "run", id], queryFn: () => api.get<Run | BlindRun>(`${E}/runs/${id}`), refetchInterval: (q) => (isLive(q.state.data) ? 2000 : false) })
export const useRunItems = (id: string, live: boolean) =>
  useQuery({ queryKey: ["evals", "items", id], queryFn: () => api.get<RunItemSummary[]>(`${E}/runs/${id}/items?limit=500`), refetchInterval: live ? 2000 : false })
export const useSummary = (id: string, enabled: boolean, live: boolean) =>
  useQuery({ queryKey: ["evals", "summary", id], queryFn: () => api.get<RunSummary>(`${E}/runs/${id}/summary`), enabled, refetchInterval: live ? 4000 : false })
export const useCompare = (run: string, blind: boolean) =>
  useQuery({ queryKey: ["evals", "compare", run, blind], queryFn: () => api.get<Compare>(`${E}/runs/${run}/compare${qs({ blind: blind ? 1 : 0 })}`) })
/** One item with its chat, files and calls; blind when asked or when the server decides. */
export const useItem = (id: string, blind: boolean) =>
  useQuery({ queryKey: ["evals", "item", id, blind], queryFn: () => api.get<RunItem>(`${E}/items/${id}${qs({ blind: blind ? 1 : 0 })}`), staleTime: Infinity, enabled: !!id })
export const useQueues = () => useQuery({ queryKey: ["evals", "queues"], queryFn: () => api.get<QueueView[]>(`${E}/queues`) })

export { E }
