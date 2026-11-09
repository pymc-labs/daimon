import { MessageSquare, X } from "lucide-react"
import { useMemo } from "react"
import { Navigate, Route, Routes } from "react-router"

import { AgentAvatar, Ago, Money } from "@/components/common"
import { Drawer } from "@/components/Drawer"
import { Facet, FilterBar, SearchInput } from "@/components/filters"
import { QueryState } from "@/components/QueryState"
import { Page } from "@/components/shell/Page"
import { TreeTable, type TreeColumn } from "@/components/tree"
import { flatten, useTreeExpanded } from "@/components/tree-state"
import { useListParam, useParam } from "@/lib/filters"
import { channelLabel, plural, usd } from "@/lib/format"
import { useCan } from "@/lib/hooks"
import { cn } from "@/lib/utils"
import { Tag } from "@/components/panel"
import { useChannels } from "./api"
import { DefaultsTab } from "./Defaults"
import { ACCESS_FILTERS, agentOf, agentsOf, channelTree, filterChannels, type Filters, nameOf, OVERRIDES, platformLabel, TAGS, treeFilter, type ChannelNode, type Node } from "./lib"
import { ThreadSettings } from "@/modules/features/Thread"
import { ChannelPanel } from "./Panel"
import { PlatformIcon } from "./shared"
import type { AgentRefs, Channel } from "./types"

function BudgetCell({ c }: { c: Channel }) {
  const b = c.budget
  if (!b) return <span className="text-faint">–</span>
  return (
    <div className="w-28 max-sm:w-20">
      <div className={cn("text-xs tabular", b.exceeded ? "font-medium text-destructive" : "text-muted-foreground")}>{usd(b.spent_usd)} of {usd(Number(b.limit_usd))}</div>
      <div className="mt-1 h-1 overflow-hidden rounded-full bg-foreground/10">
        <div className={cn("h-full rounded-full", b.exceeded ? "bg-destructive" : b.percent_used >= 80 ? "bg-warning" : "bg-teal")} style={{ width: `${Math.min(100, b.percent_used)}%` }} />
      </div>
    </div>
  )
}

const icon = "size-4 shrink-0 text-muted-foreground"

/** A thread or post gets a speech bubble and no "#"; roots and channels the platform's mark. */
function NameCell({ node, agents }: { node: ChannelNode; agents: AgentRefs }) {
  const r = node.row
  if (r.type === "group") {
    const label = r.kind === "channel" ? channelLabel(r.name, r.kind) : (r.name ?? "Unnamed")
    return <>{(node.parentId === null || r.kind === "channel") && <PlatformIcon platform={r.platform} className={icon} />}<span className="truncate">{label}</span></>
  }
  const c = r.channel
  const agent = agentOf(agents, c.agent.value)
  return (
    <>
      {c.kind === "thread" || c.kind === "forum_post" ? <MessageSquare className={icon} /> : <PlatformIcon platform={c.platform} className={icon} />}
      <span className="min-w-0">
        <span className="block truncate font-medium">{nameOf(c)}</span>
        <span className="block truncate text-xs font-normal text-muted-foreground sm:hidden">{agent?.name ?? (c.agent.value ? "Deleted agent" : "None")}</span>
      </span>
    </>
  )
}

const channelOf = <T,>(n: Node, f: (c: Channel) => T) => (n.type === "channel" ? f(n.channel) : null)

function columns(agents: AgentRefs, figures: boolean): TreeColumn<Node>[] {
  return [
    { key: "channel", header: "Channel", render: (_, node) => <NameCell node={node} agents={agents} /> },
    {
      key: "answers", header: "Answers", className: "max-sm:hidden",
      render: (n) => channelOf(n, (c) => {
        const a = agentOf(agents, c.agent.value)
        if (!a) return <span className="text-faint">–</span>
        return (
          <span className={cn("flex max-w-44 items-center gap-1.5", c.agent.source !== "channel" && "text-muted-foreground")}>
            <AgentAvatar src={a.avatar} name={a.name ?? "?"} className="size-4 text-[8px]" /><span className="truncate">{a.name ?? "Deleted agent"}</span>
          </span>
        )
      }),
    },
    {
      key: "access", header: "Access", className: "max-lg:hidden",
      render: (n) => channelOf(n, (c) => <span className="flex flex-wrap gap-1">{c.tags.map((t) => <Tag key={t}>{TAGS[t]}</Tag>)}</span>),
    },
    ...(figures ? [
      { key: "budget", header: "Budget", render: (n: Node) => channelOf(n, (c) => <BudgetCell c={c} />) },
      { key: "days", header: <span className="block text-right">Last 30 days</span>, className: "max-sm:hidden", render: (n: Node) => channelOf(n, (c) => <span className="block text-right text-muted-foreground">{c.spend && <Money usd={c.spend.last_30_days_usd} />}</span>) },
    ] : []),
    { key: "active", header: <span className="block text-right">Last active</span>, className: "max-sm:hidden", render: (n) => channelOf(n, (c) => <span className="block text-right text-muted-foreground"><Ago iso={c.last_active_at} /></span>) },
  ]
}

function countText(node: ChannelNode, n: number) {
  const kind = node.row.type === "channel" ? node.row.channel.kind : node.row.kind
  return plural(n, kind === "category" || kind === "team" ? "channel" : "thread")
}

function ChannelsTab() {
  const query = useChannels()
  const [q, setQ] = useParam("q")
  const [platforms, setPlatforms] = useListParam("platform")
  const [access, setAccess] = useListParam("access")
  const [budget, setBudget] = useListParam("budget")
  const [agents, setAgents] = useListParam("agent")
  const [override, setOverride] = useParam("override")
  const [, setOpen] = useParam("channel")
  const [expanded, toggle] = useTreeExpanded("channels")
  const all = useMemo(() => query.data?.channels ?? [], [query.data])
  const [pk, ak, bk, gk] = [platforms, access, budget, agents].map((l) => JSON.stringify(l))
  const filters = useMemo<Filters>(
    () => ({ q, platforms: JSON.parse(pk!), access: JSON.parse(ak!), budget: JSON.parse(bk!), agents: JSON.parse(gk!), override }),
    [q, pk, ak, bk, gk, override],
  )
  const tree = useMemo(() => channelTree(all), [all])
  const filter = useMemo(() => treeFilter(filters), [filters])
  const matches = useMemo(() => filterChannels(all, filters).length, [all, filters])
  const ids = useMemo(() => flatten(tree, expanded, filter).filter((f) => f.node.kind === "item").map((f) => f.node.id), [tree, expanded, filter])
  const refs = query.data?.agents ?? {}
  const figures = all.some((c) => c.spend)
  const platformNames = [...new Set(all.map((c) => c.platform))]

  return (
    <>
      <FilterBar>
        <SearchInput value={q} onChange={(v) => setQ(v || null)} className="w-56" />
        {platformNames.length > 1 && (
          <Facet label="Platform" value={platforms} onChange={setPlatforms} options={platformNames.map((p) => ({ value: p, label: platformLabel(p), icon: <PlatformIcon platform={p} className="size-3.5" /> }))} />
        )}
        <Facet label="Access" value={access} onChange={setAccess} options={ACCESS_FILTERS} />
        {figures && <Facet label="Budget" value={budget} onChange={setBudget} options={[{ value: "yes", label: "Has budget" }, { value: "no", label: "No budget" }]} />}
        <Facet label="Agent" value={agents} onChange={setAgents} options={agentsOf(all, refs).map((a) => ({ value: a.key, label: a.name }))} />
        {override && OVERRIDES[override] && (
          <button
            type="button" onClick={() => setOverride(null)} aria-label={`Clear: sets ${OVERRIDES[override].toLowerCase()}`}
            className="inline-flex h-8 items-center gap-1.5 rounded-md border bg-accent/50 px-2.5 text-xs hover:bg-accent"
          >Sets {OVERRIDES[override].toLowerCase()}<X className="size-3.5" /></button>
        )}
        <span className="ml-auto text-xs text-muted-foreground tabular">{plural(matches, "channel")}</span>
      </FilterBar>
      <QueryState query={query} className="px-6">{() => !matches ? <div className="py-16 text-center text-sm text-muted-foreground">No channels</div> : (
        <TreeTable
          rows={tree} columns={columns(refs, figures)} expanded={expanded} onToggle={toggle} filter={filter} count={countText}
          onOpen={(row) => row.type === "channel" && setOpen(row.channel.id)}
        />
      )}</QueryState>
      <Drawer param="channel" title="Channel" ids={ids} wide>{(id) => <ChannelPanel id={id} />}</Drawer>
    </>
  )
}

export function Channels() {
  const can = useCan()
  const manage = can("channels.manage")
  return (
    <Page crumbs={[{ label: "Channels" }]} tabs={manage ? [{ label: "Channels", to: "/channels", end: true }, { label: "Defaults", to: "/channels/defaults" }] : undefined} flush>
      <Routes>
        <Route index element={<ChannelsTab />} />
        <Route path="defaults" element={manage ? <DefaultsTab /> : <Navigate to="/channels" replace />} />
        <Route path=":channel/threads/:thread" element={<ThreadSettings />} />
        <Route path="*" element={<Navigate to="/channels" replace />} />
      </Routes>
    </Page>
  )
}
