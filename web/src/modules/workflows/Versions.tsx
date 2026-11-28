import { ArrowLeft, GitCompare, History, KeyRound } from "lucide-react"
import { useMemo, useState } from "react"
import { Link, useNavigate, useParams, useSearchParams } from "react-router"

import { Ago, Avatar } from "@/components/common"
import { Tag } from "@/components/panel"
import { QueryState } from "@/components/QueryState"
import { Button } from "@/components/ui/button"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { absolute } from "@/lib/format"
import { useCatalog, useRestore, useVersions, useVersionSpec } from "./api"
import { Canvas } from "./Canvas"
import { Inspector } from "./inspector/Inspector"
import { GRANT } from "./kinds"
import { ReviewSections } from "./Review"
import { reviewDiff } from "./review"
import { Shell } from "./run/RunDialogs"
import { validate } from "./spec"
import type { Version, Workflow } from "./types"

const H = "pl-4 lg:pl-6", T = "pr-4 text-right lg:pr-6"
const none = new Set<string>()

function Grants({ v }: { v: Version }) {
  if (!v.grants.length) return <span className="text-faint">–</span>
  return (
    <span className="inline-flex items-center gap-1.5 whitespace-nowrap" title={v.grants.map((g) => GRANT[g]).join(", ")}>
      <KeyRound className="size-3.5 text-warning" />{v.grants.length === 1 ? GRANT[v.grants[0]!] : `${v.grants.length} grants`}
    </span>
  )
}

export function VersionsTab({ wf }: { wf: Workflow }) {
  const query = useVersions(wf.id)
  const restore = useRestore(wf.id)
  const navigate = useNavigate()
  const [compare, setCompare] = useState<[number, number] | null>(null)
  const go = (n: number) => navigate(`/workflows/${wf.id}/versions/${n}`)
  return (
    <QueryState query={query} className="px-6 pt-6" isEmpty={(r) => !r.length} empty="Not published">{(vs) => (
      <>
        <div className="flex h-12 items-center justify-between gap-2 px-4 lg:px-6">
          <span className="text-sm text-muted-foreground tabular">{vs.length} {vs.length === 1 ? "version" : "versions"}</span>
          {vs.length > 1 && <Button variant="outline" onClick={() => setCompare([vs[1]!.number, vs[0]!.number])}><GitCompare />Compare</Button>}
        </div>
        <Table>
          <TableHeader>
            <TableRow className="hover:bg-transparent">
              <TableHead className={`w-28 ${H}`}>Version</TableHead><TableHead>Note</TableHead><TableHead className="max-md:hidden">Published by</TableHead>
              <TableHead>Published</TableHead><TableHead className="max-lg:hidden">Grants</TableHead><TableHead className="text-right">Runs</TableHead><TableHead className={T}><span className="sr-only">Actions</span></TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {vs.map((v) => (
              <TableRow key={v.number} className="h-12 cursor-pointer" onClick={() => go(v.number)}>
                <TableCell className={H}><span className="flex items-center gap-2 tabular">v{v.number}{v.live && <Tag>Live</Tag>}</span></TableCell>
                <TableCell className="max-w-80 truncate">{v.note || <span className="text-faint">–</span>}</TableCell>
                <TableCell className="max-md:hidden"><span className="flex items-center gap-2"><Avatar name={v.published_by.name} />{v.published_by.name}</span></TableCell>
                <TableCell title={absolute(v.published_at)}><Ago iso={v.published_at} /></TableCell>
                <TableCell className="max-lg:hidden"><Grants v={v} /></TableCell>
                <TableCell className="tabular text-right">{v.runs}</TableCell>
                <TableCell className={T} onClick={(e) => e.stopPropagation()}>
                  <span className="inline-flex gap-1">
                    <Button variant="ghost" onClick={() => go(v.number)}>View</Button>
                    {wf.can_edit && !v.live && <Button variant="ghost" disabled={restore.isPending} onClick={() => restore.mutate(v.number, { onSuccess: () => navigate(`/workflows/${wf.id}`) })}>Restore</Button>}
                  </span>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
        {compare && <CompareDialog wf={wf} versions={vs} initial={compare} onClose={() => setCompare(null)} />}
      </>
    )}</QueryState>
  )
}

function CompareDialog({ wf, versions, initial, onClose }: { wf: Workflow; versions: Version[]; initial: [number, number]; onClose: () => void }) {
  const [[a, b], setPair] = useState(initial)
  const { data: catalog } = useCatalog()
  const from = useVersionSpec(wf.id, a), to = useVersionSpec(wf.id, b)
  const r = useMemo(() => from.data && to.data ? reviewDiff(from.data.spec, to.data.spec, { catalog }) : null, [from.data, to.data, catalog])
  const pick = (label: string, v: number, set: (n: number) => void) => (
    <div className="flex min-w-0 flex-1 items-center gap-2 text-sm">
      <span className="text-muted-foreground">{label}</span>
      <Select value={String(v)} onValueChange={(x) => set(Number(x))}>
        <SelectTrigger aria-label={label} className="h-8 w-full"><SelectValue /></SelectTrigger>
        <SelectContent>{versions.map((x) => <SelectItem key={x.number} value={String(x.number)}>v{x.number}{x.live ? " (live)" : ""}</SelectItem>)}</SelectContent>
      </Select>
    </div>
  )
  return (
    <Shell title="Compare versions" open onOpenChange={(o) => !o && onClose()} wide footer={<Button onClick={onClose}>Close</Button>}>
      <div className="flex gap-3 max-sm:flex-col">{pick("From", a, (n) => setPair([n, b]))}{pick("To", b, (n) => setPair([a, n]))}</div>
      <div className="max-h-[56vh] space-y-5 overflow-y-auto">
        {r && a !== b ? <ReviewSections r={r} /> : a === b ? <p className="text-sm text-muted-foreground">Same version</p> : <p className="text-sm text-muted-foreground">Loading</p>}
      </div>
    </Shell>
  )
}

/** One published version on the canvas, read only. */
export function VersionView({ wf }: { wf: Workflow }) {
  const { n = "" } = useParams()
  const num = Number(n)
  const { data } = useVersionSpec(wf.id, num)
  const { data: catalog } = useCatalog()
  const restore = useRestore(wf.id)
  const navigate = useNavigate()
  const [params, setParams] = useSearchParams()
  const node = params.get("node")
  const spec = data?.spec
  const validation = useMemo(() => (spec ? validate(spec) : undefined), [spec])
  const select = (id: string | null) => setParams((p) => { if (id) p.set("node", id); else p.delete("node"); return p }, { replace: true })
  const nd = node && spec ? spec.nodes[node] : undefined
  const v = data?.version
  return (
    <div className="relative min-h-0 flex-1">
      <div className="absolute top-4 left-4 z-10 flex items-center gap-2">
        <Button variant="outline" className="bg-popover shadow-xs" asChild><Link to={`/workflows/${wf.id}/versions`}><ArrowLeft />Versions</Link></Button>
        {v && <span className="inline-flex h-8 items-center gap-2 rounded-md border bg-popover px-2.5 text-sm shadow-xs"><History className="size-4 text-muted-foreground" /><span className="tabular">v{v.number}</span>{v.live && <Tag>Live</Tag>}<span className="text-muted-foreground max-sm:hidden"><Ago iso={v.published_at} /></span></span>}
        {v && wf.can_edit && !v.live && <Button variant="outline" className="bg-popover shadow-xs" disabled={restore.isPending} onClick={() => restore.mutate(v.number, { onSuccess: () => navigate(`/workflows/${wf.id}`) })}>Restore</Button>}
      </div>
      {spec && (
        <Canvas spec={spec} validation={validation} catalog={catalog} changed={none} flash={none} selected={node} ro
          select={select} add={() => {}} remove={() => {}} duplicate={() => {}} panel={!!nd} />
      )}
      {nd && node && spec && <Inspector key={node} id={node} n={nd} spec={spec} catalog={catalog} ro set={() => {}} validation={validation} close={() => select(null)} />}
    </div>
  )
}
