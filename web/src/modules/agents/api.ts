import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useEffect, useRef } from "react"
import { toast } from "sonner"

import { api } from "@/lib/api"
import type {
  Agent, AgentConfig, AgentSummary, AgentVersion, Backend, Catalog, DataServerAgent, DataServerAgentPatch, DataServerScope, DataServerStatus,
  DataServerToolPut, DataServerTools, Draft, Effect, Skill, SkillFiles, Template,
} from "./types"

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

const nodeKey = (agentId: string, kind: string) => ["agents", agentId, "nodes", kind] as const
const nodePath = (agentId: string, kind: string) => `/api/agents/${agentId}/nodes/${kind}`

/** What a connection node of any kind knows of the agent. */
export const useNodeStatus = <T,>(agentId: string, kind: string, enabled = true) =>
  useQuery({ queryKey: nodeKey(agentId, kind), queryFn: () => api.get<T>(nodePath(agentId, kind)), enabled, retry: false })

/** Attach or detach a node; the agent detail lists its nodes, so it refreshes too. */
export function useNodeLink<T>(agentId: string, kind: string) {
  const qc = useQueryClient()
  const done = (s: T) => {
    qc.setQueryData(nodeKey(agentId, kind), s)
    void qc.invalidateQueries({ queryKey: ["agents", agentId], exact: true })
    void qc.invalidateQueries({ queryKey: ["agents", agentId, "draft"] })
    void qc.invalidateQueries({ queryKey: [...nodeKey(agentId, kind), "tools"] })
  }
  const fail = (e: Error) => toast.error(e.message)
  return {
    connect: useMutation({ mutationFn: () => api.post<T>(`${nodePath(agentId, kind)}/connect`, {}), onSuccess: done, onError: fail }),
    disconnect: useMutation({ mutationFn: () => api.post<T>(`${nodePath(agentId, kind)}/disconnect`, {}), onSuccess: done, onError: fail }),
  }
}

/** Connect a node of any kind, chosen when it is added. */
export function useConnectNode(agentId: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (kind: string) => api.post<unknown>(`${nodePath(agentId, kind)}/connect`, {}),
    onSuccess: (s, kind) => {
      qc.setQueryData(nodeKey(agentId, kind), s)
      void qc.invalidateQueries({ queryKey: ["agents", agentId], exact: true })
      void qc.invalidateQueries({ queryKey: ["agents", agentId, "draft"] })
    },
    onError: (e) => toast.error(e.message),
  })
}

const DATA = "data_server"

/** The data-server node: status (with the server's agent, tokens and activity), tools and the person's scopes. */
export const useDataServerStatus = (agentId: string, enabled = true) => useNodeStatus<DataServerStatus>(agentId, DATA, enabled)
export const useDataServerLink = (agentId: string) => useNodeLink<DataServerStatus>(agentId, DATA)
export const useDataServerTools = (agentId: string, enabled = true) =>
  useQuery({ queryKey: [...nodeKey(agentId, DATA), "tools"], queryFn: () => api.get<DataServerTools>(`${nodePath(agentId, DATA)}/tools`), enabled })
export const useDataServerScopes = (agentId: string, enabled = true) =>
  useQuery({ queryKey: [...nodeKey(agentId, DATA), "scopes"], queryFn: () => api.get<DataServerScope[]>(`${nodePath(agentId, DATA)}/scopes`), enabled, staleTime: 60_000 })

/** Edit the server's agent, its tools and tokens; each answer replaces the cached value it changes. */
export function useDataServerEdit(agentId: string) {
  const qc = useQueryClient()
  const key = nodeKey(agentId, DATA)
  const path = nodePath(agentId, DATA)
  const fail = (e: Error) => toast.error(e.message)
  return {
    update: useMutation({
      mutationFn: (b: DataServerAgentPatch) => api.patch<DataServerAgent>(`${path}/agent`, b),
      // The answer is the list form: keep the detail's tokens and activity.
      onSuccess: (a) => {
        qc.setQueryData<DataServerStatus>(key, (s) => s && s.agent ? { ...s, agent: { ...s.agent, ...a, tokens: s.agent.tokens, activity: s.agent.activity } } : s)
        void qc.invalidateQueries({ queryKey: [...key, "tools"] })
      },
      onError: fail,
    }),
    setTool: useMutation({
      mutationFn: (b: DataServerToolPut) => api.put<DataServerTools>(`${path}/tools`, b),
      onSuccess: (t) => qc.setQueryData([...key, "tools"], t),
      onError: fail,
    }),
    revokeToken: useMutation({
      mutationFn: (tokenId: string) => api.del<unknown>(`${path}/tokens/${encodeURIComponent(tokenId)}`),
      onSuccess: () => qc.invalidateQueries({ queryKey: key, exact: true }),
      onError: fail,
    }),
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
  // Connection nodes change on their own server, which applies it on the next call.
  if (node === "agent" || node === "places" || node === "node") return "live"
  if (backend !== "anthropic") return "threads"
  return ["instructions", "model", "skills", "skill"].includes(node.split(":")[0]!) ? "session" : "live"
}
