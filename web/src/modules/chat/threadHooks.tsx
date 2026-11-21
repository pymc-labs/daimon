import { useQuery } from "@tanstack/react-query"

import { api, ApiError } from "@/lib/api"
import { usd } from "@/lib/format"
import { useMe } from "@/lib/hooks"
import { Refusal } from "./Cards"
import type { SessionDetail } from "./types"
import { useThread } from "./turns"

/** Credit left, and the card that replaces the composer's promise when it is gone. */
export function useCredit() {
  const { data: me } = useMe()
  const out = !!me?.credit && me.credit.left_usd <= 0
  const card = me?.credit && (
    <Refusal card={{
      id: "out", kind: "refusal", title: "Your credit has run out.",
      body: `${usd(me.credit.total_usd)} used${me.credit.window === "month" ? " this month" : ""}.`,
      help: me.invited_by ? `Ask ${me.invited_by} to add more.` : "Ask an admin to add credit.",
    }} />
  )
  return { me, out, card, meter: me?.role === "visitor" && me.credit ? me.credit : null }
}

/** The saved thread: always fetched, and again on focus, since another tab or a restart may have saved a newer reply. */
export function useSessionDetail(id: string | undefined) {
  const { busy } = useThread(id)
  return useQuery({
    queryKey: ["chat", "session", id],
    queryFn: () => api.get<SessionDetail>(`/api/chat/sessions/${id}`),
    enabled: !!id,
    refetchOnWindowFocus: () => !busy,
    retry: (n, e) => !(e instanceof ApiError && e.status < 500) && n < 2,
  })
}
