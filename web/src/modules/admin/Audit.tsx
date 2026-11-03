import { useQuery } from "@tanstack/react-query"

import { Ago, Avatar } from "@/components/common"
import { Button } from "@/components/ui/button"
import { Drawer } from "@/components/Drawer"
import { ChannelLink, PersonLink } from "@/components/links"
import { Link } from "react-router"
import { Facet, FilterBar } from "@/components/filters"
import { rowProps, useParam } from "@/lib/filters"
import { LoadMore, QueryState } from "@/components/QueryState"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { api } from "@/lib/api"
import { absolute } from "@/lib/format"
import { usePaged } from "@/lib/hooks"
import { ScrollText } from "lucide-react"
import { toast } from "sonner"
import { Facts, PanelHead, PanelSection } from "@/components/panel"
import type { AdminPerson, AuditEntry, AuditKind, AuditSource, AuditTarget } from "./types"

// Audit

const SOURCES: Record<AuditSource, string> = { web: "Web app", security: "Chat and panels" }

const dateInput = "h-7 rounded-md border bg-background px-2 text-xs outline-none focus:border-border-strong"

export function Audit() {
  const [source, setSource] = useParam("source")
  const [actor, setActor] = useParam("actor")
  const [kind, setKind] = useParam("kind")
  const [from, setFrom] = useParam("from")
  const [to, setTo] = useParam("to")
  const filters = { source, actor, kind, from, to }
  const path = "/api/admin/audit" + query(filters)
  const q = usePaged<AuditEntry>(["admin", "audit", filters], path)
  const people = usePaged<AdminPerson>(["admin", "people"], "/api/admin/people")
  const kinds = useQuery({ queryKey: ["admin", "audit", "kinds"], queryFn: () => api.get<AuditKind[]>("/api/admin/audit/kinds"), staleTime: 60_000 })
  const shown = (kinds.data ?? []).filter((k) => !source || k.source === source)
  return (
    <>
      <FilterBar className="px-0 pt-0">
        <Facet single label="Source" value={source ? [source] : []} onChange={([v]) => setSource(v ?? null)}
          options={Object.entries(SOURCES).map(([value, label]) => ({ value, label }))} />
        <Facet single label="Who" value={actor ? [actor] : []} onChange={([v]) => setActor(v ?? null)}
          options={(people.data ?? []).map((p) => ({ value: p.id, label: p.name }))} />
        <Facet single label="Action" value={kind ? [kind] : []} onChange={([v]) => setKind(v ?? null)}
          options={shown.map((k) => ({ value: k.value, label: k.label }))} />
        <input type="date" aria-label="From" value={from} max={to || undefined} onChange={(e) => setFrom(e.target.value || null)} className={dateInput} />
        <input type="date" aria-label="To" value={to} min={from || undefined} onChange={(e) => setTo(e.target.value || null)} className={dateInput} />
      </FilterBar>
      <QueryState query={q} empty="No activity">{(data) => <><AuditTable data={data} /><LoadMore list={q} /></>}</QueryState>
    </>
  )
}

function query(filters: Record<string, string>) {
  const p = new URLSearchParams()
  for (const [k, v] of Object.entries(filters)) if (v) p.set(k, v)
  const s = p.toString()
  return s ? `?${s}` : ""
}

function Target({ t }: { t: AuditTarget }) {
  if (t.kind === "person") return <PersonLink id={t.id} name={t.name} />
  if (t.kind === "invite" && t.id) return <Link to={`/admin/invites?invite=${encodeURIComponent(t.id)}`} className="hover:underline">{t.name}</Link>
  return t.name || null
}

function AuditTable({ data }: { data: AuditEntry[] }) {
  const [, setEntry] = useParam("entry")
  return (
    <>
    <Table>
      <TableHeader>
        <TableRow className="hover:bg-transparent">
          <TableHead className="w-28">When</TableHead><TableHead>Who</TableHead><TableHead>Action</TableHead><TableHead>Target</TableHead><TableHead className="max-md:hidden">Detail</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {data.map((e) => (
          <TableRow key={e.id} {...rowProps(() => setEntry(e.id))}>
            <TableCell className="text-muted-foreground"><Ago iso={e.at} /></TableCell>
            <TableCell><span className="flex items-center gap-2"><Avatar name={e.who} className="size-5 text-[9px]" />{e.who}</span></TableCell>
            <TableCell>{e.action}</TableCell>
            <TableCell className="font-medium">{e.target.name}</TableCell>
            <TableCell className="text-muted-foreground max-md:hidden">{e.detail ?? ""}</TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
    <Drawer param="entry" title="Activity" ids={data.map((e) => e.id)}>
      {(id) => {
        const e = data.find((x) => x.id === id)
        return e && (
          <div>
            <PanelHead icon={<ScrollText className="size-4 text-muted-foreground" />} title={e.action} subtitle={e.target.name} />
            <PanelSection>
              <Facts rows={[
                ["Source", SOURCES[e.source]],
                ["Who", e.actor_id ? <PersonLink key="who" id={e.actor_id} name={e.who} /> : e.who],
                ["Target", e.target.name ? <Target key="target" t={e.target} /> : null],
                ["When", absolute(e.at, true)],
                ["Channel", e.channel ? <ChannelLink key="channel" id={e.channel.id} name={e.channel.name ?? "Unknown channel"} /> : null],
                ...(e.facts ?? [["Detail", e.detail] as [string, string | undefined]]),
              ]} />
              {e.reference && (
                <Button variant="outline" size="sm" className="mt-3" onClick={() => { void navigator.clipboard?.writeText(e.reference!); toast("Reference copied") }}>Copy reference</Button>
              )}
            </PanelSection>
          </div>
        )
      }}
    </Drawer>
    </>
  )
}
