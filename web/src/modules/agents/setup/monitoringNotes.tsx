import { Link } from "react-router"

import { absolute } from "@/lib/format"
import type { AgentMonitoring } from "../../admin/types"

const since = (when: string | null | undefined) => when && <div className="text-xs text-muted-foreground">Since {absolute(when)}</div>

/** What a workspace admin sees under the monitoring rows of the Features panel: when each switch went on, and the way to the activity it keeps. */
export function monitoringNotes(agentId: string, state: AgentMonitoring | undefined): Record<string, React.ReactNode> {
  if (!state) return {}
  return {
    monitor_direct_messages: since(state.direct_messages.since),
    monitor_mcp: (
      <>
        {since(state.coding_tools.since)}
        <Link className="text-xs underline underline-offset-2" to={`/admin/monitoring?agent=${encodeURIComponent(agentId)}`}>View activity</Link>
      </>
    ),
  }
}
