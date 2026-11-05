import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"

import { api } from "@/lib/api"
import type { AgentKey, AgentToolRule, AgentToolRules, CodingKey, ConnectBatch, ConnectStatus, GoogleBinding, KnowledgeStores, MintedCodingKey, PersonRef } from "../types"

const fail = (e: Error) => toast.error(e.message)

const keysKey = (id: string) => ["agents", id, "keys"] as const
const codingKey = (id: string) => ["agents", id, "coding-keys"] as const

/** The names of the agent's stored keys; `enabled` is whether the reader sees setup. */
export const useAgentKeys = (id: string, enabled: boolean) =>
  useQuery({ queryKey: keysKey(id), queryFn: () => api.get<AgentKey[]>(`/api/agents/${id}/keys`), enabled, retry: false })

/** Store a value under a name, or remove a name. The value is sent once and never read back. */
export function useAgentKeyWrites(id: string) {
  const qc = useQueryClient()
  const done = () => qc.invalidateQueries({ queryKey: keysKey(id) })
  return {
    save: useMutation({
      mutationFn: (b: { key: string; value: string }) => api.put<AgentKey>(`/api/agents/${id}/keys/${encodeURIComponent(b.key)}`, { value: b.value }),
      onSuccess: done,
      onError: fail,
    }),
    remove: useMutation({
      mutationFn: (key: string) => api.del<{ removed: boolean }>(`/api/agents/${id}/keys/${encodeURIComponent(key)}`),
      onSuccess: done,
      onError: fail,
    }),
  }
}

export const useCodingKeys = (id: string, enabled: boolean) =>
  useQuery({ queryKey: codingKey(id), queryFn: () => api.get<CodingKey[]>(`/api/agents/${id}/coding-keys`), enabled, retry: false })

/** Make or revoke a coding key. A made key's token is the answer to the request and held nowhere else. */
export function useCodingKeyWrites(id: string) {
  const qc = useQueryClient()
  const done = () => qc.invalidateQueries({ queryKey: codingKey(id) })
  return {
    make: useMutation({ mutationFn: () => api.post<MintedCodingKey>(`/api/agents/${id}/coding-keys`), onSuccess: done, onError: fail }),
    revoke: useMutation({ mutationFn: (keyId: string) => api.del(`/api/agents/${id}/coding-keys/${keyId}`), onSuccess: done, onError: fail }),
  }
}

/** The people an admin may pick as owners. */
export const useOwnerOptions = (id: string, enabled: boolean) =>
  useQuery({ queryKey: ["agents", id, "owner-options"], queryFn: () => api.get<PersonRef[]>(`/api/agents/${id}/owners/options`), enabled, retry: false })

/** Set the owner and co-owners as one set; the agent refetches with the new names. */
export function useSetOwners(id: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (b: { owner: string | null; co_owners: string[] }) => api.put(`/api/agents/${id}/owners`, b),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: ["agents"] }) },
    onError: fail,
  })
}

/** Retire an agent, or copy it under a new name; both are an admin's. */
export function useAgentLifecycle(id: string) {
  const qc = useQueryClient()
  const done = () => qc.invalidateQueries({ queryKey: ["agents"] })
  return {
    archive: useMutation({ mutationFn: () => api.post(`/api/agents/${id}/archive`), onSuccess: done, onError: fail }),
    copy: useMutation({ mutationFn: (name: string) => api.post<{ id: string; name: string }>(`/api/agents/${id}/copy`, { name }), onSuccess: done, onError: fail }),
  }
}

/** What the agent remembers, read-only; `enabled` is whether the reader sees setup. */
export const useKnowledge = (id: string, enabled: boolean) =>
  useQuery({ queryKey: ["agents", id, "knowledge"], queryFn: () => api.get<KnowledgeStores>(`/api/agents/${id}/knowledge`), enabled, retry: false })

export const useMemoryPaths = (id: string, store: string) =>
  useQuery({ queryKey: ["agents", id, "knowledge", store], queryFn: () => api.get<string[]>(`/api/agents/${id}/knowledge/paths?store=${encodeURIComponent(store)}`), enabled: !!store, retry: false })

export const useMemoryText = (id: string, store: string, path: string) =>
  useQuery({
    queryKey: ["agents", id, "knowledge", store, path],
    queryFn: () => api.get<{ path: string; content: string }>(`/api/agents/${id}/knowledge/content?store=${encodeURIComponent(store)}&path=${encodeURIComponent(path)}`),
    enabled: !!store && !!path,
    retry: false,
  })

/** The agent's rules for every channel; admins only. */
export const useAgentToolRules = (id: string, enabled: boolean) =>
  useQuery({ queryKey: ["agents", id, "tool-rules"], queryFn: () => api.get<AgentToolRules>(`/api/agents/${id}/tool-rules`), enabled, retry: false })

/** Set a rule, or clear it with a null rule. `expected_revision` is the rule as read. */
export function useSetAgentToolRule(id: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (b: Pick<AgentToolRule, "tool" | "rule" | "scope"> & { expected_revision: number }) => api.put<AgentToolRule>(`/api/agents/${id}/tool-rules`, b),
    onSettled: () => { void qc.invalidateQueries({ queryKey: ["agents", id, "tool-rules"] }) },
    onError: fail,
  })
}

export const useGoogleBinding = (id: string, enabled: boolean) =>
  useQuery({ queryKey: ["agents", id, "google"], queryFn: () => api.get<GoogleBinding | null>(`/api/agents/${id}/google`), enabled, retry: false })

/** One batch of connect prompts. A new batch can name the one it replaces, whose unused codes then die. */
export const useConnectBatch = (id: string) =>
  useMutation({ mutationFn: (replaces?: string) => api.post<ConnectBatch>(`/api/agents/${id}/coding-keys/connect`, replaces ? { replaces } : {}) })

/** Whether the batch was used, polled until it is or `live` ends (its codes expired). */
export function useConnectStatus(id: string, batch: string | undefined, live = true) {
  const qc = useQueryClient()
  return useQuery({
    queryKey: [...codingKey(id), "connect", batch],
    queryFn: async () => {
      const s = await api.get<ConnectStatus>(`/api/agents/${id}/coding-keys/connect/${batch}`)
      if (s.connected) void qc.invalidateQueries({ queryKey: codingKey(id), exact: true })
      return s
    },
    enabled: !!batch && live,
    refetchInterval: (q) => (q.state.data?.connected ? false : 3000),
    retry: false,
  })
}
