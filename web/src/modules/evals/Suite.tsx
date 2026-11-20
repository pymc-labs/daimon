import { useMutation, useQueryClient } from "@tanstack/react-query"
import { Download, Flag, Play, Plus, Share2, Trash2 } from "lucide-react"
import { useState } from "react"
import { useNavigate, useParams } from "react-router"

import { Drawer } from "@/components/Drawer"
import { Facet, FilterBar, SearchInput } from "@/components/filters"
import { PanelHead, PanelSection, Tag } from "@/components/panel"
import { QueryState } from "@/components/QueryState"
import { Page } from "@/components/shell/Page"
import { Button } from "@/components/ui/button"
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { api } from "@/lib/api"
import { rowProps, useListParam, useParam } from "@/lib/filters"
import { plural } from "@/lib/format"
import { E, useCases, useSuite, useSuites } from "./api"
import { canEdit, canShare, ScrubPill } from "./bits"
import { ExportBundle, ImportBundle } from "./Bundles"
import { PLATFORMS } from "./previews"
import type { Platform, Share, ShareRole } from "./types"

const ROLES: ShareRole[] = ["viewer", "reviewer", "editor"]
const FIDELITY = { full: "Full", tools: "Tools", text: "Text", authored: "Authored" } as const
const RESULT = { pass: "text-success", fail: "text-destructive", error: "text-warning" } as const
const cap = (s: string) => s[0]!.toUpperCase() + s.slice(1)

function SharePanel({ suite, scrub, flags }: { suite: string; scrub: "scrubbed" | "partial" | "unscrubbed"; flags: number }) {
  const qc = useQueryClient()
  const data = useSuite(suite).data
  const [rows, setRows] = useState<Share[] | null>(null)
  const [who, setWho] = useState("")
  const [kind, setKind] = useState<Share["kind"]>("person")
  const list = rows ?? data?.shares ?? []
  const locked = scrub === "unscrubbed"
  const save = useMutation({
    mutationFn: () => api.put(`${E}/suites/${suite}/shares`, list),
    onSuccess: () => { setRows(null); void qc.invalidateQueries({ queryKey: ["evals", "suite", suite] }) },
  })
  const set = (r: Share[]) => setRows(r)
  return (
    <div className="flex flex-1 flex-col">
      <PanelHead icon={<Share2 className="size-5" />} title="Share" subtitle={data?.name} tags={<ScrubPill status={scrub} />} />
      {(locked || flags > 0) && (
        <PanelSection>
          <p className="text-sm text-warning">{locked ? "Unscrubbed. Only owners and admins can see it; ask an admin." : `${plural(flags, "case")} with open checks`}</p>
        </PanelSection>
      )}
      <PanelSection title="People and groups">
        <ul className="space-y-1.5">
          {list.map((s, i) => (
            <li key={`${s.kind}:${s.id}`} className="flex items-center gap-2">
              <span className="min-w-0 flex-1 truncate text-sm">{s.id}</span><Tag>{s.kind === "group" ? "Group" : "Person"}</Tag>
              <Select value={s.role} onValueChange={(r) => set(list.map((x, j) => (j === i ? { ...x, role: r as ShareRole } : x)))}>
                <SelectTrigger aria-label="Role" className="h-8 w-28"><SelectValue /></SelectTrigger>
                <SelectContent>{ROLES.map((r) => <SelectItem key={r} value={r}>{cap(r)}</SelectItem>)}</SelectContent>
              </Select>
              <Button variant="ghost" size="icon-sm" aria-label="Remove" onClick={() => set(list.filter((_, j) => j !== i))}><Trash2 /></Button>
            </li>
          ))}
        </ul>
        {!locked && (
          <form className="mt-3 flex items-center gap-2" onSubmit={(e) => { e.preventDefault(); if (who.trim()) { set([...list, { kind, id: who.trim(), role: "viewer" }]); setWho("") } }}>
            <Input value={who} onChange={(e) => setWho(e.target.value)} placeholder="Person or group" aria-label="Person or group" className="h-8" />
            <Select value={kind} onValueChange={(k) => setKind(k as Share["kind"])}>
              <SelectTrigger aria-label="Kind" className="h-8 w-28"><SelectValue /></SelectTrigger>
              <SelectContent><SelectItem value="person">Person</SelectItem><SelectItem value="group">Group</SelectItem></SelectContent>
            </Select>
            <Button type="submit" variant="outline" size="icon-sm" aria-label="Add"><Plus /></Button>
          </form>
        )}
        {save.error && <p role="alert" className="mt-2 text-sm text-destructive">{save.error.message}</p>}
      </PanelSection>
      <div className="mt-auto flex justify-end border-t px-5 py-3"><Button size="sm" disabled={rows === null || save.isPending || locked} onClick={() => save.mutate()}>Save</Button></div>
    </div>
  )
}

export function Suite() {
  const { suite = "" } = useParams()
  const go = useNavigate()
  const [version, setVersion] = useParam("version")
  const [, setShare] = useParam("share")
  const [exporting, setExporting] = useState(false)
  const [importing, setImporting] = useState(false)
  const [q, setQ] = useParam("q")
  const [platforms, setPlatforms] = useListParam("platform")
  const [fidelity, setFidelity] = useListParam("fidelity")
  const [flagged, setFlagged] = useListParam("flags")
  const [tags, setTags] = useListParam("tag")
  const info = useSuite(suite, version ? Number(version) : undefined)
  const summary = useSuites().data?.find((s) => s.id === suite)
  const cases = useCases(suite, version ? Number(version) : undefined)
  const current = info.data?.version ?? summary?.version ?? 1
  const role = summary?.role ?? "viewer"
  const shown = Number(version) || current
  const all = cases.data ?? []
  const rows = all.filter((c) =>
    (!q || c.title.toLowerCase().includes(q.toLowerCase()))
    && (!platforms.length || (c.platform && platforms.includes(c.platform)))
    && (!fidelity.length || fidelity.includes(c.fidelity))
    && (!flagged.length || (flagged.includes("flagged") ? !!c.flags?.length : !c.flags?.length))
    && (!tags.length || tags.some((t) => c.tags?.includes(t))))
  const tagOptions = [...new Set(all.flatMap((c) => c.tags ?? []))].map((t) => ({ value: t, label: t }))
  const flaggedCount = all.filter((c) => c.flags?.length).length
  return (
    <Page
      crumbs={[{ label: "Evals", to: "/evals" }, { label: info.data?.name ?? summary?.name ?? suite }]}
      actions={
        <>
          <Select value={String(shown)} onValueChange={(v) => setVersion(Number(v) === current ? null : v)}>
            <SelectTrigger aria-label="Version" className="h-8 w-24"><SelectValue /></SelectTrigger>
            <SelectContent>{Array.from({ length: current }, (_, i) => current - i).map((v) => <SelectItem key={v} value={String(v)}>v{v}</SelectItem>)}</SelectContent>
          </Select>
          {canShare(role) && <Button variant="outline" size="sm" onClick={() => setShare("open")}><Share2 />Share</Button>}
          {canEdit(role) && <Button variant="outline" size="sm" onClick={() => setExporting(true)}><Download />Export</Button>}
          {canEdit(role) && (
            <DropdownMenu>
              <DropdownMenuTrigger asChild><Button variant="outline" size="sm"><Plus />Add cases</Button></DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                <DropdownMenuItem onSelect={() => go(`/evals/suites/${suite}/cases/new`)}>By hand</DropdownMenuItem>
                <DropdownMenuItem onSelect={() => setImporting(true)}>From a bundle</DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          )}
          {canEdit(role) && <Button size="sm" onClick={() => go(`/evals/suites/${suite}/run${version ? `?version=${version}` : ""}`)}><Play />New run</Button>}
        </>
      }
      flush
    >
      <QueryState query={cases} empty="No cases">
        {() => (
          <>
            <FilterBar>
              <SearchInput value={q} onChange={(v) => setQ(v || null)} className="w-56" />
              <Facet label="Platform" value={platforms} onChange={setPlatforms} options={(Object.keys(PLATFORMS) as Platform[]).map((value) => ({ value, label: PLATFORMS[value] }))} />
              <Facet label="Fidelity" value={fidelity} onChange={setFidelity} options={Object.entries(FIDELITY).map(([value, label]) => ({ value, label }))} />
              <Facet label="Checks" value={flagged} onChange={setFlagged} options={[{ value: "flagged", label: "Open flags" }, { value: "clear", label: "No flags" }]} />
              {tagOptions.length > 0 && <Facet label="Tag" value={tags} onChange={setTags} options={tagOptions} />}
              <span className="ml-auto text-xs text-muted-foreground tabular">{plural(rows.length, "case")}</span>
            </FilterBar>
            <div className="mx-auto max-w-[1040px] px-4 lg:px-6">
              {!rows.length ? <div className="py-16 text-center text-sm text-muted-foreground">No cases match</div> : (
                <Table>
                  <TableHeader>
                    <TableRow className="hover:bg-transparent">
                      <TableHead>Case</TableHead><TableHead className="max-sm:hidden">Platform</TableHead><TableHead className="max-md:hidden">Fidelity</TableHead>
                      <TableHead>Flags</TableHead><TableHead className="max-lg:hidden">Tags</TableHead><TableHead>Last result</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {rows.map((c) => (
                      <TableRow key={c.id} {...rowProps(() => go(`/evals/suites/${suite}/cases/${c.id}`))}>
                        <TableCell className="max-w-72 truncate font-medium">{c.title}</TableCell>
                        <TableCell className="text-muted-foreground max-sm:hidden">{c.platform ? PLATFORMS[c.platform] : ""}</TableCell>
                        <TableCell className="text-muted-foreground max-md:hidden">{FIDELITY[c.fidelity as keyof typeof FIDELITY] ?? c.fidelity}</TableCell>
                        <TableCell>{c.flags?.length ? <span className="inline-flex items-center gap-1 text-warning"><Flag className="size-3.5" />{c.flags.length}</span> : null}</TableCell>
                        <TableCell className="max-lg:hidden"><span className="flex gap-1">{c.tags?.map((t) => <Tag key={t}>{t}</Tag>)}</span></TableCell>
                        <TableCell>{c.last_result ? <span className={RESULT[c.last_result]}>{cap(c.last_result)}</span> : <span className="text-faint">None</span>}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              )}
            </div>
          </>
        )}
      </QueryState>
      <Drawer param="share" title="Share">{() => <SharePanel suite={suite} scrub={summary?.scrub_status ?? "unscrubbed"} flags={flaggedCount} />}</Drawer>
      <ExportBundle open={exporting} onOpenChange={setExporting} suite={suite} version={shown} />
      <ImportBundle open={importing} onOpenChange={setImporting} />
    </Page>
  )
}
