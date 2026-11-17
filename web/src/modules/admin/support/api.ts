import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import { ApiError, api, type Problem } from "@/lib/api"
import { channelLabel } from "@/lib/format"
import { useChannels } from "@/modules/channels/api"
import type { PersonHit, Place, SupportPage } from "./types"

/** Under `features`, so a feature change reloads the page's places too. */
export const KEY = ["features", "support"]

export const useSupportPage = (enabled = true) => useQuery({ queryKey: KEY, queryFn: () => api.get<SupportPage>("/api/support"), enabled, retry: false })

/** What a save answered: `fresh` is the page read again after a stale value (409), for the form to reload from. */
export type Outcome = { error: string | null; problems: Problem[]; fresh?: SupportPage }

/** Change a limit, menu or the request channel. The answer is the whole page; a stale value (409) reloads it. */
export function useSupportSave() {
  const qc = useQueryClient()
  const m = useMutation({ meta: { inline: true }, mutationFn: ({ path, body }: { path: string; body: unknown }) => api.put<SupportPage>(`/api/support/${path}`, body) })
  return {
    pending: m.isPending,
    async run(path: string, body: unknown): Promise<Outcome> {
      try {
        qc.setQueryData(KEY, await m.mutateAsync({ path, body }))
        return { error: null, problems: [] }
      } catch (e) {
        if (e instanceof ApiError && e.status === 409) {
          await qc.invalidateQueries({ queryKey: KEY })
          return { error: "Changed meanwhile. Reloaded.", problems: [], fresh: qc.getQueryData<SupportPage>(KEY) }
        }
        return { error: e instanceof ApiError && e.message ? e.message : "Couldn't save", problems: e instanceof ApiError ? e.problems : [] }
      }
    },
  }
}

/** The agents and channels of this platform, as places. */
export function usePlaces(d: SupportPage): Place[] {
  const { data } = useChannels()
  const channels = (data?.channels ?? []).filter((c) => c.kind === "channel" && c.platform === d.platform)
  return [
    ...d.agents.map((a): Place => ({ scope: "agent", id: a.id, name: a.name })),
    ...channels.map((c): Place => ({ scope: "channel", id: c.id, name: channelLabel(c.name, c.kind) })),
  ]
}

/** The first few people of the workspace's platform whose name or handle contains `q`, chat-only ones too. */
export const usePeopleFind = (q: string) =>
  useQuery({
    queryKey: ["support", "people", q],
    queryFn: () => api.get<PersonHit[]>(`/api/support/people?q=${encodeURIComponent(q)}`),
    enabled: !!q,
    placeholderData: keepPreviousData,
  })
