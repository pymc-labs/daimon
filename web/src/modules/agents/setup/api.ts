import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"

import { api } from "@/lib/api"
import type { AgentKey, CodingKey, MintedCodingKey } from "../types"

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
