import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"

import { api, qs } from "@/lib/api"
import type { DmExplain, DmGrant, DmPerson, DmRuleAdd, DmRuleChange, DmSettings, PlatformGroups } from "./types"

const KEY = ["admin", "dm"]

export const useDmSettings = () => useQuery({ queryKey: KEY, queryFn: () => api.get<DmSettings>("/api/admin/dm") })

export const usePlatformGroups = (enabled = true) =>
  useQuery({ queryKey: ["admin", "platform-groups"], queryFn: () => api.get<PlatformGroups>("/api/admin/platform-groups"), staleTime: 60_000, enabled })

/** Chat people whose stored name contains `q`. */
export const useDmPeople = (q: string) =>
  useQuery({
    queryKey: ["admin", "dm", "people", q],
    queryFn: () => api.get<DmPerson[]>(`/api/admin/dm/people${qs({ q })}`),
    placeholderData: keepPreviousData,
    enabled: q.length > 0,
  })

/** Every DM settings write answers with the whole settings, which replace the cached ones. */
export function useDmSave() {
  const qc = useQueryClient()
  const write = useMutation({
    mutationFn: (run: () => Promise<DmSettings>) => run(),
    onSuccess: (saved) => { qc.setQueryData(KEY, saved); toast("Saved") },
  })
  const go = (run: () => Promise<DmSettings>) => write.mutate(run)
  return {
    pending: write.isPending,
    setDefault: (agent: string | null) => go(() => api.patch("/api/admin/dm", { default_agent: agent })),
    add: (rule: DmRuleAdd) => go(() => api.post("/api/admin/dm/rules", rule)),
    change: (position: number, change: DmRuleChange) => go(() => api.patch(`/api/admin/dm/rules/${position}`, change)),
    remove: (position: number) => go(() => api.del(`/api/admin/dm/rules/${position}`)),
  }
}

/** Where a DM from `user`, in `groups`, would go. */
export const useDmExplain = () =>
  useMutation({ mutationFn: ({ user, groups }: { user: string; groups: string[] }) => api.get<DmExplain>(`/api/admin/dm/explain?${new URLSearchParams([["user", user], ...groups.map((g) => ["group", g])])}`) })

/** Who may DM an agent, in a few words. */
export function dmText(dm: DmGrant): string {
  if (!dm.on) return "Off"
  const n = dm.users.length + dm.groups.length
  return dm.who === "members" ? "Everyone" : dm.who === "listed" ? `${n} listed` : `Everyone except ${n}`
}
