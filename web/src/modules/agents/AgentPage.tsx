import { useQueryClient } from "@tanstack/react-query"
import { ReactFlowProvider } from "@xyflow/react"
import { Eye, History as HistoryIcon, MessagesSquare, Plug, Plus, Blocks, ScanEye, SlidersHorizontal, Sparkles, Users } from "lucide-react"
import { useMemo } from "react"
import { Link, useParams } from "react-router"

import { useParam } from "@/lib/filters"
import { ErrorState } from "@/components/QueryState"
import { Page } from "@/components/shell/Page"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { ApiError } from "@/lib/api"
import { useCan } from "@/lib/hooks"
import { NotFound } from "@/pages/Status"
import { useAgent, useCatalog, useConnectNode, useDraft, useDraftEditor } from "./api"
import { Canvas } from "./Canvas"
import { useMonitoringState } from "./setup/useMonitoringState"
import { DraftBar } from "./Draft"
import { Inspector } from "./inspector/Inspector"
import { nodeId, nodeUi } from "./nodes/registry"
import { LifecycleMenu } from "./setup/LifecycleMenu"
import type { AgentSummary } from "./types"

/** The header actions keep their icon and drop the word on a phone, so the crumb stays readable. */
const ICON_ON_PHONE = "max-sm:w-7 max-sm:px-0"

/** The agent card's shape while a slow agent loads, so the page never looks empty. */
function Loading() {
  return (
    <div role="status" aria-label="Loading" className="flex h-full items-center justify-center bg-[radial-gradient(var(--border-strong)_1px,transparent_1px)] bg-size-[20px_20px] p-4 max-md:items-start">
      <div className="w-[272px] rounded-2xl border border-border-strong bg-card shadow-[0_6px_24px_rgb(12_31_64/0.08)]">
        <div className="flex items-center gap-3 p-4">
          <Skeleton className="size-11 rounded-lg bg-border" />
          <div className="flex-1 space-y-2"><Skeleton className="h-4 w-24 bg-border" /><Skeleton className="h-3 w-40 bg-border" /></div>
        </div>
        <div className="space-y-3 border-t px-4 py-3.5">
          {[28, 36, 20, 24].map((w) => <Skeleton key={w} className="h-3 bg-border" style={{ width: `${w * 4}px` }} />)}
        </div>
      </div>
    </div>
  )
}

export function AgentPage() {
  const { id = "" } = useParams()
  const can = useCan()
  const monitored = useMonitoringState(id, can("workspace.manage")).isSuccess
  const query = useAgent(id)
  const { data: agent, error } = query
  const { data: draft } = useDraft(id, agent?.can_edit === true)
  const { data: catalog } = useCatalog()
  const { config, update } = useDraftEditor(agent, draft)
  const connect = useConnectNode(id)
  const [node, setNode] = useParam("node")
  // Opened from the list: name the agent at once, while the rest loads.
  const listed = useQueryClient().getQueryData<AgentSummary[]>(["agents"])?.find((a) => a.id === id)
  const changes = useMemo(() => draft?.changes ?? [], [draft])
  const changed = useMemo(() => new Set(changes.map((c) => c.key)), [changes])
  const changedNodes = useMemo(() => new Set(changes.map((c) => c.node)), [changes])
  if (error instanceof ApiError && error.status === 404) return <NotFound />
  const ro = !agent?.can_edit
  if (!agent && query.isError) return <Page crumbs={[{ label: "Agents", to: "/agents" }]}><ErrorState error={error} onRetry={() => void query.refetch()} /></Page>
  return (
    <Page
      crumbs={[{ label: "Agents", to: "/agents" }, { label: config?.name ?? listed?.name ?? "" }]}
      flush
      className="relative overflow-hidden bg-muted/40"
      actions={agent && (
        <>
          {ro && (
            <Tooltip>
              <TooltipTrigger asChild>
                <span className="inline-flex h-7 items-center gap-1.5 rounded-md border px-2 text-xs text-muted-foreground"><Eye className="size-3.5" /><span className="max-sm:sr-only">View only</span></span>
              </TooltipTrigger>
              <TooltipContent>Ask {agent.owner.name}</TooltipContent>
            </Tooltip>
          )}
          {!ro && (
            <DropdownMenu>
              <DropdownMenuTrigger asChild><Button variant="outline" size="sm" className={ICON_ON_PHONE}><Plus /><span className="max-sm:sr-only">Add</span></Button></DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="w-44">
                <DropdownMenuItem onSelect={() => setNode("skills")}><Sparkles />Skill</DropdownMenuItem>
                <DropdownMenuItem onSelect={() => setNode("mcp:new")}><Plug />MCP server</DropdownMenuItem>
                {agent.nodes.filter((x) => x.server == null).map((x) => {
                  const Icon = nodeUi(x.kind).icon
                  return (
                    <DropdownMenuItem key={x.kind} disabled={connect.isPending}
                      onSelect={() => connect.mutate(x.kind, { onSuccess: () => setNode(nodeId(x.kind)) })}>
                      <Icon />{x.label}
                    </DropdownMenuItem>
                  )
                })}
                <DropdownMenuItem onSelect={() => setNode("places")}><Blocks />Channel</DropdownMenuItem>
                <DropdownMenuItem onSelect={() => setNode("helpers")}><Users />Helper</DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          )}
          <Button variant={node === "features" ? "secondary" : "outline"} size="sm" className={ICON_ON_PHONE} onClick={() => setNode(node === "features" ? null : "features")}>
            <SlidersHorizontal /><span className="max-sm:sr-only">Features</span>
          </Button>
          {monitored && (
            <Button variant={node === "monitoring" ? "secondary" : "outline"} size="sm" className={ICON_ON_PHONE} onClick={() => setNode(node === "monitoring" ? null : "monitoring")}>
              <ScanEye /><span className="max-sm:sr-only">Monitoring</span>
            </Button>
          )}
          <Button variant={node === "history" ? "secondary" : "outline"} size="sm" className={ICON_ON_PHONE} onClick={() => setNode(node === "history" ? null : "history")}>
            <HistoryIcon /><span className="max-sm:sr-only">History</span>
          </Button>
          <Button variant="outline" size="sm" asChild className={ICON_ON_PHONE}><Link to={`/chat?agent=${agent.id}`}><MessagesSquare /><span className="max-sm:sr-only">Chat</span></Link></Button>
          <LifecycleMenu agent={agent} />
        </>
      )}
    >
      {!agent && <Loading />}
      {agent && config && (
        <ReactFlowProvider>
          <Canvas
            agent={agent}
            config={config}
            catalog={catalog}
            changed={changed}
            changedNodes={changedNodes}
            selected={node}
            select={(n) => setNode(n || null)}
            ro={ro}
          />
          {node && (
            <Inspector
              node={node}
              agent={agent}
              config={config}
              catalog={catalog}
              ro={ro}
              changed={changed}
              update={update}
              select={(n) => setNode(n || null)}
            />
          )}
          {draft && !ro && <DraftBar agent={agent} draft={draft} shift={!!node} />}
        </ReactFlowProvider>
      )}
    </Page>
  )
}
