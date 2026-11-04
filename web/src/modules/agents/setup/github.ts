import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"

import { api } from "@/lib/api"
import type { SyncedSkill } from "../types"

export type GithubRepo = {
  name: string; url: string; access: "read" | "write"; working: boolean; shared: boolean
  status: "active" | "suspended" | "revoked"; installation: "active" | "suspended" | "removed"
  granted_at: string | null; granted_by: string | null
}
export type GithubSkill = {
  skill_id: string; title: string; version: string | null; origin: "repo" | "library" | "external" | "anthropic"
  repo: string | null; branch: string | null; path: string | null; url: string | null; updated_at: string | null
}
export type AgentGithub = {
  configured: boolean; can_manage: boolean
  repos: GithubRepo[]
  sync: { state: "pending" | "running" | "done"; at: string; error: string | null } | null
  skills: GithubSkill[]
  release: { name: string; at: string | null; url: string | null } | null
}

const fail = (e: Error) => toast.error(e.message)
const key = (id: string) => ["agents", id, "github"] as const
const base = (id: string) => `/api/agents/${id}/github`

/** The agent's GitHub link; `enabled` is whether the reader sees setup. */
export const useAgentGithub = (id: string, enabled: boolean) =>
  useQuery({ queryKey: key(id), queryFn: () => api.get<AgentGithub>(base(id)), enabled, retry: false })

/** What is wrong with a repo, in words; empty when it works. A paused or removed install explains the rest. */
export function repoTrouble(r: GithubRepo): string[] {
  if (r.installation === "suspended") return ["Install paused"]
  if (r.installation === "removed") return ["Install removed"]
  return r.status === "suspended" ? ["Paused"] : r.status === "revoked" ? ["Access revoked"] : []
}

/** Whether the link needs a look: a failed sync or a repo that does not work. */
export const hasTrouble = (g: AgentGithub) => !!g.sync?.error || g.repos.some((r) => repoTrouble(r).length > 0)

/** The owner's actions; each change answers with the whole link, which replaces the cached one. */
export function useGithubWrites(id: string) {
  const qc = useQueryClient()
  const set = (g: AgentGithub) => qc.setQueryData(key(id), g)
  return {
    connect: useMutation({
      mutationFn: () => api.post<{ url: string }>(`${base(id)}/connect`),
      onSuccess: ({ url }) => location.assign(url),
      onError: fail,
    }),
    setWorking: useMutation({ mutationFn: (repo: string) => api.put<AgentGithub>(`${base(id)}/working`, { repo }), onSuccess: set, onError: fail }),
    remove: useMutation({
      mutationFn: (repo: string) => api.del<AgentGithub>(`${base(id)}/repos/${repo.split("/").map(encodeURIComponent).join("/")}`),
      onSuccess: set,
      onError: fail,
    }),
    resync: useMutation({
      mutationFn: () => api.post<{ results: SyncedSkill[] }>(`${base(id)}/resync`),
      // The agent's prefix, so its skill list refreshes with the link.
      onSuccess: () => { void qc.invalidateQueries({ queryKey: ["agents", id] }) },
      onError: fail,
    }),
  }
}
