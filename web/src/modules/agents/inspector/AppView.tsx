import { ExternalLink } from "lucide-react"
import { Link } from "react-router"
import { AppIcon } from "@/components/brand"
import { Button } from "@/components/ui/button"
import { useMe } from "@/lib/hooks"
import { AppPanel } from "@/modules/apps/AppFrame"
import type { Agent } from "../types"

/** An embedded app's view of this agent, in the inspector. */
export function AppView({ agent, appKey }: { agent: Agent; appKey: string }) {
  const app = useMe().data?.apps.find((a) => a.key === appKey)
  const panel = agent.apps.find((a) => a.key === appKey)
  if (!app || !panel) return null
  return (
    <div className="flex flex-1 flex-col">
      <div className="flex items-center gap-3 border-b px-5 py-4">
        <span className="flex size-9 items-center justify-center rounded-lg border bg-muted"><AppIcon icon={app.icon} className="size-5" /></span>
        <div className="min-w-0">
          <div className="truncate text-base font-semibold">{app.name}</div>
          <div className="truncate text-xs text-muted-foreground">{agent.name}</div>
        </div>
        {panel.page && (
          <Button variant="outline" size="sm" className="ml-auto" asChild>
            <Link to={`/apps/${app.key}${panel.page}`}>Open in {app.name}<ExternalLink /></Link>
          </Button>
        )}
      </div>
      <AppPanel app={app} path={panel.embed} className="flex-1" />
    </div>
  )
}
