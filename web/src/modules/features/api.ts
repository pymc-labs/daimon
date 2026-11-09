import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import { ApiError, api, qs } from "@/lib/api"
import type { FeaturePlace, FeaturesDoc, FeatureSchema, FeatureView, Tier } from "./types"

const query = (p: FeaturePlace) => qs({ agent: p.agent, channel: p.channel, thread: p.thread })

export const useFeatureSchema = () =>
  useQuery({ queryKey: ["features", "schema"], queryFn: () => api.get<FeatureSchema[]>("/api/features/schema"), staleTime: Infinity })

export const useFeatures = (place: FeaturePlace) =>
  useQuery({ queryKey: ["features", place], queryFn: () => api.get<FeaturesDoc>(`/api/features${query(place)}`) })

export type Change = { key: string; tier: Tier; value: string; locked?: boolean; expected: string }

/** Change one tier of one key. Resolves to an error text, or null; a stale read (409) reloads instead. */
export function useFeatureSave(place: FeaturePlace) {
  const qc = useQueryClient()
  const m = useMutation({
    mutationFn: (c: Change) => api.put<FeatureView>(`/api/features${query(place)}`, c),
  })
  return {
    pending: m.isPending,
    async run(c: Change): Promise<string | null> {
      try {
        await m.mutateAsync(c)
        await qc.invalidateQueries({ queryKey: ["features"] })
        return null
      } catch (e) {
        if (e instanceof ApiError && e.status === 409) {
          await qc.invalidateQueries({ queryKey: ["features"] })
          return "Changed meanwhile. Reloaded."
        }
        return e instanceof ApiError && e.message ? e.message : "Couldn't save"
      }
    },
  }
}
