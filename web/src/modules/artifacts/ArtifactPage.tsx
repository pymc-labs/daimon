import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { ArrowRightLeft, Download, ExternalLink, History, Link2, Share2, Trash2 } from "lucide-react"
import { useState } from "react"
import { useNavigate, useParams } from "react-router"
import { toast } from "sonner"

import { ErrorState } from "@/components/QueryState"
import { DrawerTabs, PanelMenu } from "@/components/panel"
import { Page } from "@/components/shell/Page"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import { api, ApiError } from "@/lib/api"
import { useConfirm } from "@/lib/confirm"
import { useParam } from "@/lib/filters"
import { NotFound } from "@/pages/Status"
import { Activity } from "./Activity"
import { expiry, fullUrl, KEY, useArtifact } from "./bits"
import { Data } from "./Data"
import { Details } from "./Details"
import { Preview } from "./Preview"
import { ShareDialog } from "./Share"
import type { ArtifactDetail, ArtifactVersion } from "./types"
import { Versions } from "./Versions"

function Transfer({ a, open, onOpenChange }: { a: ArtifactDetail; open: boolean; onOpenChange: (o: boolean) => void }) {
  const qc = useQueryClient()
  const [to, setTo] = useState("")
  const go = useMutation({
    mutationFn: () => api.post(`/api/artifacts/${a.id}/transfer`, { person: to }),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: KEY }); onOpenChange(false); toast("Ownership transferred") },
  })
  return (
    <Dialog open={open} onOpenChange={(o) => { onOpenChange(o); if (!o) setTo("") }}>
      <DialogContent className="gap-4 p-5 sm:max-w-sm">
        <DialogHeader>
          <DialogTitle className="text-base">Transfer {a.title}</DialogTitle>
          <DialogDescription>{a.editors.length ? "You stay an editor." : "Add an editor in Share first."}</DialogDescription>
        </DialogHeader>
        <Select value={to} onValueChange={setTo} disabled={!a.editors.length}>
          <SelectTrigger className="w-full" aria-label="New owner"><SelectValue placeholder="New owner" /></SelectTrigger>
          <SelectContent>{a.editors.map((p) => <SelectItem key={p.id} value={p.id}>{p.name}</SelectItem>)}</SelectContent>
        </Select>
        <DialogFooter className="-mx-5 -mb-5 flex-row sm:justify-end">
          <Button variant="outline" className="max-sm:flex-1" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button className="max-sm:flex-1" disabled={!to || go.isPending} onClick={() => go.mutate()}>Transfer</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function Banner({ a, version, back }: { a: ArtifactDetail; version: number; back: () => void }) {
  const qc = useQueryClient()
  const restore = useMutation({
    mutationFn: () => api.post<ArtifactDetail>(`/api/artifacts/${a.id}/versions/${version}/restore`),
    onSuccess: (r) => { void qc.invalidateQueries({ queryKey: KEY }); back(); toast(`Restored as version ${r.version}`) },
  })
  return (
    <div className="flex h-11 shrink-0 items-center gap-2 border-b bg-warning/10 px-3 text-sm">
      <History className="size-4 shrink-0 text-warning" />
      <span className="min-w-0 flex-1 truncate font-medium">Viewing version {version}</span>
      <Button variant="outline" size="sm" onClick={back}>Back to current</Button>
      {a.can.edit && <Button size="sm" disabled={restore.isPending} onClick={() => restore.mutate()}>Restore</Button>}
    </div>
  )
}

export function ArtifactPage() {
  const { id = "" } = useParams()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const confirm = useConfirm()
  const query = useArtifact(id)
  const a = query.data
  const [v, setV] = useParam("v")
  const [sharing, setSharing] = useState(false)
  const [transferring, setTransferring] = useState(false)
  const versions = useQuery({ queryKey: [...KEY, "versions", id], queryFn: () => api.get<ArtifactVersion[]>(`/api/artifacts/${id}/versions`), enabled: !!a?.can.edit })
  const del = useMutation({
    mutationFn: () => api.del(`/api/artifacts/${id}`),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: KEY }); navigate("/artifacts"); toast("Artifact deleted") },
  })
  if (query.error instanceof ApiError && query.error.status === 404) return <NotFound />
  const crumbs = [{ label: "Artifacts", to: "/artifacts" }, { label: a?.title ?? "" }]
  if (!a) {
    return (
      <Page crumbs={crumbs}>
        {query.isError ? <ErrorState error={query.error} onRetry={() => void query.refetch()} /> : <Skeleton className="h-96" />}
      </Page>
    )
  }
  const viewing = v && Number(v) !== a.version ? Number(v) : null
  const setVersion = (n: number | null) => setV(n === null || n === a.version ? null : String(n))
  const left = expiry(a.expires_at)
  const link = a.link ?? fullUrl(a.url)
  const menu = [
    { label: "Open in new tab", icon: <ExternalLink />, onSelect: () => window.open(a.url, "_blank") },
    { label: "Download HTML", icon: <Download />, onSelect: () => window.open(`${a.url}?download=1`, "_blank") },
    ...(a.can.transfer ? [{ label: "Transfer", icon: <ArrowRightLeft />, onSelect: () => setTransferring(true) }] : []),
    ...(a.can.delete ? [{
      label: "Delete", icon: <Trash2 />, destructive: true, disabled: del.isPending,
      onSelect: async () => { if (await confirm({ title: "Delete artifact?", body: a.title, action: "Delete", destructive: true })) del.mutate() },
    }] : []),
  ]
  return (
    <Page
      crumbs={crumbs} flush
      actions={
        <>
          {left && <span className="mr-1 inline-flex h-5 items-center rounded border px-1.5 text-[11px] text-muted-foreground max-sm:hidden">{left === "Expired" ? left : `Expires in ${left.replace(" left", "")}`}</span>}
          <Button variant="outline" size="sm" onClick={() => { void navigator.clipboard?.writeText(link); toast("Link copied") }}><Link2 /><span className="max-sm:sr-only">Copy link</span></Button>
          {a.can.share && <Button size="sm" onClick={() => setSharing(true)}><Share2 /><span className="max-sm:sr-only">Share</span></Button>}
          <PanelMenu items={menu} />
        </>
      }
    >
      <div className="flex h-full max-lg:flex-col max-lg:overflow-y-auto">
        <Preview
          artifact={a} version={viewing} versions={versions.data} onVersion={setVersion} className="max-lg:h-[75dvh] max-lg:flex-none"
          banner={viewing && <Banner a={a} version={viewing} back={() => setVersion(null)} />}
        />
        <aside className="w-[360px] shrink-0 overflow-y-auto border-l max-lg:w-full max-lg:overflow-visible max-lg:border-t max-lg:border-l-0">
          <DrawerTabs tabs={[
            { value: "details", label: "Details", children: <Details a={a} /> },
            // Data, activity and versions are for those who manage the page.
            ...(a.can.edit ? [
              { value: "data", label: "Data", children: <Data a={a} /> },
              { value: "activity", label: "Activity", children: <Activity a={a} /> },
              { value: "versions", label: "Versions", children: <Versions versions={versions.data ?? []} viewing={viewing} onPick={setVersion} /> },
            ] : []),
          ]} />
        </aside>
      </div>
      {a.can.share && <ShareDialog artifact={a} open={sharing} onOpenChange={setSharing} />}
      {a.can.transfer && <Transfer a={a} open={transferring} onOpenChange={setTransferring} />}
    </Page>
  )
}
