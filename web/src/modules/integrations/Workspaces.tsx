import { useQuery } from "@tanstack/react-query"
import { BrandIcon } from "@/components/brand"
import { StatusDot } from "@/components/common"
import { Drawer } from "@/components/Drawer"
import { Facts, PanelHead, PanelSection } from "@/components/panel"
import { QueryState } from "@/components/QueryState"
import { api } from "@/lib/api"
import { pressProps, useParam } from "@/lib/filters"
import type { Install, InstallStatus } from "./types"

const PLATFORM = { discord: "Discord", slack: "Slack", teams: "Microsoft Teams" }
const STATUS: Record<InstallStatus, { label: string; dot: "healthy" | "failing" | "setup" }> = {
  connected: { label: "Connected", dot: "healthy" }, pending: { label: "Setting up", dot: "setup" },
  failed: { label: "Setup failed", dot: "failing" }, archived: { label: "Removed", dot: "setup" },
  disconnected: { label: "Disconnected", dot: "failing" },
}

/** One status per install; a failing sync outranks the connection state. */
const statusOf = (w: Install) => (w.problem ? { label: "Sync failing", dot: "failing" as const } : STATUS[w.status])

export function WorkspacesTab() {
  const q = useQuery({ queryKey: ["integrations", "workspaces"], queryFn: () => api.get<{ workspaces: Install[] }>("/api/integrations/workspaces") })
  return <QueryState query={q} empty="No chat app is connected" isEmpty={(d) => !d.workspaces.length}>{(d) => <Installs rows={d.workspaces} />}</QueryState>
}

function Installs({ rows }: { rows: Install[] }) {
  const [, setApp] = useParam("app")
  return (
    <>
      <div className="divide-y rounded-lg border">
        {rows.map((w) => (
          <div key={w.platform} className="flex min-h-14 cursor-pointer items-center gap-3 px-4 py-2.5 hover:bg-accent/50" {...pressProps(() => setApp(w.platform))}>
            <BrandIcon brand={w.platform} className="size-5" />
            <div className="min-w-0 flex-1">
              <div className="truncate text-sm font-medium">{w.name}</div>
              <div className="truncate text-xs text-muted-foreground">
                {PLATFORM[w.platform]}
                {w.teams.length > 0 && `, ${w.teams.map((t) => t.name ?? "Unnamed team").join(", ")}`}
              </div>
            </div>
            <span className="text-sm"><StatusDot status={statusOf(w).dot} label={statusOf(w).label} /></span>
          </div>
        ))}
      </div>
      <Drawer param="app" title="Chat app" ids={rows.map((w) => w.platform)}>
        {(id) => {
          const w = rows.find((x) => x.platform === id)
          return w ? <Detail w={w} /> : null
        }}
      </Drawer>
    </>
  )
}

function Detail({ w }: { w: Install }) {
  const status = statusOf(w)
  return (
    <div>
      <PanelHead icon={<BrandIcon brand={w.platform} className="size-5" />} title={w.name} subtitle={PLATFORM[w.platform]} />
      <PanelSection>
        <Facts rows={[
          ["Platform", PLATFORM[w.platform]],
          ["Status", <StatusDot key="status" status={status.dot} label={status.label} />],
          ["Connection", w.problem ? STATUS[w.status].label : null],
        ]} />
      </PanelSection>
      {w.teams.length > 0 && (
        <PanelSection title="Teams">
          <ul className="space-y-1.5 text-sm">
            {w.teams.map((t) => <li key={t.id}>{t.name ?? "Unnamed team"}</li>)}
          </ul>
        </PanelSection>
      )}
    </div>
  )
}
