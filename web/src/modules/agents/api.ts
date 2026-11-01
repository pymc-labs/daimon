import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useEffect, useRef } from "react"
import { toast } from "sonner"

import { api } from "@/lib/api"
import type { Agent, AgentConfig, AgentSummary, AgentVersion, Backend, Catalog, Draft, Effect, Skill, SkillFiles, Template } from "./types"

export const useAgents = () => useQuery({ queryKey: ["agents"], queryFn: () => api.get<AgentSummary[]>("/api/agents") })
export const useAgent = (id: string) => useQuery({ queryKey: ["agents", id], queryFn: () => api.get<Agent>(`/api/agents/${id}`) })
export const useDraft = (id: string, enabled = true) =>
  useQuery({ queryKey: ["agents", id, "draft"], queryFn: () => api.get<Draft | null>(`/api/agents/${id}/draft`), enabled })
export const useVersions = (id: string, enabled = true) =>
  useQuery({ queryKey: ["agents", id, "versions"], queryFn: () => api.get<AgentVersion[]>(`/api/agents/${id}/versions`), enabled })
export const useCatalog = () => useQuery({ queryKey: ["agents", "catalog"], queryFn: () => api.get<Catalog>("/api/agents/catalog"), staleTime: Infinity })
export const useTemplates = () => useQuery({ queryKey: ["agents", "templates"], queryFn: () => api.get<Template[]>("/api/agents/templates"), staleTime: Infinity })

/** The files of a custom skill at the version the config pins. */
export const useSkillFiles = (agentId: string, skill: Skill, enabled: boolean) =>
  useQuery({
    queryKey: ["agents", agentId, "skill-files", skill.skill_id, skill.version],
    queryFn: () => api.get<SkillFiles>(`/api/agents/${agentId}/skills/${skill.skill_id}/files?version=${encodeURIComponent(skill.version)}`),
    enabled,
    retry: false,
  })

/** Publish `files` as a new version of the skill (or a new skill when `skillId` is null); a null body keeps that file as `version` has it. */
export const saveSkillFiles = (agentId: string, skillId: string | null, files: Record<string, string | null>, version?: string) =>
  api.post<Skill>(`/api/agents/${agentId}/skills`, { skill_id: skillId, files, version })

/** Store or remove the token an agent's MCP server is called with. */
export function useServerToken(agentId: string, key: string) {
  const qc = useQueryClient()
  const done = () => { void qc.invalidateQueries({ queryKey: ["agents", agentId], exact: true }); void qc.invalidateQueries({ queryKey: ["agents", agentId, "draft"] }) }
  const fail = (e: Error) => toast.error(e.message)
  const path = `/api/agents/${agentId}/mcp/${encodeURIComponent(key)}/token`
  return {
    save: useMutation({ mutationFn: (token: string) => api.put(path, { token }), onSuccess: done, onError: fail }),
    remove: useMutation({ mutationFn: () => api.del(path), onSuccess: done, onError: fail }),
  }
}

/** The config being edited: the person's draft if any, else the current version. */
function configOf(agent: Agent): AgentConfig {
  const { name, purpose, avatar, model, system, skills, tools, mcp_servers, places } = agent
  return { name, purpose, avatar, model, system, skills, tools, mcp_servers, places }
}

/**
 * Edits go to the server-side draft. The cache updates at once; the PUT is debounced and
 * its answer (with the server's change list) lands only if no newer edit is pending.
 */
export function useDraftEditor(agent: Agent | undefined, draft: Draft | null | undefined) {
  const qc = useQueryClient()
  const key = ["agents", agent?.id, "draft"]
  const latest = useRef<AgentConfig | null>(null)
  const seq = useRef(0)
  const timer = useRef<ReturnType<typeof setTimeout>>(undefined)
  useEffect(() => () => clearTimeout(timer.current), [])
  const config = draft?.config ?? (agent ? configOf(agent) : null)

  const update = (fn: (c: AgentConfig) => void, delay = 250) => {
    if (!agent || !config) return
    const next = structuredClone(latest.current ?? config)
    fn(next)
    latest.current = next
    const base = draft?.base_version ?? agent.version
    qc.setQueryData<Draft | null>(key, (d) => ({ base_version: base, changes: d?.changes ?? [], stale: d?.stale ?? false, config: next }))
    const n = ++seq.current
    clearTimeout(timer.current)
    timer.current = setTimeout(() => {
      api.patch<Draft | null>(`/api/agents/${agent.id}/draft`, { base_version: base, config: next }).then((d) => {
        if (n !== seq.current) return
        latest.current = null
        qc.setQueryData(key, d)
        qc.invalidateQueries({ queryKey: ["agents"], exact: true })
      }, (e) => toast.error(e.message))
    }, delay)
  }
  return { config, update }
}

/** Agents the person may chat with change when an agent is created, applied or reverted. */
const chatAgentsStale = (qc: ReturnType<typeof useQueryClient>) => qc.invalidateQueries({ queryKey: ["chat", "agents"] })

export function useDiscard(id: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: () => api.del(`/api/agents/${id}/draft`),
    onSuccess: () => { qc.setQueryData(["agents", id, "draft"], null); qc.invalidateQueries({ queryKey: ["agents"], exact: true }) },
  })
}

export function useApply(id: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (note: string) => api.post<Agent>(`/api/agents/${id}/apply`, { note }),
    onSuccess: (a) => {
      qc.setQueryData(["agents", id], a)
      qc.setQueryData(["agents", id, "draft"], null)
      qc.invalidateQueries({ queryKey: ["agents", id, "versions"] })
      qc.invalidateQueries({ queryKey: ["agents"], exact: true })
      void chatAgentsStale(qc)
      toast(`Applied as v${a.version}`)
    },
    onError: (e) => toast.error(e.message),
  })
}

export function useRevert(id: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (version: number) => api.post<Agent>(`/api/agents/${id}/revert`, { version }),
    onSuccess: (a, v) => {
      qc.setQueryData(["agents", id], a)
      qc.invalidateQueries({ queryKey: ["agents", id] })
      qc.invalidateQueries({ queryKey: ["agents"], exact: true })
      void chatAgentsStale(qc)
      toast(`Reverted to v${v} as v${a.version}`)
    },
    onError: (e) => toast.error(e.message),
  })
}

/** What an edit to this node does once applied; mirrors the server's tagging. */
export function nodeEffect(node: string, backend: Backend): Effect {
  if (node === "agent" || node === "places") return "live"
  if (backend !== "anthropic") return "threads"
  return ["instructions", "model", "skills", "skill"].includes(node.split(":")[0]!) ? "session" : "live"
}
