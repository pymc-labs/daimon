import "@xyflow/react/dist/style.css"
import {
  Background, BackgroundVariant, EdgeLabelRenderer, type Edge, type EdgeProps, getViewportForBounds, Handle, type Node as FlowNode, type NodeProps, Position,
  ReactFlow, useReactFlow, useStore,
} from "@xyflow/react"
import { CircleAlert, Copy, KeyRound, type LucideIcon, Maximize, Minus, MoreHorizontal, PenLine, Plus, ShieldAlert, ShieldOff, Trash2 } from "lucide-react"
import { createContext, useContext, useEffect, useMemo, useRef, useState } from "react"

import { Command, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList } from "@/components/ui/command"
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger } from "@/components/ui/dropdown-menu"
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover"
import { useTheme } from "@/lib/theme-context"
import { cn } from "@/lib/utils"
import { badgesOf, type BadgeKind, type CardBadge, cardHeight, MAX_BADGES } from "./cards"
import { layout, NODE_W, pathOf, TRIGGERS, type LaidEdge, type Layout } from "./layout"
import { titleOf, type Preset } from "./spec"
import type { EdgeState, NodeRun } from "./runs"
import { Dot } from "./ui"
import { KINDS, type Catalog, type Kind, type Spec, type Validation } from "./types"
import { factsOf, KIND, STEP, TRIGGER, triggerText } from "./kinds"
import { duration } from "@/lib/format"
import { useWheelZoom } from "./zoom"

export const PANEL_W = 440
/** Side panel over a canvas: a full-height sheet on phones, a docked column from `sm`. */
export const PANEL = "fixed inset-0 z-50 flex flex-col bg-popover sm:absolute sm:inset-y-0 sm:left-auto sm:z-20 sm:w-[440px] sm:border-l sm:shadow-[-12px_0_32px_rgb(12_31_64/0.06)]"
const MIN_ZOOM = 0.3, MAX_ZOOM = 1.5
export type InsertAt = { from: string | null; slot?: string; to?: string }
/** A run's state over the graph: status per node, taken edges, and whether the trigger fired. */
export type RunOverlay = { nodes: Record<string, NodeRun>; edges: Record<string, EdgeState>; started: boolean; ended?: boolean }

type Ctx = {
  spec: Spec; validation?: Validation; catalog?: Catalog; changed: Set<string>; flash: Set<string>; selected: string | null; ro: boolean
  select: (id: string | null) => void; add: (at: InsertAt, kind: Kind, preset?: Preset) => void; remove: (id: string) => void; duplicate: (id: string) => void
  hover: string | null; setHover: (id: string | null) => void; menu: string | null; setMenu: (k: string | null) => void
  run?: RunOverlay
}
const C = createContext<Ctx>(null!)


const BADGE: Record<BadgeKind, { icon: LucideIcon; label: string; cls?: string }> = {
  problems: { icon: CircleAlert, label: "Problems", cls: "border-destructive/40 text-destructive" },
  writes: { icon: PenLine, label: "Writes" },
  grant: { icon: KeyRound, label: "Grant" },
  untrusted: { icon: ShieldAlert, label: "Untrusted", cls: "border-warning/40 text-warning" },
  waiver: { icon: ShieldOff, label: "Waiver", cls: "border-destructive/40 text-destructive" },
}
const badgeCls = "inline-flex h-[18px] shrink-0 items-center gap-1 rounded border bg-background px-1 text-[10.5px] font-medium whitespace-nowrap text-muted-foreground"

function Badges({ items }: { items: CardBadge[] }) {
  const shown = items.length > MAX_BADGES ? items.slice(0, MAX_BADGES - 1) : items
  const rest = items.slice(shown.length)
  return (
    <span className="mt-2 flex h-[18px] min-w-0 shrink-0 items-center gap-1 overflow-hidden pl-6">
      {shown.map((b) => {
        const B = BADGE[b.kind]
        // Problems show their count; the badge's tooltip and label say what it counts.
        const text = b.kind === "problems" ? String(b.count) : B.label
        return <span key={b.kind} className={cn(badgeCls, B.cls)} title={b.kind === "problems" ? `${b.count} ${b.count === 1 ? "problem" : "problems"}` : B.label}><B.icon className="size-3" />{text}</span>
      })}
      {rest.length > 0 && <span className={badgeCls} title={rest.map((b) => BADGE[b.kind].label).join(", ")}>+{rest.length}</span>}
    </span>
  )
}

function StepCard({ id }: NodeProps) {
  const { spec, validation, catalog, changed, flash, selected, ro, remove, duplicate, run } = useContext(C)
  const n = spec.nodes[id]!
  const K = KIND[n.kind]
  const badges = run ? [] : badgesOf(id, n, validation)
  const r = run?.nodes[id]
  const st = run ? r?.status ?? "pending" : null
  const missed = !!run?.ended && !r
  return (
    <div
      style={{ width: NODE_W, height: cardHeight(badges) }}
      className={cn(
        "group relative flex cursor-pointer flex-col justify-center overflow-hidden rounded-xl border bg-card py-2.5 pr-2 pl-4 shadow-[0_1px_3px_rgb(0_0_0/0.05)] transition-[border-color,box-shadow,background-color] duration-500 hover:border-border-strong",
        st && (missed ? "border-dashed opacity-50" : RING[st]),
        selected === id && "border-ring ring-3 ring-ring/15 hover:border-ring",
        flash.has(id) && "border-link bg-link/5 ring-4 ring-link/25 duration-150",
      )}
    >
      {st === "running" && <span className="pointer-events-none absolute inset-0 animate-pulse rounded-xl ring-4 ring-link/30 ring-inset" />}
      <Handle type="target" position={Position.Top} className="!pointer-events-none !opacity-0" />
      <span className={cn("absolute inset-y-0 left-0 w-1", K.strip)} />
      <span className="flex h-6 min-w-0 shrink-0 items-center gap-2">
        <K.icon className={cn("size-4 shrink-0", K.text)} />
        <span className="truncate text-sm font-medium">{titleOf(id, n)}</span>
        {changed.has(id) && <Dot />}
        {!ro && (
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <button className="nodrag ml-auto flex size-6 shrink-0 items-center justify-center rounded-md text-muted-foreground opacity-0 group-hover:opacity-100 hover:bg-accent data-[state=open]:opacity-100 [&_svg]:size-4" onClick={(e) => e.stopPropagation()} aria-label="Step menu">
                <MoreHorizontal />
              </button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" onClick={(e) => e.stopPropagation()}>
              <DropdownMenuItem onSelect={() => duplicate(id)}><Copy />Duplicate</DropdownMenuItem>
              <DropdownMenuSeparator />
              <DropdownMenuItem variant="destructive" disabled={id === spec.start && Object.keys(spec.nodes).length === 1} onSelect={() => remove(id)}><Trash2 />Delete</DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        )}
      </span>
      {missed ? <span className="mt-0.5 h-4 pl-6 text-xs text-faint">Not reached</span> : st ? <RunLine st={st} r={r} /> : (
        <span className="mt-0.5 flex h-4 min-w-0 shrink-0 items-center gap-2.5 pl-6 text-xs text-muted-foreground">
          {factsOf(n, spec, catalog).map((f, i) => <span key={i} className={cn(i === 0 ? "truncate" : "shrink-0 text-faint")}>{f}</span>)}
        </span>
      )}
      {badges.length > 0 && <Badges items={badges} />}
      <Handle type="source" position={Position.Bottom} className="!pointer-events-none !opacity-0" />
    </div>
  )
}

/** Card treatment per step status; the status line carries icon and text, so color is never alone. */
const RING: Record<NodeRun["status"], string> = {
  pending: "border-dashed bg-card/70", ready: "border-dashed", running: "border-link", waiting: "border-warning ring-3 ring-warning/15",
  succeeded: "", failed: "border-destructive ring-3 ring-destructive/15", unknown: "border-destructive ring-3 ring-destructive/15",
  skipped: "border-dashed opacity-60", cancelled: "opacity-70",
}

function RunLine({ st, r }: { st: NodeRun["status"]; r?: NodeRun }) {
  const S = STEP[st]
  return (
    <span className="mt-0.5 flex h-4 min-w-0 shrink-0 items-center gap-2.5 pl-6 text-xs text-muted-foreground">
      <span className={cn("flex items-center gap-1", S.cls)}><S.icon className="size-3" /><span className="text-foreground">{S.label}</span></span>
      {r?.progress && <span className="tabular">{r.progress.done} of {r.progress.total}</span>}
      {r && r.visits > 1 && <span className="tabular" title={`${r.visits} visits`}>×{r.visits}</span>}
      {r?.reused && <span className="text-faint">{r.reused === "pinned" ? "Pinned" : "Reused"}</span>}
      {r?.ms != null && !r.reused && <span className="ml-auto shrink-0 pr-1 text-faint tabular">{duration(r.ms)}</span>}
    </span>
  )
}

function TriggerChips() {
  const { spec } = useContext(C)
  return (
    <div className="flex h-[30px] items-center justify-center gap-1.5">
      {(spec.triggers ?? []).map((t) => {
        const Icon = TRIGGER[t.kind].icon
        return <span key={t.id} className="flex h-[30px] items-center gap-1.5 rounded-full border bg-popover px-3 text-xs whitespace-nowrap text-muted-foreground shadow-[0_1px_2px_rgb(0_0_0/0.04)]"><Icon className="size-3.5" />{triggerText(t)}</span>
      })}
      <Handle type="source" position={Position.Bottom} className="!pointer-events-none !opacity-0" />
    </div>
  )
}

/** The kind menu behind every `+`. */
function AddMenu({ k, at, className, children }: { k: string; at: InsertAt; className?: string; children: React.ReactNode }) {
  const { menu, setMenu, add } = useContext(C)
  return (
    <Popover open={menu === k} onOpenChange={(o) => setMenu(o ? k : null)}>
      <PopoverTrigger asChild>
        <button className={cn("nodrag nopan flex size-6 items-center justify-center rounded-full border bg-popover text-muted-foreground shadow-[0_1px_3px_rgb(0_0_0/0.08)] hover:border-border-strong hover:text-foreground", menu === k && "border-ring text-foreground", className)}
          onClick={(e) => e.stopPropagation()} aria-label="Add step">
          {children}
        </button>
      </PopoverTrigger>
      <PopoverContent className="w-56 p-0" align="start" side="right" sideOffset={8} onClick={(e) => e.stopPropagation()}>
        <Command>
          <CommandInput placeholder="Add step" className="h-9" />
          <CommandList>
            <CommandEmpty>No match</CommandEmpty>
            <CommandGroup>
              {KINDS.map((kind) => {
                const K = KIND[kind]
                return <CommandItem key={kind} value={K.label} onSelect={() => { setMenu(null); add(at, kind) }}><K.icon className={K.text} />{K.label}</CommandItem>
              })}
              <CommandItem value="Ask a person" onSelect={() => { setMenu(null); add(at, "approval", "ask") }}><KIND.approval.icon className={KIND.approval.text} />Ask a person</CommandItem>
            </CommandGroup>
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  )
}

function Leaf({ data }: NodeProps<FlowNode<{ from: string }>>) {
  const { ro } = useContext(C)
  return (
    <div className="flex w-6 flex-col items-center">
      <span className="h-3 w-px bg-border-strong" />
      {!ro && <AddMenu k={`leaf:${data.from}`} at={{ from: data.from }}><Plus className="size-3.5" /></AddMenu>}
    </div>
  )
}

function FlowEdge({ id, data }: EdgeProps<Edge<LaidEdge & { maxVisits?: number }>>) {
  const { ro, hover, setHover, menu, run } = useContext(C)
  const e = data!
  const state: EdgeState | null = !run ? null : e.from === TRIGGERS ? (run.started ? "taken" : "pending") : run.edges[id] ?? "pending"
  const pts = e.points
  const a = pts.at(-2)!, b = pts.at(-1)!
  const dx = Math.sign(b.x - a.x), dy = Math.sign(b.y - a.y)
  const tip = `M${b.x},${b.y} L${b.x - dy * 4 - dx * 7},${b.y - dx * 4 - dy * 7} L${b.x + dy * 4 - dx * 7},${b.y + dx * 4 - dy * 7} Z`
  const stroke = e.kind === "error" ? "var(--destructive)" : state === "taken" ? "var(--muted-foreground)" : "var(--border-strong)"
  const faint = state === "not_taken"
  const k = `edge:${id}`
  const showPlus = !ro && !e.back && (hover === id || menu === k)
  const at: InsertAt = e.from === TRIGGERS ? { from: null, to: e.to } : { from: e.from, slot: e.slot, to: e.to }
  return (
    <>
      <g opacity={faint ? 0.4 : 1} className="transition-opacity duration-500">
        <path d={pathOf(pts)} fill="none" stroke={stroke} strokeWidth={state === "taken" ? 2 : 1.5} className="transition-[stroke] duration-500"
          strokeDasharray={e.back || faint ? "5 4" : e.kind === "error" ? "3 3" : undefined} />
        <path d={tip} fill={stroke} />
      </g>
      <path d={pathOf(pts)} fill="none" stroke="transparent" strokeWidth={18} onMouseEnter={() => setHover(id)} onMouseLeave={() => setHover(null)} />
      <EdgeLabelRenderer>
        {e.labelAt && (e.label || e.back) && (
          <span
            style={{ transform: `translate(-50%,-50%) translate(${e.labelAt.x}px,${e.labelAt.y}px)` }}
            className={cn("absolute flex h-5 items-center rounded border bg-popover px-1.5 text-[11px] font-medium whitespace-nowrap text-muted-foreground",
              e.back && "border-dashed", e.kind === "error" && "text-destructive", faint && "opacity-50", state === "taken" && "border-border-strong text-foreground")}
          >
            {e.label}
            {e.label && e.back && <span className="mx-1.5 h-3 w-px bg-border" />}
            {e.back && <span className="tabular">max {e.maxVisits ?? 3}</span>}
          </span>
        )}
        {showPlus && (
          <span style={{ transform: `translate(-50%,-50%) translate(${e.plusAt.x}px,${e.plusAt.y}px)` }} className="pointer-events-auto absolute"
            onMouseEnter={() => setHover(id)} onMouseLeave={() => setHover(null)}>
            <AddMenu k={k} at={at}><Plus className="size-3.5" /></AddMenu>
          </span>
        )}
      </EdgeLabelRenderer>
    </>
  )
}

const NODE_TYPES = { step: StepCard, triggers: TriggerChips, leaf: Leaf }
const EDGE_TYPES = { flow: FlowEdge }

export type CanvasProps = Omit<Ctx, "hover" | "setHover" | "menu" | "setMenu"> & { panel: boolean; /** Pixels of the canvas the open panel covers. */ overlap?: number; tools?: React.ReactNode }

export function Canvas({ panel, overlap = PANEL_W, tools, ...props }: CanvasProps) {
  const { spec, select, selected } = props
  const { resolved } = useTheme()
  const rf = useReactFlow()
  const wrap = useRef<HTMLDivElement>(null)
  useWheelZoom(wrap, rf, MIN_ZOOM, MAX_ZOOM)
  const [hover, setHoverNow] = useState<string | null>(null)
  const [menu, setMenu] = useState<string | null>(null)
  const timer = useRef<ReturnType<typeof setTimeout>>(undefined)
  const setHover = (id: string | null) => {
    clearTimeout(timer.current)
    if (id) setHoverNow(id)
    else timer.current = setTimeout(() => setHoverNow(null), 120)
  }
  const chipKey = (spec.triggers ?? []).map(triggerText).join("\n")
  const { validation } = props
  const runView = !!props.run
  const laid: Layout = useMemo(() => {
    const heights = Object.fromEntries(Object.entries(spec.nodes).map(([id, n]) => [id, cardHeight(runView ? [] : badgesOf(id, n, validation))]))
    return layout(spec, chipKey ? chipKey.split("\n") : [], heights)
  }, [spec, chipKey, validation, runView])
  const nodes = useMemo<FlowNode[]>(() => [
    ...laid.nodes.map((b) => ({ id: b.id, type: b.id === TRIGGERS ? "triggers" : "step", position: { x: b.x, y: b.y }, data: {}, width: b.w, height: b.h, ...(b.id === TRIGGERS && { style: { width: b.w } }) })),
    ...laid.leaves.map((l) => ({ id: `leaf:${l.id}`, type: "leaf", position: { x: l.at.x - 12, y: l.at.y - 24 }, data: { from: l.id }, width: 24, height: 36, selectable: false })),
  ], [laid])
  const edges = useMemo<Edge[]>(() => laid.edges.map((e) => ({
    id: e.id, source: e.from, target: e.to, type: "flow", data: { ...e, maxVisits: spec.nodes[e.to]?.max_visits },
  })), [laid, spec])

  // Frame the graph once: fit, but never below a readable zoom; a tall graph starts at its top.
  const width = useStore((s) => s.width), height = useStore((s) => s.height)
  const framed = useRef(false)
  const frame = (duration = 0) => {
    if (!width || !height) return
    if (width < 640) {
      // Phones read top down: fit the width, scroll for the rest.
      const zoom = Math.min(1, Math.max(MIN_ZOOM, (width - 24) / laid.width))
      return void rf.setViewport({ x: (width - laid.width * zoom) / 2, y: 64, zoom }, { duration })
    }
    const room = width - (panel ? overlap : 0)
    const vp = getViewportForBounds({ x: -40, y: -20, width: laid.width + 80, height: laid.height + 40 }, room, height - 72, MIN_ZOOM, 1, 0.04)
    const floor = runView ? 0.5 : 0.72 // a run reads best whole
    const zoom = Math.max(floor, vp.zoom)
    const x = (room - laid.width * zoom) / 2
    const y = vp.zoom < floor ? 64 : Math.max(64, (height - laid.height * zoom) / 2 - 8)
    void rf.setViewport({ x, y, zoom }, { duration })
  }
  useEffect(() => { if (!framed.current && width) { frame(); framed.current = true } }, [width, height, laid]) // eslint-disable-line react-hooks/exhaustive-deps

  // Keep the selected step clear of the panel.
  useEffect(() => {
    if (!selected || !width || !panel) return
    const b = laid.nodes.find((n) => n.id === selected)
    if (!b) return
    const vp = rf.getViewport()
    const left = b.x * vp.zoom + vp.x, right = left + b.w * vp.zoom, top = b.y * vp.zoom + vp.y, bottom = top + b.h * vp.zoom
    const room = width - overlap - 24
    const dx = right > room ? Math.max(room - right, 24 - left) : left < 24 ? 24 - left : 0
    const dy = bottom > height - 80 ? height - 80 - bottom : top < 64 ? 64 - top : 0
    if (dx || dy) void rf.setViewport({ ...vp, x: vp.x + dx, y: vp.y + dy }, { duration: 250 })
  }, [selected]) // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <C.Provider value={{ ...props, hover, setHover, menu, setMenu }}>
      <ReactFlow
        ref={wrap}
        nodes={nodes}
        edges={edges}
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
        onNodeClick={(_, n) => n.type === "step" && select(n.id)}
        onPaneClick={() => { setMenu(null); if (selected) select(null) }}
        proOptions={{ hideAttribution: true }}
        style={{ background: "transparent" }}
      >
        <Background variant={BackgroundVariant.Dots} gap={20} size={1.2} color="var(--border-strong)" />
        <Zoom fit={() => frame(200)} right={panel ? overlap + 16 : 16} tools={tools} />
      </ReactFlow>
    </C.Provider>
  )
}

/** Zoom controls top right, after any canvas tools. */
function Zoom({ fit, right, tools }: { fit: () => void; right: number; tools?: React.ReactNode }) {
  const rf = useReactFlow()
  const zoom = useStore((s) => s.transform[2])
  const b = "flex size-8 items-center justify-center text-muted-foreground hover:bg-accent hover:text-foreground [&_svg]:size-4"
  return (
    <div style={{ right }} className="absolute top-4 z-10 flex items-center gap-2">
      {tools}
      <div className="flex items-center overflow-hidden rounded-lg border bg-popover shadow-[0_1px_3px_rgb(0_0_0/0.06)]">
      <button className={b} onClick={() => rf.zoomOut({ duration: 150 })} aria-label="Zoom out"><Minus /></button>
      <button className="h-8 w-12 text-xs text-muted-foreground tabular hover:bg-accent" onClick={() => rf.zoomTo(1, { duration: 150 })} aria-label="Reset zoom">{Math.round(zoom * 100)}%</button>
      <button className={b} onClick={() => rf.zoomIn({ duration: 150 })} aria-label="Zoom in"><Plus /></button>
      <span className="h-5 w-px bg-border" />
      <button className={b} onClick={fit} aria-label="Fit"><Maximize /></button>
      </div>
    </div>
  )
}
