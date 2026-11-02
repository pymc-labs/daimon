import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import { ApiError, api } from "@/lib/api"
import type { ChannelChangeResult, ChannelDetail, ChannelHistoryEntry, ChannelList } from "./types"

export const useChannels = () => useQuery({ queryKey: ["channels"], queryFn: () => api.get<ChannelList>("/api/channels") })
export const useChannel = (id: string) => useQuery({ queryKey: ["channels", id], queryFn: () => api.get<ChannelDetail>(`/api/channels/${id}`) })
export const useChannelHistory = (id: string, enabled: boolean) =>
  useQuery({ queryKey: ["channels", id, "history"], queryFn: () => api.get<ChannelHistoryEntry[]>(`/api/channels/${id}/history`), enabled })

export type Outcome = { notes: string[]; error: string | null }

/**
 * Save one section. The answer replaces the channel in the cache; a stale value (409) reloads it instead.
 * `run` resolves with the notes to show, or the label of the refusal.
 */
export function useSave(id: string) {
  const qc = useQueryClient()
  const m = useMutation({
    mutationFn: ({ method, path, body }: { method: "put" | "post"; path: string; body: unknown }) =>
      api[method]<ChannelChangeResult>(`/api/channels/${id}/${path}`, body),
  })
  const refresh = () => Promise.all([
    qc.invalidateQueries({ queryKey: ["channels", id] }),
    qc.invalidateQueries({ queryKey: ["channels"], exact: true }),
  ])
  return {
    pending: m.isPending,
    async run(method: "put" | "post", path: string, body: unknown): Promise<Outcome> {
      try {
        const r = await m.mutateAsync({ method, path, body })
        qc.setQueryData(["channels", id], r.channel)
        await refresh()
        return { notes: r.notes, error: null }
      } catch (e) {
        if (e instanceof ApiError && e.status === 409) {
          await refresh()
          return { notes: [], error: "Changed meanwhile. Reloaded." }
        }
        return { notes: [], error: e instanceof ApiError && e.message ? e.message : "Couldn't save" }
      }
    },
  }
}
