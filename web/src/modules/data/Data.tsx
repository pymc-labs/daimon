import { useQuery } from "@tanstack/react-query"
import { CircleAlert, CircleCheck, Download, ExternalLink, Zap } from "lucide-react"
import { useState } from "react"
import { Link, Navigate, Route, Routes, useMatch, useParams } from "react-router"
import { Ago, Prose, StatusDot } from "@/components/common"
import { Drawer } from "@/components/Drawer"
import { Empty, Facet, FilterBar, SearchInput } from "@/components/filters"
import { Facts, PanelHead, PanelSection } from "@/components/panel"
import { LoadMore, QueryState } from "@/components/QueryState"
import { Page } from "@/components/shell/Page"
import { Button } from "@/components/ui/button"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { api } from "@/lib/api"
import { rowProps, useListParam, useParam } from "@/lib/filters"
import { compact, plural, shortDate } from "@/lib/format"
import { usePaged } from "@/lib/hooks"
import type { DataItem, DataRecord, DataSource, Search } from "./types"

const TABS = [{ label: "Search", to: "/data", end: true }, { label: "Sources", to: "/data/sources" }]
const KEY = ["data", "sources"]

const useSources = () => useQuery({ queryKey: KEY, queryFn: () => api.get<DataSource[]>("/api/data/sources") })

export function Data() {
  const open = useMatch("/data/sources/:id")
  const sources = useSources()
  const name = sources.data?.find((s) => s.id === open?.params.id)?.name
  const crumbs = open
    ? [{ label: "Data", to: "/data" }, { label: "Sources", to: "/data/sources" }, { label: name ?? "Source" }]
    : [{ label: "Data", to: "/data" }]
  return (
    <Page crumbs={crumbs} tabs={TABS}>
      <Routes>
        <Route index element={<Searching sources={sources.data ?? []} />} />
        <Route path="sources" element={<Sources />} />
        <Route path="sources/:id" element={<Browse sources={sources.data ?? []} />} />
        <Route path="*" element={<Navigate to="/data" replace />} />
      </Routes>
      <Drawer param="item" title="Item" wide>{(id) => <ItemPanel id={id} sources={sources.data ?? []} />}</Drawer>
    </Page>
  )
}

/** Fresh or stale, by icon and label. */
function Freshness({ source }: { source: DataSource | undefined }) {
  if (!source) return null
  return source.fresh
    ? <span className="inline-flex items-center gap-1.5"><CircleCheck className="size-3.5 text-success" />Fresh</span>
    : <span className="inline-flex items-center gap-1.5 text-muted-foreground"><CircleAlert className="size-3.5" />Stale</span>
}

function Searching({ sources }: { sources: DataSource[] }) {
  const [q, setQ] = useParam("q")
  const [picked, setPicked] = useListParam("source")
  const [, setItem] = useParam("item")
  const text = q.trim()
  const found = useQuery({
    queryKey: ["data", "search", text, picked],
    queryFn: () => api.get<Search>(`/api/data/search?q=${encodeURIComponent(text)}${picked.map((s) => `&source=${s}`).join("")}`),
    enabled: !!text,
  })
  const byId = new Map(sources.map((s) => [s.id, s]))
  return (
    <>
      <FilterBar className="px-0 pt-0">
        <SearchInput value={q} onChange={(v) => setQ(v || null)} placeholder="Search your data" className="w-72 max-sm:w-full" />
        <Facet label="Source" options={sources.map((s) => ({ value: s.id, label: s.name }))} value={picked} onChange={setPicked} />
      </FilterBar>
      {!text ? <Empty>Search the sources you can read</Empty> : (
        <QueryState query={found} empty="Nothing matches" isEmpty={(d) => !d.hits.length && !d.live.length}>
          {(d) => (
            <>
              <Table>
                <TableHeader>
                  <TableRow className="hover:bg-transparent">
                    <TableHead>Title</TableHead><TableHead>Source</TableHead>
                    <TableHead className="max-sm:hidden">Freshness</TableHead><TableHead className="text-right">Date</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {d.hits.map((h) => (
                    <TableRow key={h.record_id} {...rowProps(() => setItem(h.record_id))}>
                      <TableCell className="max-w-0 min-w-48 align-top">
                        <div className="truncate font-medium">{h.title}</div>
                        <div className="truncate text-xs text-muted-foreground">{h.snippet}</div>
                      </TableCell>
                      <TableCell className="align-top">{byId.get(h.source_id)?.name}</TableCell>
                      <TableCell className="align-top max-sm:hidden"><Freshness source={byId.get(h.source_id)} /></TableCell>
                      <TableCell className="text-right align-top text-muted-foreground">{shortDate(h.occurred_at)}</TableCell>
                    </TableRow>
                  ))}
                  {d.live.map((l) => l.skipped ? (
                    <TableRow key={l.source_id} className="hover:bg-transparent">
                      <TableCell className="text-muted-foreground"><span className="inline-flex items-center gap-1.5"><CircleAlert className="size-3.5" />{l.skipped === "timeout" ? "Timed out" : "Couldn't search"}</span></TableCell>
                      <TableCell>{l.source_name}</TableCell><TableCell className="max-sm:hidden" /><TableCell />
                    </TableRow>
                  ) : l.hits.map((h, i) => (
                    <TableRow key={`${l.source_id}|${i}`} className="hover:bg-transparent">
                      <TableCell className="max-w-0 min-w-48 align-top">
                        <div className="flex items-center gap-1.5">
                          <span className="truncate font-medium">{h.title}</span>
                          {h.url && <a href={h.url} target="_blank" rel="noreferrer" aria-label={`Open ${h.title}`} className="shrink-0 text-muted-foreground hover:text-foreground"><ExternalLink className="size-3.5" /></a>}
                        </div>
                        <div className="truncate text-xs text-muted-foreground">{h.text}</div>
                      </TableCell>
                      <TableCell className="align-top">{l.source_name}</TableCell>
                      <TableCell className="align-top text-muted-foreground max-sm:hidden"><span className="inline-flex items-center gap-1.5"><Zap className="size-3.5" />Live</span></TableCell>
                      <TableCell />
                    </TableRow>
                  )))}
                </TableBody>
              </Table>
            </>
          )}
        </QueryState>
      )}
    </>
  )
}

function Sources() {
  const q = useSources()
  return (
    <QueryState query={q} empty="No source is connected">
      {(rows) => (
        <Table>
          <TableHeader>
            <TableRow className="hover:bg-transparent">
              <TableHead>Source</TableHead><TableHead className="max-sm:hidden">Kind</TableHead><TableHead className="text-right">Items</TableHead>
              <TableHead className="max-md:hidden">Freshness</TableHead><TableHead className="max-md:hidden">Synced</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((s) => (
              <TableRow key={s.id}>
                <TableCell className="font-medium"><Link to={`/data/sources/${s.id}`} className="hover:underline">{s.name}</Link></TableCell>
                <TableCell className="text-muted-foreground max-sm:hidden">{s.kind}</TableCell>
                <TableCell className="text-right tabular">{compact(s.records)}</TableCell>
                <TableCell className="max-md:hidden"><Freshness source={s} /></TableCell>
                <TableCell className="text-muted-foreground max-md:hidden"><Ago iso={s.last_sync_at} /></TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </QueryState>
  )
}

function Browse({ sources }: { sources: DataSource[] }) {
  const { id = "" } = useParams()
  const [, setItem] = useParam("item")
  const list = usePaged<DataItem>(["data", "items", id], `/api/data/sources/${id}/items?limit=25`)
  const source = sources.find((s) => s.id === id)
  return (
    <>
      <div className="flex items-center gap-4 pb-3 text-sm">
        <StatusDot status={source?.fresh ? "healthy" : "setup"} label={source?.fresh ? "Fresh" : "Stale"} />
        {source && <span className="ml-auto text-xs text-muted-foreground tabular">{plural(source.records, "item")}</span>}
      </div>
      <QueryState query={list} empty="Nothing here that you can read">
        {(rows) => (
          <>
            <Table>
              <TableHeader>
                <TableRow className="hover:bg-transparent">
                  <TableHead>Title</TableHead><TableHead className="max-sm:hidden">Kind</TableHead>
                  <TableHead className="max-md:hidden">Author</TableHead><TableHead className="text-right">Date</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map((r) => (
                  <TableRow key={r.id} {...rowProps(() => setItem(r.id))}>
                    <TableCell className="max-w-0 min-w-48 truncate font-medium">{r.title}</TableCell>
                    <TableCell className="text-muted-foreground max-sm:hidden">{r.kind}</TableCell>
                    <TableCell className="text-muted-foreground max-md:hidden">{r.author}</TableCell>
                    <TableCell className="text-right text-muted-foreground">{shortDate(r.occurred_at)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            <LoadMore list={list} />
          </>
        )}
      </QueryState>
    </>
  )
}

const size = (n: number) => (n < 1024 ? `${n} B` : n < 1_048_576 ? `${Math.round(n / 1024)} KB` : `${(n / 1_048_576).toFixed(1)} MB`)

function ItemPanel({ id, sources }: { id: string; sources: DataSource[] }) {
  const first = useQuery({ queryKey: ["data", "record", id], queryFn: () => api.get<DataRecord>(`/api/data/records/${id}`) })
  const [extra, setExtra] = useState<DataRecord[]>([])
  const [loading, setLoading] = useState(false)
  return (
    <QueryState query={first}>
      {(r) => {
        const pages = [r, ...extra]
        const last = pages[pages.length - 1]!
        const source = sources.find((s) => s.id === r.item.source_id)
        return (
          <div>
            <PanelHead title={r.item.title} subtitle={source?.name} actions={r.item.url ? <Button asChild variant="outline" size="sm"><a href={r.item.url} target="_blank" rel="noreferrer"><ExternalLink />Open</a></Button> : null} />
            <PanelSection>
              <Facts rows={[["Source", source?.name], ["Kind", r.item.kind], ["Author", r.item.author], ["Date", shortDate(r.item.occurred_at)], ["Freshness", source ? <Freshness key="f" source={source} /> : null]]} />
            </PanelSection>
            <PanelSection title="Text">
              <Prose text={pages.map((p) => p.text).join("")} breaks />
              {last.next_start != null && (
                <Button variant="outline" size="sm" className="mt-3" disabled={loading} onClick={() => {
                  setLoading(true)
                  void api.get<DataRecord>(`/api/data/records/${id}?start=${last.next_start}`).then((p) => setExtra([...extra, p])).finally(() => setLoading(false))
                }}>Show more</Button>
              )}
            </PanelSection>
            {r.files.length > 0 && (
              <PanelSection title="Files">
                <ul className="divide-y rounded-lg border">
                  {r.files.map((f) => (
                    <li key={f.ord} className="flex min-h-11 items-center gap-3 px-3 py-1.5 text-sm">
                      <span className="min-w-0 flex-1 truncate">{f.name}</span>
                      <span className="text-xs text-muted-foreground tabular">{size(f.size)}</span>
                      <Button asChild variant="ghost" size="icon-sm"><a href={`/api/data/records/${id}/files/${f.ord}`} download aria-label={`Download ${f.name}`}><Download /></a></Button>
                    </li>
                  ))}
                </ul>
              </PanelSection>
            )}
          </div>
        )
      }}
    </QueryState>
  )
}
