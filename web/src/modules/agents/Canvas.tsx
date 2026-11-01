import "@xyflow/react/dist/style.css"

import {
  Background, BackgroundVariant, BaseEdge, type Edge, type EdgeProps, Handle, type Node, Position, ReactFlow, useEdgesState, useNodesState, useReactFlow,
  useStore,
} from "@xyflow/react"
import { Blocks, Cpu, Maximize, Minus, Plug, Plus, ScrollText, Sparkles, Wrench } from "lucide-react"
import { createContext, useContext, useEffect, useMemo, useRef, useState } from "react"

import { AgentAvatar, Ago, Avatar, Money } from "@/components/common"
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { useTheme } from "@/lib/theme-context"
import { cn } from "@/lib/utils"
import { pressProps } from "@/lib/filters"
import { useConnectNode } from "./api"
import { Dot, Locked, Pill, PlaceIcon, RuleChip } from "./bits"
import { layoutNarrow, layoutWide, type Dep, type Plan, type Pt, type Size } from "./layout"
import { EFFORT_LABEL, placeName } from "./lib"
import { nodeId, nodeUi } from "./nodes/registry"
import type { Agent, AgentConfig, AgentNode as ConnectionNode, Catalog } from "./types"

type Ctx = {
  agent: Agent
  config: AgentConfig
  catalog?: Catalog
  changed: Set<string>
  changedNodes: Set<string>
  selected: string
  select: (id: string) => void
  ro: boolean
}
const CanvasCtx = createContext<Ctx>(null!)
const useCanvas = () => useContext(CanvasCtx)

// ---------- Nodes ----------

function Card({ id, icon, title, count, children, className, action }: {
  id: string; icon: React.ReactNode; title: string; count?: React.ReactNode; children?: React.ReactNode; className?: string; action?: React.ReactNode
}) {
  const { selected, select, changedNodes } = useCanvas()
  const on = selected === id || (id === "skills" && selected.startsWith("skill:"))
  return (
    <div
      {...pressProps(() => select(id))}
      aria-label={title}
      className={cn(
        "w-60 cursor-pointer rounded-xl border bg-card text-card-foreground shadow-[0_1px_3px_rgb(0_0_0/0.05)] transition-[border-color,box-shadow] hover:border-border-strong",
        on && "border-ring ring-3 ring-ring/15 hover:border-ring", className,
      )}
    >
      <Handle type="target" position={Position.Left} isConnectable={false} className="!pointer-events-none !opacity-0" />
      <Handle type="source" position={Position.Right} isConnectable={false} className="!pointer-events-none !opacity-0" />
      <div className={cn("flex h-10 items-center gap-2 px-3", children && "border-b")}>
        <span className="flex size-4 shrink-0 items-center justify-center text-muted-foreground [&_svg]:size-4">{icon}</span>
        <span className="truncate text-[13px] font-medium">{title}</span>
        {count != null && <span className="text-xs text-faint tabular">{count}</span>}
        {changedNodes.has(id) && <Dot />}
        <span className="ml-auto flex items-center">{action}</span>
      </div>
      {children}
    </div>
  )
}

function Row({ k, children, className }: { k: string; children: React.ReactNode; className?: string }) {
  const { select, selected, changed } = useCanvas()
  return (
    <div
      data-row={k}
      {...pressProps(() => select(k))}
      onClick={(e) => { e.stopPropagation(); select(k) }}
      className={cn("flex h-8 items-center gap-2 px-3 text-[13px] hover:bg-accent/70", selected === k && "bg-accent", className)}
    >
      {children}
      {changed.has(k) && <Dot className="ml-1" />}
    </div>
  )
}

function AgentNode() {
  const { agent, config, select, selected, changedNodes } = useCanvas()
  return (
    <div
      {...pressProps(() => select("agent"))}
      aria-label={config.name}
      className={cn(
        "w-[272px] cursor-pointer rounded-2xl border border-border-strong bg-card shadow-[0_6px_24px_rgb(12_31_64/0.08)] transition-[border-color,box-shadow]",
        selected === "agent" && "border-ring ring-3 ring-ring/15",
      )}
    >
      <Handle type="target" position={Position.Left} isConnectable={false} className="!pointer-events-none !opacity-0" />
      <Handle type="source" position={Position.Right} isConnectable={false} className="!pointer-events-none !opacity-0" />
      <div className="flex items-center gap-3 p-4">
        <AgentAvatar src={config.avatar} name={config.name} className="size-11 rounded-lg text-sm" />
        <div className="min-w-0">
          <div className="flex items-center gap-1.5">
            <span className="truncate text-[15px] font-semibold">{config.name}</span>
            {changedNodes.has("agent") && <Dot />}
          </div>
          <div className="line-clamp-2 text-xs text-muted-foreground">{config.purpose || "No description"}</div>
        </div>
      </div>
      <div className="grid grid-cols-[76px_1fr] items-center gap-y-2 border-t px-4 py-3 text-xs">
        <span className="text-muted-foreground">Version</span>
        <span className="flex items-center gap-1.5"><span className="font-medium tabular">v{agent.version}</span><Ago iso={agent.updated_at} className="text-muted-foreground" /></span>
        <span className="text-muted-foreground">Owner</span>
        <span className="flex min-w-0 items-center gap-1.5">
          <Avatar name={agent.owner.name} className="size-4 text-[8px]" /><span className="truncate">{agent.owner.name}</span>
          {agent.co_owners.length > 0 && <span className="text-faint">+{agent.co_owners.length}</span>}
        </span>
        <span className="text-muted-foreground">This month</span>
        <Money usd={agent.month_usd} />
        <span className="text-muted-foreground">Last used</span>
        <Ago iso={agent.last_used_at} />
      </div>
    </div>
  )
}

const words = (s: string) => s.split(/\s+/).filter(Boolean).length

function InstructionsNode() {
  const { config, agent } = useCanvas()
  const text = config.system?.split("\n").filter((l) => !l.startsWith("#")).join("\n").replace(/[*`]/g, "").replace(/^- /gm, "• ").replace(/\n{2,}/g, "\n").trim()
  return (
    <Card id="instructions" icon={<ScrollText />} title="Instructions">
      <div className="p-3">
        {config.system == null ? <Locked who={agent.owner.name} /> : text ? (
          <>
            <p className="line-clamp-5 text-xs leading-[18px] whitespace-pre-line text-muted-foreground">{text}</p>
            <div className="mt-2 text-[11px] text-faint tabular">{words(config.system)} words</div>
          </>
        ) : <span className="text-xs text-faint">Empty</span>}
      </div>
    </Card>
  )
}

const PROVIDER = { anthropic: "Anthropic", openai: "OpenAI", gemini: "Google", selfhosted: "Self-hosted" }

function ModelNode() {
  const { config, catalog } = useCanvas()
  const c = catalog?.models.find((x) => x.id === config.model.id)
  return (
    <Card id="model" icon={<Cpu />} title="Model">
      <div className="space-y-1 p-3">
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium">{c?.label ?? config.model.id}{config.model.speed === "fast" && " Fast"}</span>
          <Pill>{EFFORT_LABEL[config.model.effort]}</Pill>
        </div>
        <div className="text-xs text-muted-foreground">
          {PROVIDER[config.model.backend]}{c && <span className="text-faint">, ${c.in_usd} in / ${c.out_usd} out per million</span>}
        </div>
      </div>
    </Card>
  )
}

function PlacesNode() {
  const { config, agent } = useCanvas()
  return (
    <Card id="places" icon={<Blocks />} title="Channels" count={config.places?.length}>
      {config.places == null ? <Locked who={agent.owner.name} className="p-3" /> : !config.places.length ? (
        <div className="p-3 text-xs text-faint">None</div>
      ) : (
        <div className="py-1">
          {config.places.map((p) => (
            <Row key={p.id} k={`place:${p.id}`}>
              <PlaceIcon place={p} />
              <span className="truncate">{placeName(p)}</span>
              {p.default && <Pill className="ml-auto">Default</Pill>}
            </Row>
          ))}
        </div>
      )}
    </Card>
  )
}

function SkillsNode() {
  const { config } = useCanvas()
  return (
    <Card id="skills" icon={<Sparkles />} title="Skills" count={config.skills.length}>
      {!config.skills.length ? <div className="p-3 text-xs text-faint">None</div> : (
        <div className="py-1">
          {config.skills.map((s) => (
            <Row key={s.skill_id} k={`skill:${s.skill_id}`}>
              <span className="truncate">{s.title}</span>
              {s.type === "anthropic" && <span className="text-[11px] text-faint">Anthropic</span>}
              {s.platforms && <span className="text-[11px] text-faint">{s.platforms.map((p) => p[0]!.toUpperCase() + p.slice(1)).join(", ")} only</span>}
              {s.requires?.length ? <Plug className="ml-auto size-3.5 shrink-0 text-faint" /> : null}
            </Row>
          ))}
        </div>
      )}
    </Card>
  )
}

function ToolsNode() {
  const { config, catalog, changed } = useCanvas()
  const on = config.tools.filter((t) => t.enabled)
  return (
    <Card id="tools" icon={<Wrench />} title="Tools" count={`${on.length}/${config.tools.length}`}>
      <div className="flex flex-wrap gap-1 p-3">
        {on.map((t) => (
          <Pill key={t.name} className={cn("text-foreground/80", changed.has(`tool:${t.name}`) && "border-link/40 text-link")}>
            {catalog?.tools.find((x) => x.name === t.name)?.label ?? t.name}
          </Pill>
        ))}
        {!on.length && <span className="text-xs text-faint">None</span>}
      </div>
    </Card>
  )
}

function McpNode({ id }: { id: string }) {
  const { config } = useCanvas()
  const s = config.mcp_servers.find((x) => `mcp:${x.key}` === id)
  if (!s) return null
  const cred = s.credential === "expired" ? <span className="text-[11px] font-medium text-destructive">Expired</span>
      : s.credential === "missing" ? <span className="text-[11px] font-medium text-warning">Not signed in</span> : null
  return (
    <Card
      id={id}
      className="w-64"
      icon={<Plug />}
      title={s.name}
      count={s.tools.length}
      action={cred && <Tooltip><TooltipTrigger asChild><span className="flex items-center">{cred}</span></TooltipTrigger><TooltipContent>Sign in again</TooltipContent></Tooltip>}
    >
      <div className="py-1">
        {s.tools.map((t) => (
          <Row key={t.name} k={`${id}/${t.name}`}>
            <span className="truncate font-mono text-[12px]">{t.name}</span>
            <RuleChip rule={t.rule} className="ml-auto" />
          </Row>
        ))}
      </div>
    </Card>
  )
}

/** A platform node: the kind's own card body under its label. */
function PlatformNode({ id }: { id: string }) {
  const { agent } = useCanvas()
  const node = agent.nodes.find((n) => nodeId(n.kind) === id)
  if (!node) return null
  const ui = nodeUi(node.kind)
  return <Card id={id} className="w-64" icon={<ui.icon />} title={node.label}><ui.Card agent={agent} node={node} /></Card>
}

/** The Connections branch: its count, and adding an MCP server or a node kind not yet attached. */
function HubNode() {
  const { agent, config, select, ro } = useCanvas()
  const connect = useConnectNode(agent.id)
  const n = connections(agent, config).length
  const addable = agent.nodes.filter((x) => x.server == null)
  return (
    <div className="flex h-8 items-center gap-1.5 rounded-full border bg-card pr-1 pl-3 text-xs shadow-[0_1px_3px_rgb(0_0_0/0.05)]">
      <Handle type="target" position={Position.Top} isConnectable={false} className="!pointer-events-none !opacity-0" />
      <Handle type="source" position={Position.Bottom} isConnectable={false} className="!pointer-events-none !opacity-0" />
      <span className="font-medium">Connections</span>
      <span className="text-faint tabular">{n}</span>
      {ro ? <span className="w-1.5" /> : (
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button className="flex size-6 items-center justify-center rounded-full text-muted-foreground hover:bg-accent hover:text-foreground" aria-label="Add connection">
              <Plus className="size-3.5" />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="center" className="w-48">
            <DropdownMenuItem onSelect={() => select("mcp:new")}><Plug />MCP server</DropdownMenuItem>
            {addable.map((x) => {
              const Icon = nodeUi(x.kind).icon
              return (
                <DropdownMenuItem key={x.kind} disabled={connect.isPending}
                  onSelect={() => connect.mutate(x.kind, { onSuccess: () => select(nodeId(x.kind)) })}>
                  <Icon />{x.label}
                </DropdownMenuItem>
              )
            })}
          </DropdownMenuContent>
        </DropdownMenu>
      )}
    </div>
  )
}

const NODE_TYPES = {
  agent: AgentNode, instructions: InstructionsNode, model: ModelNode, places: PlacesNode,
  skills: SkillsNode, tools: ToolsNode, mcp: ({ id }: { id: string }) => <McpNode id={id} />,
  platform: ({ id }: { id: string }) => <PlatformNode id={id} />, hub: HubNode,
}

// ---------- Edges ----------

/** Orthogonal polyline with rounded corners. */
function roundedPath(p: Pt[], r = 10) {
  let d = `M ${p[0]!.x} ${p[0]!.y}`
  for (let i = 1; i < p.length - 1; i++) {
    const a = p[i - 1]!, b = p[i]!, c = p[i + 1]!
    const r1 = Math.min(r, Math.hypot(b.x - a.x, b.y - a.y) / 2, Math.hypot(c.x - b.x, c.y - b.y) / 2)
    const ux = Math.sign(b.x - a.x), uy = Math.sign(b.y - a.y), vx = Math.sign(c.x - b.x), vy = Math.sign(c.y - b.y)
    d += ` L ${b.x - ux * r1} ${b.y - uy * r1} Q ${b.x} ${b.y} ${b.x + vx * r1} ${b.y + vy * r1}`
  }
  const z = p.at(-1)!
  return `${d} L ${z.x} ${z.y}`
}

function LineEdge({ id, data }: EdgeProps) {
  const { points, dep } = (data ?? {}) as { points?: Pt[]; dep?: boolean }
  if (!points) return null
  return (
    <BaseEdge
      id={id}
      path={roundedPath(points)}
      style={dep ? { stroke: "var(--link)", strokeWidth: 1.25, strokeDasharray: "4 4", opacity: 0.7 } : { stroke: "var(--border-strong)", strokeWidth: 1.25 }}
    />
  )
}
const EDGE_TYPES = { line: LineEdge }

// ---------- Graph and layout ----------

const LEFT = ["instructions", "model", "places"]
const RIGHT = ["skills", "tools"]
const HUB = "connections"
/** Below this canvas width the editor is one column. */
const NARROW = 760

/** MCP servers no node claims, then each attached node. */
function connections(agent: Agent, config: AgentConfig): { id: string; type: "mcp" | "platform" }[] {
  const claimed = new Set(agent.nodes.flatMap((n: ConnectionNode) => (n.server ? [n.server] : [])))
  return [
    ...config.mcp_servers.filter((s) => !claimed.has(s.key)).map((s) => ({ id: `mcp:${s.key}`, type: "mcp" as const })),
    ...agent.nodes.filter((n) => n.server).map((n) => ({ id: nodeId(n.kind), type: "platform" as const })),
  ]
}

function graph(agent: Agent, config: AgentConfig, ro: boolean) {
  const conns = connections(agent, config)
  const hub = conns.length || !ro
  const nodes: Node[] = [
    ...LEFT.map((id) => ({ id, type: id })),
    { id: "agent", type: "agent" },
    ...RIGHT.map((id) => ({ id, type: id })),
    ...(hub ? [{ id: HUB, type: "hub" }] : []),
    ...conns,
  ].map((n) => ({ ...n, position: { x: 0, y: 0 }, data: {}, draggable: false, selectable: false }))
  const ids = new Set(conns.map((c) => c.id))
  const deps = config.skills.flatMap((s) => (s.requires ?? []).filter((k) => ids.has(`mcp:${k}`)).map((k) => ({ skill: s.skill_id, target: `mcp:${k}` })))
  return { nodes, conns: conns.map((c) => c.id), hub: hub ? HUB : null, deps }
}

/** The y of a skill's row inside the skills card. */
const rowY = (skill: string) => {
  const el = document.querySelector<HTMLElement>(`.react-flow__node[data-id="skills"] [data-row="skill:${skill}"]`)
  return el ? el.offsetTop + el.offsetHeight / 2 : 20
}

// ---------- Canvas ----------

export const PANEL_W = 440

type Props = Omit<Ctx, "ro"> & { ro: boolean }

export function Canvas({ agent, config, catalog, changed, changedNodes, selected, select, ro }: Props) {
  const { resolved } = useTheme()
  const structure = `${config.mcp_servers.map((s) => s.key).join(",")}|${agent.nodes.map((n) => `${n.kind}:${n.server}`).join(",")}|${ro}|${config.skills.map((s) => `${s.skill_id}:${(s.requires ?? []).join("+")}`).join(",")}`
  const built = useMemo(() => graph(agent, config, ro), [structure]) // eslint-disable-line react-hooks/exhaustive-deps
  const [nodes, setNodes, onNodesChange] = useNodesState<Node>([])
  const [edges, setEdges] = useEdgesState<Edge>([])
  const fitted = useRef(false)
  const [fit, setFit] = useState(false)
  const rf = useReactFlow()
  const width = useStore((s) => s.width)
  const narrow = width > 0 && width < NARROW
  const sizes = useStore((s) => s.nodes.map((n) => `${n.id}:${n.measured?.width ?? 0}x${n.measured?.height ?? 0}`).join("|"))
  const measured = useStore((s) => s.nodes.length > 0 && s.nodes.every((n) => n.measured?.width))
  const ready = nodes.length > 0 && nodes.every((n) => n.style?.opacity === 1)

  useEffect(() => {
    setNodes((prev) => built.nodes.map((n) => {
      const p = prev.find((x) => x.id === n.id)
      return p ? { ...n, position: p.position, measured: p.measured, style: p.style } : { ...n, style: { opacity: 0 } }
    }))
  }, [built, setNodes])

  useEffect(() => {
    if (!measured) return
    const known = Object.fromEntries(rf.getNodes().map((n) => [n.id, { w: n.measured?.width ?? 240, h: n.measured?.height ?? 80 }]))
    const size = (id: string): Size => known[id] ?? { w: 240, h: 80 }
    const deps: Dep[] = built.deps.map((d) => ({ key: `${d.skill}>${d.target}`, target: d.target, y: rowY(d.skill) }))
    const plan: Plan = { left: LEFT, right: RIGHT, hub: built.hub, conns: built.conns, skills: "skills", deps }
    const laid = (narrow ? layoutNarrow : layoutWide)("agent", plan, size)
    setNodes((ns) => ns.map((n) => ({ ...n, position: laid.pos[n.id] ?? n.position, style: { opacity: 1 } })))
    setEdges(Object.entries(laid.edges).map(([id, l]) => ({ id, source: l.source, target: l.target, type: "line", data: { points: l.points, dep: l.dep } })))
  }, [sizes, measured, built, narrow]) // eslint-disable-line react-hooks/exhaustive-deps

  // Fit once laid out, and again when the editor switches between one and two columns.
  useEffect(() => { fitted.current = false }, [narrow])
  useEffect(() => {
    if (!ready || fitted.current) return
    fitted.current = true
    setTimeout(() => {
      if (!narrow) { void rf.fitView({ padding: 0.06, maxZoom: 1 }).then(() => setFit(true)); return }
      // One column reads top down at full size; scroll to see the rest.
      const ns = rf.getNodes()
      const right = Math.max(...ns.map((n) => n.position.x + (n.measured?.width ?? 0)))
      const zoom = Math.min(1, (width - 24) / (right + 12))
      void rf.setViewport({ x: Math.max(12, (width - right * zoom) / 2), y: 16, zoom }).then(() => setFit(true))
    }, 30)
  }, [ready, narrow, sizes]) // eslint-disable-line react-hooks/exhaustive-deps

  // Keep the selected node clear of the inspector; pan back when it closes. One column: the panel covers it.
  const home = useRef<{ x: number; y: number; zoom: number } | null>(null)
  useEffect(() => {
    if (!fit || narrow) return
    if (!selected) {
      if (home.current) void rf.setViewport(home.current, { duration: 250 })
      home.current = null
      return
    }
    const id = selected.startsWith("skill:") ? "skills" : selected.startsWith("place:") ? "places" : selected.split("/")[0]!
    const n = rf.getNode(id)
    const el = document.querySelector(".react-flow")
    if (!n?.measured?.width || !el) return
    const vp = rf.getViewport()
    const left = n.position.x * vp.zoom + vp.x
    const right = left + n.measured.width * vp.zoom
    const room = el.clientWidth - PANEL_W - 24
    if (right > room || left < 24) {
      home.current ??= vp
      void rf.setViewport({ ...vp, x: vp.x - (right > room ? Math.min(right - room, left - 24) : left - 24) }, { duration: 250 })
    }
  }, [selected, fit]) // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <CanvasCtx.Provider value={{ agent, config, catalog, changed, changedNodes, selected, select, ro }}>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        onNodesChange={onNodesChange}
        nodeTypes={NODE_TYPES}
        edgeTypes={EDGE_TYPES}
        colorMode={resolved}
        minZoom={0.3}
        maxZoom={1.6}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={false}
        panOnScroll
        zoomOnDoubleClick={false}
        onNodeClick={() => {}}
        onPaneClick={() => selected && select("")}
        proOptions={{ hideAttribution: true }}
        className="agent-canvas"
        style={{ background: "transparent" }}
      >
        <Background variant={BackgroundVariant.Dots} gap={20} size={1.2} color="var(--border-strong)" />
        <Zoom />
      </ReactFlow>
    </CanvasCtx.Provider>
  )
}

function Zoom() {
  const rf = useReactFlow()
  const zoom = useStore((s) => s.transform[2])
  const b = "flex size-8 items-center justify-center text-muted-foreground hover:bg-accent hover:text-foreground [&_svg]:size-4"
  return (
    <div className="absolute bottom-4 left-4 z-10 flex items-center overflow-hidden rounded-lg border bg-popover shadow-[0_1px_3px_rgb(0_0_0/0.06)]">
      <button className={b} onClick={() => rf.zoomOut({ duration: 150 })} aria-label="Zoom out"><Minus /></button>
      <button className="h-8 w-12 text-xs text-muted-foreground tabular hover:bg-accent" onClick={() => rf.zoomTo(1, { duration: 150 })} aria-label="Reset zoom">{Math.round(zoom * 100)}%</button>
      <button className={b} onClick={() => rf.zoomIn({ duration: 150 })} aria-label="Zoom in"><Plus /></button>
      <span className="h-5 w-px bg-border" />
      <button className={b} onClick={() => rf.fitView({ padding: 0.08, maxZoom: 1, duration: 200 })} aria-label="Fit"><Maximize /></button>
    </div>
  )
}
