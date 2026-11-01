import "@xyflow/react/dist/style.css"

import {
  Background, BackgroundVariant, BaseEdge, type Edge, type EdgeProps, Handle, type Node, Position, ReactFlow, useNodesState, useReactFlow,
  useStore,
} from "@xyflow/react"
import type { ELK, ElkExtendedEdge, ElkNode } from "elkjs/lib/elk-api"
import { Blocks, Cpu, Maximize, Minus, Plug, Plus, ScrollText, Sparkles, Wrench } from "lucide-react"
import { createContext, useContext, useEffect, useMemo, useRef, useState } from "react"

import { AppIcon } from "@/components/brand"
import { AgentAvatar, Ago, Avatar, Money } from "@/components/common"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { useMe } from "@/lib/hooks"
import { useTheme } from "@/lib/theme"
import { cn } from "@/lib/utils"
import { pressProps } from "@/components/filters"
import { Dot, Locked, Pill, PlaceIcon, RuleChip } from "./bits"
import { EFFORT_LABEL, placeName } from "./lib"
import type { Agent, AgentApp, AgentConfig, Catalog } from "./types"

type Ctx = {
  agent: Agent
  config: AgentConfig
  catalog?: Catalog
  changed: Set<string>
  changedNodes: Set<string>
  selected: string
  select: (id: string) => void
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
    <Card id="places" icon={<Blocks />} title="Where it runs" count={config.places?.length}>
      {config.places == null ? <Locked who={agent.owner.name} className="p-3" /> : !config.places.length ? (
        <div className="p-3 text-xs text-faint">Nowhere yet</div>
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

function AppNode({ id }: { id: string }) {
  const app = useMe().data?.apps.find((a) => `app:${a.key}` === id)
  if (!app) return null
  return <Card id={id} icon={<AppIcon icon={app.icon} />} title={app.name} />
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
    <Card id="tools" icon={<Wrench />} title="Built-in tools" count={`${on.length}/${config.tools.length}`}>
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
  const apps = useMe().data?.apps
  const s = config.mcp_servers.find((x) => `mcp:${x.key}` === id)
  if (!s) return null
  const app = apps?.find((a) => a.key === s.app)
  const cred = s.credential === "expired" ? <span className="text-[11px] font-medium text-destructive">Expired</span>
      : s.credential === "missing" ? <span className="text-[11px] font-medium text-warning">Not signed in</span> : null
  return (
    <Card
      id={id}
      className="w-64"
      icon={app ? <AppIcon icon={app.icon} /> : <Plug />}
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

const NODE_TYPES = {
  agent: AgentNode, instructions: InstructionsNode, model: ModelNode, places: PlacesNode,
  skills: SkillsNode, tools: ToolsNode, mcp: ({ id }: { id: string }) => <McpNode id={id} />, app: ({ id }: { id: string }) => <AppNode id={id} />,
}

// ---------- Edges ----------

type Pt = { x: number; y: number }
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

function ElkEdge({ id, data }: EdgeProps) {
  const pts = (data as { points?: Pt[]; dep?: boolean } | undefined)?.points
  if (!pts) return null
  const dep = (data as { dep?: boolean }).dep
  return (
    <BaseEdge
      id={id}
      path={roundedPath(pts)}
      style={dep ? { stroke: "var(--link)", strokeWidth: 1.25, strokeDasharray: "4 4", opacity: 0.7 } : { stroke: "var(--border-strong)", strokeWidth: 1.25 }}
    />
  )
}
const EDGE_TYPES = { elk: ElkEdge }

// ---------- Graph and layout ----------

const LEFT = ["instructions", "model", "places"]

function graph(config: AgentConfig, apps: AgentApp[]) {
  const mcp = config.mcp_servers.map((s) => `mcp:${s.key}`)
  const left = [...LEFT.map((id) => ({ id, type: id })), ...apps.map((a) => ({ id: `app:${a.key}`, type: "app" }))]
  const nodes: (Node & { partition: number })[] = [
    ...left.map((n) => ({ ...n, partition: 0 })),
    { id: "agent", type: "agent", partition: 1 },
    { id: "skills", type: "skills", partition: 2 },
    { id: "tools", type: "tools", partition: 2 },
    ...mcp.map((id) => ({ id, type: "mcp", partition: 3 })),
  ].map((n) => ({ ...n, position: { x: 0, y: 0 }, data: {}, draggable: false, selectable: false }))
  const e = (source: string, target: string, data: Record<string, unknown> = {}): Edge => ({ id: `${source}>${target}${"skill" in data ? `:${data.skill}` : ""}`, source, target, type: "elk", data })
  const edges: Edge[] = [
    ...left.map((n) => e(n.id, "agent")),
    e("agent", "skills"), e("agent", "tools"),
    ...mcp.map((id) => e("agent", id)),
    ...config.skills.flatMap((s) => (s.requires ?? []).filter((k) => mcp.includes(`mcp:${k}`)).map((k) => e("skills", `mcp:${k}`, { dep: true, skill: s.skill_id }))),
  ]
  return { nodes, edges }
}

/** The layout engine loads on first use and runs in a worker, off the page's thread. */
let elk: Promise<ELK> | undefined
const engine = () => (elk ??= Promise.all([import("elkjs/lib/elk-api"), import("elkjs/lib/elk-worker.min.js?url")])
  .then(([{ default: Engine }, { default: workerUrl }]) => new Engine({ workerUrl })))
const HEAD = 20

async function layout(nodes: (Node & { partition?: number })[], edges: Edge[]) {
  const size = (n: Node) => ({ width: n.measured?.width ?? 240, height: n.measured?.height ?? 80 })
  const rowY = (skill: string) => {
    const el = document.querySelector<HTMLElement>(`.react-flow__node[data-id="skills"] [data-row="skill:${skill}"]`)
    return el ? el.offsetTop + el.offsetHeight / 2 : HEAD
  }
  const g: ElkNode = {
    id: "root",
    layoutOptions: {
      "elk.algorithm": "layered", "elk.direction": "RIGHT", "elk.partitioning.activate": "true", "elk.edgeRouting": "ORTHOGONAL",
      "elk.layered.spacing.nodeNodeBetweenLayers": "64", "elk.spacing.nodeNode": "20", "elk.layered.spacing.edgeNodeBetweenLayers": "20",
      "elk.layered.spacing.edgeEdgeBetweenLayers": "8", "elk.layered.nodePlacement.strategy": "BRANDES_KOEPF", "elk.layered.nodePlacement.bk.fixedAlignment": "BALANCED",
      "elk.layered.considerModelOrder.strategy": "NODES_AND_EDGES", "elk.layered.crossingMinimization.forceNodeModelOrder": "true",
    },
    children: nodes.map((n) => {
      const { width, height } = size(n)
      const mid = n.id === "agent" ? height / 2 : HEAD
      const deps = n.id === "skills" ? edges.filter((e) => e.source === "skills").map((e) => (e.data as { skill: string }).skill) : []
      return {
        id: n.id, width, height,
        layoutOptions: { "elk.partitioning.partition": String(n.partition ?? 0), "elk.portConstraints": "FIXED_POS" },
        ports: [
          { id: `${n.id}:in`, x: -1, y: mid, width: 1, height: 1, layoutOptions: { "elk.port.side": "WEST" } },
          { id: `${n.id}:out`, x: width, y: mid, width: 1, height: 1, layoutOptions: { "elk.port.side": "EAST" } },
          ...[...new Set(deps)].map((s) => ({ id: `skills:${s}`, x: width, y: rowY(s), width: 1, height: 1, layoutOptions: { "elk.port.side": "EAST" } })),
        ],
      }
    }),
    edges: edges.map((e): ElkExtendedEdge => {
      const skill = (e.data as { skill?: string }).skill
      return { id: e.id, sources: [skill ? `skills:${skill}` : `${e.source}:out`], targets: [`${e.target}:in`] }
    }),
  }
  const out = await (await engine()).layout(g)
  const pos = Object.fromEntries((out.children ?? []).map((c) => [c.id, { x: c.x ?? 0, y: c.y ?? 0 }]))
  const pts = Object.fromEntries((out.edges ?? []).map((e) => {
    const s = e.sections?.[0]
    return [e.id, s ? [s.startPoint, ...(s.bendPoints ?? []), s.endPoint] : undefined]
  }))
  // Center the agent on the graph; shift the ends of its edges with it (last/first segment is horizontal).
  const a = out.children?.find((c) => c.id === "agent")
  if (a) {
    const ys = (out.children ?? []).flatMap((c) => [c.y ?? 0, (c.y ?? 0) + (c.height ?? 0)])
    const dy = Math.round((Math.min(...ys) + Math.max(...ys)) / 2 - ((a.y ?? 0) + (a.height ?? 0) / 2))
    pos.agent!.y += dy
    for (const e of edges) {
      const p = pts[e.id]
      if (!p || (e.target !== "agent" && e.source !== "agent")) continue
      if (p.length === 2) { const mx = (p[0]!.x + p[1]!.x) / 2; p.splice(1, 0, { x: mx, y: p[0]!.y }, { x: mx, y: p[1]!.y }) }
      const ends = e.target === "agent" ? [p.length - 2, p.length - 1] : [0, 1]
      for (const i of ends) p[i] = { ...p[i]!, y: p[i]!.y + dy }
    }
  }
  return { pos, pts }
}

// ---------- Canvas ----------

export const PANEL_W = 440

export function Canvas({ agent, config, catalog, changed, changedNodes, selected, select }: Ctx) {
  const { resolved } = useTheme()
  const structure = `${agent.apps.map((a) => a.key).join(",")}|${config.mcp_servers.map((s) => s.key).join(",")}|${config.skills.map((s) => `${s.skill_id}:${(s.requires ?? []).join("+")}`).join(",")}`
  const built = useMemo(() => graph(config, agent.apps), [structure]) // eslint-disable-line react-hooks/exhaustive-deps
  const [nodes, setNodes, onNodesChange] = useNodesState<Node>([])
  const [edges, setEdges] = useState<Edge[]>([])
  const [ready, setReady] = useState(false)
  const fitted = useRef(false)
  const [fit, setFit] = useState(false)
  const rf = useReactFlow()
  const sizes = useStore((s) => s.nodes.map((n) => `${n.id}:${n.measured?.width ?? 0}x${n.measured?.height ?? 0}`).join("|"))
  const measured = useStore((s) => s.nodes.length > 0 && s.nodes.every((n) => n.measured?.width))

  useEffect(() => {
    setNodes((prev) => built.nodes.map((n) => {
      const p = prev.find((x) => x.id === n.id)
      return p ? { ...n, position: p.position, measured: p.measured, style: p.style } : { ...n, style: { opacity: 0 } }
    }))
    setEdges((prev) => built.edges.map((e) => ({ ...e, data: { ...e.data, points: (prev.find((x) => x.id === e.id)?.data as { points?: Pt[] })?.points } })))
  }, [built, setNodes])

  useEffect(() => {
    if (!measured) return
    let live = true
    layout(rf.getNodes() as (Node & { partition?: number })[], built.edges).then(({ pos, pts }) => {
      if (!live) return
      setNodes((ns) => ns.map((n) => ({ ...n, position: pos[n.id] ?? n.position, style: { opacity: 1 } })))
      setEdges(built.edges.map((e) => ({ ...e, data: { ...e.data, points: pts[e.id] } })))
      setReady(true)
    })
    return () => { live = false }
  }, [sizes, measured, built]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!ready || fitted.current) return
    fitted.current = true
    setTimeout(() => rf.fitView({ padding: 0.06, maxZoom: 1 }).then(() => setFit(true)), 30)
  }, [ready]) // eslint-disable-line react-hooks/exhaustive-deps

  // Keep the selected node clear of the inspector; pan back when it closes.
  const home = useRef<{ x: number; y: number; zoom: number } | null>(null)
  useEffect(() => {
    if (!fit) return
    if (!selected) {
      if (home.current) rf.setViewport(home.current, { duration: 250 })
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
      rf.setViewport({ ...vp, x: vp.x - (right > room ? Math.min(right - room, left - 24) : left - 24) }, { duration: 250 })
    }
  }, [selected, fit]) // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <CanvasCtx.Provider value={{ agent, config, catalog, changed, changedNodes, selected, select }}>
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
