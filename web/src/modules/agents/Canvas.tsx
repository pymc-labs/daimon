import "@xyflow/react/dist/style.css"

import {
  Background, BackgroundVariant, BaseEdge, type Edge, type EdgeProps, getViewportForBounds, Handle, type InternalNode, type Node, Position, ReactFlow, useEdgesState,
  useInternalNode, useNodesState, useReactFlow, useStore,
} from "@xyflow/react"
import { Blocks, Brain, ChevronDown, ChevronUp, Cpu, KeyRound, Maximize, Minus, Plug, Plus, ScrollText, Sparkles, Users, Wrench } from "lucide-react"
import { createContext, useContext, useEffect, useMemo, useRef, useState } from "react"

import { BrandIcon } from "@/components/brand"
import { AgentAvatar, Ago, Avatar, Money } from "@/components/common"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { modelLabel } from "@/lib/format"
import { useTheme } from "@/lib/theme-context"
import { cn } from "@/lib/utils"
import { pressProps } from "@/lib/filters"
import { useAgents } from "./api"
import { useAgentKeys, useCodingKeys, useKnowledge } from "./setup/api"
import { GithubFacts } from "./setup/GithubPanel"
import { Dot, Locked, Pill, PlaceIcon, RuleChip } from "./bits"
import { depLine, layoutNarrow, layoutRing, spine, spoke, type Box, type Group, type Pt, type Size } from "./layout"
import { EFFORT_LABEL, placeName } from "./lib"
import { nodeId, nodeUi } from "./nodes/registry"
import type { Agent, AgentConfig, AgentNode as ConnectionNode, Catalog } from "./types"
import { useWheelZoom } from "./zoom"

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
      {children && <Fold>{children}</Fold>}
    </div>
  )
}

/** Card bodies taller than this fold behind Show more, so one long card does not shrink the whole canvas. */
const FOLD = 240
const below = (body: HTMLElement, row: HTMLElement) => row.offsetTop - body.offsetTop + row.offsetHeight > FOLD

function Fold({ children }: { children: React.ReactNode }) {
  const { selected } = useCanvas()
  const body = useRef<HTMLDivElement>(null)
  const [tall, setTall] = useState(false)
  const [more, setMore] = useState(0)
  const [open, setOpen] = useState(false)
  useEffect(() => {
    const el = body.current
    if (!el) return
    const ro = new ResizeObserver(() => {
      // Folding to hide a row or so would cost as much room as the button.
      setTall(el.offsetHeight > FOLD + 48)
      setMore([...el.querySelectorAll<HTMLElement>("[data-row]")].filter((r) => below(el, r)).length)
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  // A hidden row selected from the inspector or a link, or reached by Tab, opens the card.
  const reveal = (row: HTMLElement | null | undefined) => { if (row && body.current && below(body.current, row)) setOpen(true) }
  useEffect(() => reveal([...(body.current?.querySelectorAll<HTMLElement>("[data-row]") ?? [])].find((r) => r.dataset.row === selected)), [selected])
  const folded = tall && !open
  return (
    <>
      <div
        className={cn(folded && "overflow-clip")}
        style={folded ? { maxHeight: FOLD } : undefined}
        onFocus={(e) => reveal((e.target as HTMLElement).closest<HTMLElement>("[data-row]"))}
      >
        <div ref={body}>{children}</div>
      </div>
      {folded && <div className="pointer-events-none relative -mt-8 h-8 bg-gradient-to-t from-card" />}
      {tall && (
        <button
          className="flex h-8 w-full items-center justify-center gap-1 rounded-b-xl border-t text-xs text-muted-foreground hover:bg-accent/70 hover:text-foreground [&_svg]:size-3.5"
          onClick={(e) => { e.stopPropagation(); setOpen(!open) }}
          onKeyDown={(e) => e.stopPropagation()}
        >
          {open ? <>Show less<ChevronUp /></> : <>{more ? `Show ${more} more` : "Show more"}<ChevronDown /></>}
        </button>
      )}
    </>
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
        <span className="text-muted-foreground">Last 30 days</span>
        <Money usd={agent.spend_usd} />
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
  const c = catalog?.models.find((x) => x.id === config.model.id && x.backend === config.model.backend)
  return (
    <Card id="model" icon={<Cpu />} title="Model">
      <div className="space-y-1 p-3">
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium">{modelLabel(config.model.id, c?.label)}{config.model.speed === "fast" && " Fast"}</span>
          {config.model.backend === "anthropic" && <Pill>{EFFORT_LABEL[config.model.effort]}</Pill>}
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

function HelpersNode() {
  const { config, agent } = useCanvas()
  const roster = useAgents().data
  return (
    <Card id="helpers" icon={<Users />} title="Helpers" count={config.helpers?.length}>
      {config.helpers == null ? <Locked who={agent.owner.name} className="p-3" /> : !config.helpers.length ? (
        <div className="p-3 text-xs text-faint">None</div>
      ) : (
        <div className="py-1">
          {config.helpers.map((h) => (
            <Row key={h.id} k={`helper:${h.id}`}>
              <AgentAvatar src={roster?.find((a) => a.id === h.id)?.avatar} name={h.name} className="size-4 text-[8px]" />
              <span className="truncate">{h.name}</span>
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

function PeopleNode() {
  const { agent } = useCanvas()
  const people = [{ ...agent.owner, role: "Owner" }, ...agent.co_owners.map((p) => ({ ...p, role: "Co-owner" }))]
  return (
    <Card id="people" icon={<Users />} title="People" count={people.length}>
      <div className="py-1">
        {people.map((p) => (
          <div key={p.id} className="flex h-8 items-center gap-2 px-3 text-[13px]">
            <Avatar name={p.name} className="size-4 text-[8px]" /><span className="truncate">{p.name}</span>
            <span className="ml-auto text-[11px] text-faint">{p.role}</span>
          </div>
        ))}
      </div>
    </Card>
  )
}

function KeysNode() {
  const { agent } = useCanvas()
  const keys = useAgentKeys(agent.id, agent.can_view_details)
  const names = keys.data?.map((k) => k.key) ?? []
  return (
    <Card id="keys" icon={<KeyRound />} title="Keys" count={keys.data ? names.length : undefined}>
      <div className="flex flex-wrap gap-1 p-3">
        {names.slice(0, 6).map((k) => <Pill key={k} className="font-mono text-foreground/80">{k}</Pill>)}
        {names.length > 6 && <span className="text-xs text-faint">+{names.length - 6}</span>}
        {keys.data && !names.length && <span className="text-xs text-faint">None</span>}
      </div>
    </Card>
  )
}

function KnowledgeNode() {
  const { agent } = useCanvas()
  const k = useKnowledge(agent.id, agent.can_view_details)
  const n = k.data ? Number(k.data.shared) + k.data.channels.length : undefined
  return (
    <Card id="knowledge" icon={<Brain />} title="Knowledge" count={n}>
      <div className="p-3 text-xs text-faint">{k.data ? (n ? `${k.data.shared ? "Shared" : ""}${k.data.shared && k.data.channels.length ? ", " : ""}${k.data.channels.length ? `${k.data.channels.length} in channels` : ""}` : "Nothing yet") : ""}</div>
    </Card>
  )
}

function CodingNode() {
  const { agent } = useCanvas()
  const keys = useCodingKeys(agent.id, agent.can_view_details)
  return (
    <Card id="coding" icon={<Plug />} title="Access via MCP" count={keys.data?.length}>
      <div className="p-3 text-xs text-faint">{keys.data?.length ? "Keys in use" : keys.data ? "No keys" : ""}</div>
    </Card>
  )
}

function GithubNode() {
  const { agent } = useCanvas()
  return <Card id="github" icon={<BrandIcon brand="github" />} title="GitHub"><GithubFacts agent={agent} /></Card>
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
      count={s.listed === false ? "Not listed" : s.tools.length}
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

const NODE_TYPES = {
  agent: AgentNode, instructions: InstructionsNode, model: ModelNode, places: PlacesNode,
  skills: SkillsNode, tools: ToolsNode, helpers: HelpersNode, people: PeopleNode, keys: KeysNode, coding: CodingNode, knowledge: KnowledgeNode, github: GithubNode, mcp: ({ id }: { id: string }) => <McpNode id={id} />,
  platform: ({ id }: { id: string }) => <PlatformNode id={id} />,
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

/** A smooth curve through every point (Catmull-Rom as cubic Béziers). */
function smoothPath(p: Pt[]) {
  let d = `M ${p[0]!.x} ${p[0]!.y}`
  for (let i = 0; i < p.length - 1; i++) {
    const a = p[i - 1] ?? p[i]!, b = p[i]!, c = p[i + 1]!, e = p[i + 2] ?? c
    d += ` C ${b.x + (c.x - a.x) / 6} ${b.y + (c.y - a.y) / 6} ${c.x - (e.x - b.x) / 6} ${c.y - (e.y - b.y) / 6} ${c.x} ${c.y}`
  }
  return d
}

/** Line kinds; each is drawn from the cards' live boxes, so it follows them as they glide. */
type LineData = { kind: "spoke" | "spine" } | { kind: "dep"; y: number; i: number }

const boxOf = (n: InternalNode | undefined): Box | null =>
  n?.measured.width ? { ...n.internals.positionAbsolute, w: n.measured.width, h: n.measured.height ?? 0 } : null

function LineEdge({ id, source, target, data }: EdgeProps) {
  const s = boxOf(useInternalNode(source)), t = boxOf(useInternalNode(target)), a = boxOf(useInternalNode("agent"))
  const line = data as LineData | undefined
  if (!s || !t || !a || !line) return null
  if (line.kind === "dep") {
    return <BaseEdge id={id} path={smoothPath(depLine(s, line.y, t, a, line.i))} style={{ stroke: "var(--link)", strokeWidth: 1.25, strokeDasharray: "4 4", opacity: 0.7 }} />
  }
  return <BaseEdge id={id} path={roundedPath(line.kind === "spine" ? spine(s, t) : spoke(s, t))} style={{ stroke: "var(--border-strong)", strokeWidth: 1.25 }} />
}
const EDGE_TYPES = { line: LineEdge }

// ---------- Graph and layout ----------

/** The agent's brief and its abilities; keys, coding keys and GitHub are setup, for those who see it. */
const lanes = (agent: Agent) => ({
  left: ["instructions", "model", "places", "people"],
  right: ["skills", "tools", "helpers", ...(agent.can_view_details ? ["knowledge", "keys", "coding", "github"] : [])],
})
/** The ring, clockwise from the top: abilities top right with skills nearest the connections they call, connections below, the brief top left. */
const groups = (agent: Agent, conns: string[]): Group[] => {
  const { left, right } = lanes(agent)
  return [{ ids: [...right].reverse(), at: 60 }, { ids: [...conns].reverse(), at: 180 }, { ids: [...left].reverse(), at: 300 }]
}
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

function graph(agent: Agent, config: AgentConfig) {
  const conns = connections(agent, config)
  const { left, right } = lanes(agent)
  const nodes: Node[] = [
    ...left.map((id) => ({ id, type: id })),
    { id: "agent", type: "agent" },
    ...right.map((id) => ({ id, type: id })),
    ...conns,
  ].map((n) => ({ ...n, position: { x: 0, y: 0 }, data: {}, draggable: false, selectable: false }))
  const ids = new Set(conns.map((c) => c.id))
  const deps = config.skills.flatMap((s) => (s.requires ?? []).filter((k) => ids.has(`mcp:${k}`)).map((k) => ({ skill: s.skill_id, target: `mcp:${k}` })))
  return { nodes, conns: conns.map((c) => c.id), column: [...left, ...right, ...conns.map((c) => c.id)], deps }
}

/** How long cards take to glide to a new layout. */
const GLIDE_MS = 280
const SHOWN = { opacity: 1 }

/** The y of a skill's row inside the skills card. */
const rowY = (skill: string) => {
  const el = document.querySelector<HTMLElement>(`.react-flow__node[data-id="skills"] [data-row="skill:${skill}"]`)
  return el ? el.offsetTop + el.offsetHeight / 2 : 20
}

// ---------- Canvas ----------

export const PANEL_W = 440
const MIN_ZOOM = 0.3
const MAX_ZOOM = 1.6

type Props = Omit<Ctx, "ro"> & { ro: boolean }

export function Canvas({ agent, config, catalog, changed, changedNodes, selected, select, ro }: Props) {
  const { resolved } = useTheme()
  const structure = `${agent.can_view_details}|${config.mcp_servers.map((s) => s.key).join(",")}|${agent.nodes.map((n) => `${n.kind}:${n.server}`).join(",")}|${ro}|${config.skills.map((s) => `${s.skill_id}:${(s.requires ?? []).join("+")}`).join(",")}`
  const built = useMemo(() => graph(agent, config), [structure]) // eslint-disable-line react-hooks/exhaustive-deps
  const [nodes, setNodes, onNodesChange] = useNodesState<Node>([])
  const [edges, setEdges] = useEdgesState<Edge>([])
  const [fit, setFit] = useState(false)
  const rf = useReactFlow()
  const width = useStore((s) => s.width)
  const height = useStore((s) => s.height)
  const narrow = width > 0 && width < NARROW
  const sizes = useStore((s) => s.nodes.map((n) => `${n.id}:${n.measured?.width ?? 0}x${n.measured?.height ?? 0}`).join("|"))
  const measured = useStore((s) => s.nodes.length > 0 && s.nodes.every((n) => n.measured?.width))
  const wrap = useRef<HTMLDivElement>(null)
  useWheelZoom(wrap, rf, MIN_ZOOM, MAX_ZOOM)

  useEffect(() => {
    setNodes((prev) => built.nodes.map((n) => {
      const p = prev.find((x) => x.id === n.id)
      return p ? { ...n, position: p.position, measured: p.measured, style: p.style } : { ...n, style: { opacity: 0 } }
    }))
  }, [built, setNodes])

  // Frame each layout (cards still loading resize it) until the person moves the view, clicks or selects.
  const auto = useRef(true)
  useEffect(() => { auto.current = true }, [narrow])
  useEffect(() => {
    const el = wrap.current
    const hold = () => { auto.current = false }
    el?.addEventListener("pointerdown", hold, true)
    el?.addEventListener("wheel", hold, true)
    return () => { el?.removeEventListener("pointerdown", hold, true); el?.removeEventListener("wheel", hold, true) }
  }, [])
  const frame = (to: Record<string, Pt>, size: (id: string) => Size, duration: number) => {
    const ids = Object.keys(to)
    const x0 = Math.min(...ids.map((id) => to[id]!.x)), y0 = Math.min(...ids.map((id) => to[id]!.y))
    const x1 = Math.max(...ids.map((id) => to[id]!.x + size(id).w)), y1 = Math.max(...ids.map((id) => to[id]!.y + size(id).h))
    if (!narrow) return rf.setViewport(getViewportForBounds({ x: x0, y: y0, width: x1 - x0, height: y1 - y0 }, width, height, MIN_ZOOM, 1, 0.06), { duration })
    // One column reads top down at full size; scroll to see the rest.
    const zoom = Math.min(1, (width - 24) / (x1 + 12))
    return rf.setViewport({ x: Math.max(12, (width - x1 * zoom) / 2), y: 16, zoom }, { duration })
  }

  // New cards appear in place; cards already shown glide there, their lines following.
  const glide = useRef(0)
  useEffect(() => () => cancelAnimationFrame(glide.current), [])
  const place = (to: Record<string, Pt>) => {
    cancelAnimationFrame(glide.current)
    const from = Object.fromEntries(rf.getNodes().filter((n) => n.style?.opacity === 1).map((n) => [n.id, n.position]))
    const t0 = performance.now()
    const frame = (now: number) => {
      const k = Math.min(1, (now - t0) / GLIDE_MS), e = 1 - (1 - k) ** 3
      setNodes((ns) => ns.map((n) => {
        const a = from[n.id], b = to[n.id]
        return b ? { ...n, position: a ? { x: a.x + (b.x - a.x) * e, y: a.y + (b.y - a.y) * e } : b, style: SHOWN } : n
      }))
      if (k < 1) glide.current = requestAnimationFrame(frame)
    }
    if (Object.keys(from).length) glide.current = requestAnimationFrame(frame)
    else frame(t0 + GLIDE_MS)
  }

  const last = useRef<{ to: Record<string, Pt>; size: (id: string) => Size } | null>(null)
  useEffect(() => {
    if (!measured) return
    const known = Object.fromEntries(rf.getNodes().map((n) => [n.id, { w: n.measured?.width ?? 240, h: n.measured?.height ?? 80 }]))
    const size = (id: string): Size => known[id] ?? { w: 240, h: 80 }
    const to = narrow ? layoutNarrow("agent", built.column, size) : layoutRing("agent", groups(agent, built.conns), size)
    place(to)
    last.current = { to, size }
    if (auto.current || !fit) void frame(to, size, fit ? GLIDE_MS : 0).then(() => setFit(true))
    setEdges([
      ...built.nodes.filter((n) => n.id !== "agent").map((n) => ({ id: `agent>${n.id}`, source: "agent", target: n.id, type: "line", data: { kind: narrow ? "spine" : "spoke" } })),
      ...(narrow ? [] : built.deps.map((d, i) => ({ id: `dep:${d.skill}>${d.target}`, source: "skills", target: d.target, type: "line", data: { kind: "dep", y: rowY(d.skill), i } }))),
    ])
  }, [sizes, measured, built, narrow]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (auto.current && fit && last.current) void frame(last.current.to, last.current.size, 0) }, [width, height]) // eslint-disable-line react-hooks/exhaustive-deps

  // Keep the selected node clear of the inspector; pan back when it closes. One column: the panel covers it.
  const home = useRef<{ x: number; y: number; zoom: number } | null>(null)
  useEffect(() => {
    if (selected) auto.current = false // a later reframe would put the card back under the inspector
    if (!fit || narrow) return
    if (!selected) {
      if (home.current) void rf.setViewport(home.current, { duration: 250 })
      home.current = null
      return
    }
    const id = selected.startsWith("skill:") ? "skills" : selected.startsWith("place:") ? "places" : selected.startsWith("helper:") ? "helpers" : selected.split("/")[0]!
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
        ref={wrap}
        nodes={nodes}
        edges={edges}
        onNodesChange={onNodesChange}
        nodeTypes={NODE_TYPES}
        edgeTypes={EDGE_TYPES}
        colorMode={resolved}
        minZoom={MIN_ZOOM}
        maxZoom={MAX_ZOOM}
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
