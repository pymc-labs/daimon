// Connection node kinds in the editor: a canvas card body and an inspector panel per kind.
// A new kind (a server the backend serves under /nodes/{kind}) adds one entry.
import { Database, Plug, Trash2, type LucideIcon } from "lucide-react"

import { Button } from "@/components/ui/button"
import { useNodeLink } from "../api"
import type { Agent, AgentNode } from "../types"
import { DataServerCard, DataServerPanel } from "./DataServer"

export type NodeCardProps = { agent: Agent; node: AgentNode }
export type NodePanelProps = { agent: Agent; node: AgentNode; ro: boolean; select: (id: string) => void }
export type NodeKindUi = { icon: LucideIcon; Card: React.ComponentType<NodeCardProps>; Panel: React.ComponentType<NodePanelProps> }

export const NODE_UI: Record<string, NodeKindUi> = {
  data_server: { icon: Database, Card: DataServerCard, Panel: DataServerPanel },
}

/** A kind this build has no entry for: its label, and Disconnect. */
const FALLBACK: NodeKindUi = {
  icon: Plug,
  Card: () => null,
  Panel: ({ agent, node, ro, select }) => {
    const link = useNodeLink<unknown>(agent.id, node.kind)
    if (ro) return null
    return (
      <div className="mt-auto border-t px-5 py-3">
        <Button variant="ghost" size="sm" className="text-muted-foreground hover:text-destructive" disabled={link.disconnect.isPending}
          onClick={() => link.disconnect.mutate(undefined, { onSuccess: () => select("") })}>
          <Trash2 />Disconnect
        </Button>
      </div>
    )
  },
}

export const nodeUi = (kind: string): NodeKindUi => NODE_UI[kind] ?? FALLBACK

/** Canvas and inspector id of a node. */
export const nodeId = (kind: string) => `node:${kind}`
