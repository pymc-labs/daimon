import { useQuery } from "@tanstack/react-query"
import { ExternalLink, Link2 } from "lucide-react"
import { useNavigate } from "react-router"
import { toast } from "sonner"

import { AgentAvatar, Ago } from "@/components/common"
import { Facet, FilterBar, SearchInput } from "@/components/filters"
import { QueryState } from "@/components/QueryState"
import { Page } from "@/components/shell/Page"
import { Button } from "@/components/ui/button"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { api } from "@/lib/api"
import { rowProps, useListParam, useParam } from "@/lib/filters"
import { plural } from "@/lib/format"
import { Pill } from "../agents/bits"
import { AccessLabel, ACCESS, expiry, fullUrl, KEY, UseTags } from "./bits"
import type { ArtifactSummary } from "./types"
import { Thumb } from "./Viewing"

const ownerOf = (a: ArtifactSummary) => (a.mine ? "You" : a.owner.name)

/** Rows that match the search text and every picked facet. */
export function filterArtifacts(all: ArtifactSummary[], f: { q: string; agents: string[]; owners: string[]; access: string[] }) {
  const q = f.q.toLowerCase()
  return all.filter((a) =>
    (!q || `${a.title} ${a.agent.name}`.toLowerCase().includes(q))
    && (!f.agents.length || f.agents.includes(a.agent.id)) && (!f.owners.length || f.owners.includes(ownerOf(a))) && (!f.access.length || f.access.includes(a.access)))
}

function RowActions({ a }: { a: ArtifactSummary }) {
  const tip = (label: string, child: React.ReactNode) => (
    <Tooltip><TooltipTrigger asChild>{child}</TooltipTrigger><TooltipContent>{label}</TooltipContent></Tooltip>
  )
  return (
    <span className="flex justify-end gap-0.5 opacity-0 transition-opacity group-focus-within/row:opacity-100 group-hover/row:opacity-100">
      {tip("Copy link", <Button variant="ghost" size="icon-sm" aria-label="Copy link" onClick={(e) => { e.stopPropagation(); void navigator.clipboard?.writeText(fullUrl(a.url)); toast("Link copied") }}><Link2 /></Button>)}
      {tip("Open in new tab", <Button variant="ghost" size="icon-sm" aria-label="Open in new tab" asChild><a href={a.url} target="_blank" rel="noreferrer" onClick={(e) => e.stopPropagation()}><ExternalLink /></a></Button>)}
    </span>
  )
}

function ArtifactTable({ rows: all }: { rows: ArtifactSummary[] }) {
  const navigate = useNavigate()
  const [q, setQ] = useParam("q")
  const [agents, setAgents] = useListParam("agent")
  const [owners, setOwners] = useListParam("owner")
  const [access, setAccess] = useListParam("access")
  const rows = filterArtifacts(all, { q, agents, owners, access })
  const agentOptions = [...new Map(all.map((a) => [a.agent.id, a.agent.name])).entries()].map(([value, label]) => ({ value, label }))
  const ownerOptions = [...new Set(all.map(ownerOf))].map((o) => ({ value: o, label: o }))
  return (
    <>
      <FilterBar>
        <SearchInput value={q} onChange={(v) => setQ(v || null)} className="w-56" />
        <Facet label="Agent" value={agents} onChange={setAgents} options={agentOptions} />
        <Facet label="Owner" value={owners} onChange={setOwners} options={ownerOptions} />
        <Facet label="Access" value={access} onChange={setAccess} options={Object.entries(ACCESS).map(([value, { label }]) => ({ value, label }))} />
        <span className="ml-auto text-xs text-muted-foreground tabular">{plural(rows.length, "artifact")}</span>
      </FilterBar>
      <div className="mx-auto max-w-[1040px] px-4 lg:px-6">
        {!rows.length ? <div className="py-16 text-center text-sm text-muted-foreground">No artifacts match</div> : (
          <Table>
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead>Artifact</TableHead><TableHead className="max-sm:hidden">Who can open</TableHead><TableHead className="max-md:hidden">Uses</TableHead>
                <TableHead className="max-sm:hidden">Updated</TableHead><TableHead className="max-lg:hidden">Owner</TableHead><TableHead className="w-[72px] max-sm:hidden"><span className="sr-only">Actions</span></TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((a) => {
                const left = expiry(a.expires_at)
                return (
                  <TableRow key={a.id} className="group/row" {...rowProps(() => navigate(`/artifacts/${a.id}`))}>
                    <TableCell className="max-w-80">
                      <span className="flex min-w-0 items-center gap-3">
                        <Thumb id={a.id} version={a.version} className="w-12 max-sm:hidden" />
                        <span className="block min-w-0">
                          <span className="flex items-center gap-2"><span className="truncate font-medium">{a.title}</span>{left && <Pill>{left === "Expired" ? left : `Expires in ${left.replace(" left", "")}`}</Pill>}</span>
                          <span className="mt-0.5 flex items-center gap-1.5 text-xs text-muted-foreground"><AgentAvatar src={a.agent.avatar} name={a.agent.name} className="size-4 text-[8px]" /><span className="truncate">{a.agent.name}</span></span>
                        </span>
                      </span>
                    </TableCell>
                    <TableCell className="max-sm:hidden"><AccessLabel access={a.access} count={a.people_count} /></TableCell>
                    <TableCell className="max-md:hidden"><UseTags a={a} /></TableCell>
                    <TableCell className="max-sm:hidden"><Ago iso={a.updated_at} /></TableCell>
                    <TableCell className="text-muted-foreground max-lg:hidden">{ownerOf(a)}</TableCell>
                    <TableCell className="max-sm:hidden"><RowActions a={a} /></TableCell>
                  </TableRow>
                )
              })}
            </TableBody>
          </Table>
        )}
      </div>
    </>
  )
}

export function Artifacts() {
  const list = useQuery({ queryKey: [...KEY, "list"], queryFn: () => api.get<ArtifactSummary[]>("/api/artifacts") })
  return (
    <Page crumbs={[{ label: "Artifacts" }]}>
      <QueryState query={list} empty="No artifacts">{(rows) => <ArtifactTable rows={rows} />}</QueryState>
    </Page>
  )
}
