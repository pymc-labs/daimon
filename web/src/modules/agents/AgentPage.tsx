import { ReactFlowProvider } from "@xyflow/react"
import { Eye, History as HistoryIcon, MessagesSquare, Plug, Plus, Blocks, Sparkles } from "lucide-react"
import { useMemo } from "react"
import { Link, useParams } from "react-router"

import { useParam } from "@/lib/filters"
import { ErrorState } from "@/components/QueryState"
import { Page } from "@/components/shell/Page"
import { Button } from "@/components/ui/button"
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { ApiError } from "@/lib/api"
import { NotFound } from "@/pages/Status"
import { useAgent, useCatalog, useDraft, useDraftEditor } from "./api"
import { Canvas } from "./Canvas"
import { DraftBar } from "./Draft"
import { Inspector } from "./inspector/Inspector"

export function AgentPage() {
  const { id = "" } = useParams()
  const query = useAgent(id)
  const { data: agent, error } = query
  const { data: draft } = useDraft(id, agent?.can_edit === true)
  const { data: catalog } = useCatalog()
  const { config, update } = useDraftEditor(agent, draft)
  const [node, setNode] = useParam("node")
  const changes = useMemo(() => draft?.changes ?? [], [draft])
  const changed = useMemo(() => new Set(changes.map((c) => c.key)), [changes])
  const changedNodes = useMemo(() => new Set(changes.map((c) => c.node)), [changes])
  if (error instanceof ApiError && error.status === 404) return <NotFound />
  const ro = !agent?.can_edit
  if (!agent && query.isError) return <Page crumbs={[{ label: "Agents", to: "/agents" }]}><ErrorState error={error} onRetry={() => void query.refetch()} /></Page>
  return (
    <Page
      crumbs={[{ label: "Agents", to: "/agents" }, { label: config?.name ?? "" }]}
      flush
      className="relative overflow-hidden bg-muted/40"
      actions={agent && (
        <>
          {ro && (
            <Tooltip>
              <TooltipTrigger asChild>
                <span className="inline-flex h-7 items-center gap-1.5 rounded-md border px-2 text-xs text-muted-foreground"><Eye className="size-3.5" />View only</span>
              </TooltipTrigger>
              <TooltipContent>Ask {agent.owner.name}</TooltipContent>
            </Tooltip>
          )}
          {!ro && (
            <DropdownMenu>
              <DropdownMenuTrigger asChild><Button variant="outline" size="sm"><Plus />Add</Button></DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="w-44">
                <DropdownMenuItem onSelect={() => setNode("skills")}><Sparkles />Skill</DropdownMenuItem>
                <DropdownMenuItem onSelect={() => setNode("mcp:new")}><Plug />MCP server</DropdownMenuItem>
                <DropdownMenuItem onSelect={() => setNode("places")}><Blocks />Channel</DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          )}
          <Button variant={node === "history" ? "secondary" : "outline"} size="sm" onClick={() => setNode(node === "history" ? null : "history")}>
            <HistoryIcon />History
          </Button>
          <Button variant="outline" size="sm" asChild><Link to={`/chat?agent=${agent.id}`}><MessagesSquare />Chat</Link></Button>
        </>
      )}
    >
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
