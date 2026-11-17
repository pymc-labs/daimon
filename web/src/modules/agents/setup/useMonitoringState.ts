import { useQuery } from "@tanstack/react-query"

import { api } from "@/lib/api"
import type { AgentMonitoring } from "../../admin/types"

/** One agent's monitoring state, which only a workspace admin may read: a success means the panel is theirs to show. */
export function useMonitoringState(agentId: string, enabled = true) {
  return useQuery({
    queryKey: ["admin", "monitoring", agentId, "state"],
    queryFn: () => api.get<AgentMonitoring>(`/api/admin/monitoring/agents/${encodeURIComponent(agentId)}`),
    enabled,
    retry: false,
  })
}
