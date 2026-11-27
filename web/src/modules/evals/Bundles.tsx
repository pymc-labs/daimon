import { useMutation, useQueryClient } from "@tanstack/react-query"
import { Download, Upload } from "lucide-react"
import { useRef, useState } from "react"

import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Switch } from "@/components/ui/switch"
import { ApiError } from "@/lib/api"
import { plural } from "@/lib/format"
import { E, useIsAdmin } from "./api"
import type { ImportReport } from "./types"

/** The server refuses bundles above its `max_bundle_mb`; checking here saves the upload. */
const MAX_MB = 128

/** The server's refusal text from a failed response. */
async function refusal(res: Response, fallback: string) {
  const j = await res.json().catch(() => null)
  return new ApiError(res.status, typeof j?.detail === "string" ? j.detail : j?.detail?.message ?? fallback)
}

const Row = ({ label, children }: { label: string; children: React.ReactNode }) => <label className="grid gap-1.5 text-sm"><span className="text-muted-foreground">{label}</span>{children}</label>

/** Upload a bundle: the zip travels as the raw request body. */
export function ImportBundle({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const qc = useQueryClient()
  const file = useRef<HTMLInputElement>(null)
  const [name, setName] = useState("")
  const [passphrase, setPassphrase] = useState("")
  const [unscrubbed, setUnscrubbed] = useState(false)
  const admin = useIsAdmin()
  const send = useMutation({
    mutationFn: async () => {
      const f = file.current?.files?.[0]
      if (!f) throw new ApiError(400, "Choose a bundle")
      if (f.size > MAX_MB * 1024 * 1024) throw new ApiError(413, `Bundles are limited to ${MAX_MB} MB`)
      const res = await fetch(`${E}/bundles/import${unscrubbed ? "?accept_unscrubbed=true" : ""}`, {
        method: "POST", credentials: "include", body: f,
        headers: { "X-Daimon-Web": "1", ...(passphrase ? { "X-Bundle-Passphrase": passphrase } : {}) },
      })
      if (!res.ok) throw await refusal(res, "Import failed")
      return (await res.json()) as ImportReport
    },
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["evals"] }),
  })
  const done = send.data
  return (
    <Dialog open={open} onOpenChange={(o) => { onOpenChange(o); if (!o) send.reset() }}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader><DialogTitle>{done ? "Imported" : "Import bundle"}</DialogTitle></DialogHeader>
        {done ? (
          <div className="space-y-2 text-sm">
            <p>{plural(done.suites.length, "suite")}, {plural(done.runs.length, "run")}, {plural(done.blobs, "file")}</p>
            {done.warnings.map((w) => <p key={w} className="text-warning">{w}</p>)}
            <DialogFooter><Button onClick={() => onOpenChange(false)}>Done</Button></DialogFooter>
          </div>
        ) : (
          <div className="space-y-3">
            <Row label="Bundle"><Input ref={file} type="file" accept=".zip,.dmeval,application/zip" onChange={(e) => setName(e.target.files?.[0]?.name ?? "")} aria-label="Bundle" /></Row>
            <Row label="Passphrase"><Input type="password" value={passphrase} onChange={(e) => setPassphrase(e.target.value)} className="h-8" /></Row>
            {admin && <div className="flex items-center justify-between text-sm"><span>Accept unscrubbed content</span><Switch checked={unscrubbed} onCheckedChange={setUnscrubbed} aria-label="Accept unscrubbed content" /></div>}
            {send.error && <p role="alert" className="text-sm text-destructive">{send.error.message}</p>}
            <DialogFooter>
              <Button variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
              <Button disabled={!name || send.isPending} onClick={() => send.mutate()}><Upload />Import</Button>
            </DialogFooter>
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}

/** Write a bundle of a suite version; the server streams the zip, which is saved as it arrives. The passphrase travels in a header. */
export function ExportBundle({ open, onOpenChange, suite, version, runs = [] }: { open: boolean; onOpenChange: (o: boolean) => void; suite: string; version: number; runs?: string[] }) {
  const [title, setTitle] = useState("")
  const [passphrase, setPassphrase] = useState("")
  const [identities, setIdentities] = useState(false)
  const admin = useIsAdmin()
  const make = useMutation({
    mutationFn: async () => {
      const res = await fetch(`${E}/bundles/export`, {
        method: "POST", credentials: "include",
        headers: { "X-Daimon-Web": "1", "Content-Type": "application/json", ...(passphrase ? { "X-Bundle-Passphrase": passphrase } : {}) },
        body: JSON.stringify({ suites: [{ id: suite, version }], runs, title: title || undefined, include_identities: admin && identities }),
      })
      if (!res.ok) throw await refusal(res, "Export failed")
      const name = /filename="([^"]+)"/.exec(res.headers.get("Content-Disposition") ?? "")?.[1] ?? `${suite}.zip`
      const url = URL.createObjectURL(await res.blob())
      const a = Object.assign(document.createElement("a"), { href: url, download: name })
      a.click()
      URL.revokeObjectURL(url)
    },
    onSuccess: () => onOpenChange(false),
  })
  return (
    <Dialog open={open} onOpenChange={(o) => { onOpenChange(o); if (!o) make.reset() }}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader><DialogTitle>Export</DialogTitle></DialogHeader>
        <div className="space-y-3">
          <Row label="Title"><Input value={title} onChange={(e) => setTitle(e.target.value)} className="h-8" /></Row>
          <Row label="Passphrase"><Input type="password" value={passphrase} onChange={(e) => setPassphrase(e.target.value)} className="h-8" /></Row>
          {admin && <div className="flex items-center justify-between text-sm"><span>Include identities</span><Switch checked={identities} onCheckedChange={setIdentities} aria-label="Include identities" /></div>}
          {make.error && <p role="alert" className="text-sm text-destructive">{make.error.message}</p>}
          <DialogFooter>
            <Button variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
            <Button disabled={make.isPending} onClick={() => make.mutate()}><Download />Export</Button>
          </DialogFooter>
        </div>
      </DialogContent>
    </Dialog>
  )
}
