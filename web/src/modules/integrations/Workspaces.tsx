import { useQuery } from "@tanstack/react-query"
import { BrandIcon } from "@/components/brand"
import { StatusDot } from "@/components/common"
import { QueryState } from "@/components/QueryState"
import { api } from "@/lib/api"
import type { Install, InstallStatus } from "./types"

const PLATFORM = { discord: "Discord", slack: "Slack", teams: "Microsoft Teams" }
const STATUS: Record<InstallStatus, { label: string; dot: "healthy" | "failing" | "setup" }> = {
  connected: { label: "Connected", dot: "healthy" }, pending: { label: "Setting up", dot: "setup" },
  failed: { label: "Setup failed", dot: "failing" }, archived: { label: "Removed", dot: "setup" },
  disconnected: { label: "Disconnected", dot: "failing" },
}

export function WorkspacesTab() {
  const q = useQuery({ queryKey: ["integrations", "workspaces"], queryFn: () => api.get<{ workspaces: Install[] }>("/api/integrations/workspaces") })
  return <QueryState query={q} empty="No chat app is connected" isEmpty={(d) => !d.workspaces.length}>{(d) => <Installs rows={d.workspaces} />}</QueryState>
}

function Installs({ rows }: { rows: Install[] }) {
  return (
    <div className="divide-y rounded-lg border">
      {rows.map((w) => (
        <div key={`${w.platform}:${w.name}`} className="flex min-h-14 items-center gap-3 px-4 py-2.5">
          <BrandIcon brand={w.platform} className="size-5" />
          <div className="min-w-0 flex-1">
            <div className="truncate text-sm font-medium">{w.name}</div>
            <div className="text-xs text-muted-foreground">
              {PLATFORM[w.platform]}
              {w.teams.length > 0 && `, ${w.teams.map((t) => t.name ?? "Unnamed team").join(", ")}`}
            </div>
          </div>
          {w.problem && <span className="text-xs text-destructive">Sync failing</span>}
          <span className="text-sm"><StatusDot status={STATUS[w.status].dot} label={STATUS[w.status].label} /></span>
        </div>
      ))}
    </div>
  )
}
