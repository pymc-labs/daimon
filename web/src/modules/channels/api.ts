import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import { ApiError, api } from "@/lib/api"
import { useEnvironments } from "@/modules/agents/library/api"
import type {
  ChannelChangeResult, ChannelDefaults, ChannelDetail, ChannelHistoryEntry, ChannelList, ChannelSettings,
} from "./types"

export const useChannels = () => useQuery({ queryKey: ["channels"], queryFn: () => api.get<ChannelList>("/api/channels") })
export const useChannel = (id: string) => useQuery({ queryKey: ["channels", id], queryFn: () => api.get<ChannelDetail>(`/api/channels/${id}`) })
export const useChannelSettings = (id: string) =>
  useQuery({ queryKey: ["channels", id, "settings"], queryFn: () => api.get<ChannelSettings>(`/api/channels/${id}/settings`) })
export const useChannelHistory = (id: string, enabled: boolean) =>
  useQuery({ queryKey: ["channels", id, "history"], queryFn: () => api.get<ChannelHistoryEntry[]>(`/api/channels/${id}/history`), enabled })
export const useChannelDefaults = () =>
  useQuery({ queryKey: ["channel-defaults"], queryFn: () => api.get<ChannelDefaults>("/api/channel-defaults") })

/** Sandbox names the workspace already uses, offered when typing. */
export function useSandboxNames() {
  const { data } = useChannels()
  return [...new Set([data?.default_environment.value, ...(data?.channels ?? []).map((c) => c.environment.value)].filter((v): v is string => !!v))].sort()
}

/** Sandboxes to pick from: the workspace's own list where the reader may see it, else the ones already in use. */
export function useSandboxChoices() {
  const used = useSandboxNames()
  const listed = useEnvironments()
  return listed.data ? listed.data.map((e) => e.name) : used
}

export type Outcome = { notes: string[]; error: string | null }

/** Asks whether to go ahead with what the server said needs a confirm (428). */
export type Confirmer = (message: string) => Promise<boolean>

/**
 * Save one section. The answer replaces the channel in the cache; a stale value (409) reloads it instead.
 * A 428 asks `confirm` and, when it agrees, resends the body with `confirm_open_network`.
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
    async run(method: "put" | "post", path: string, body: unknown, confirm?: Confirmer): Promise<Outcome> {
      try {
        let r: ChannelChangeResult
        try {
          r = await m.mutateAsync({ method, path, body })
        } catch (e) {
          if (!(e instanceof ApiError && e.status === 428 && confirm)) throw e
          if (!(await confirm(e.message))) return { notes: [], error: null }
          r = await m.mutateAsync({ method, path, body: { ...(body as object), confirm_open_network: true } })
        }
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

export type DefaultsKey = "agent" | "environment" | "participation" | "progress-plans"

/** Change one workspace default. The answer is every default as it is now; the directory reloads. */
export function useDefaultsSave() {
  const qc = useQueryClient()
  const m = useMutation({
    mutationFn: ({ key, body }: { key: DefaultsKey; body: unknown }) => api.put<ChannelDefaults>(`/api/channel-defaults/${key}`, body),
  })
  return {
    pending: m.isPending,
    async run(key: DefaultsKey, body: unknown, confirm?: Confirmer): Promise<Outcome> {
      const refresh = () => Promise.all([
        qc.invalidateQueries({ queryKey: ["channel-defaults"] }),
        qc.invalidateQueries({ queryKey: ["channels"] }),
      ])
      try {
        let r: ChannelDefaults
        try {
          r = await m.mutateAsync({ key, body })
        } catch (e) {
          if (!(e instanceof ApiError && e.status === 428 && confirm)) throw e
          if (!(await confirm(e.message))) return { notes: [], error: null }
          r = await m.mutateAsync({ key, body: { ...(body as object), confirm_open_network: true } })
        }
        qc.setQueryData(["channel-defaults"], r)
        await refresh()
        return { notes: [], error: null }
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
