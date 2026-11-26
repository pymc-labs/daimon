import { ExternalLink } from "lucide-react"
import { useEffect } from "react"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { api } from "@/lib/api"
import { timeOnly } from "@/lib/format"
import type { Started } from "@/modules/data/types"

const BASE = "/api/profile/data"

/** A device sign-in: the code to enter at the provider, polled until they approve it. */
export function DeviceCode({ s, onClose, onDone }: { s: Started; onClose: () => void; onDone: () => void }) {
  useEffect(() => {
    let live = true
    const tick = async () => {
      try {
        const r = await api.post<{ connected: boolean }>(`${BASE}/connect/${s.pending_id}/poll`)
        if (live && r.connected) return onDone()
      } catch { /* a failed poll is retried */ }
      if (live) timer = window.setTimeout(tick, Math.max(s.interval, 2) * 1000)
    }
    let timer = window.setTimeout(tick, Math.max(s.interval, 2) * 1000)
    return () => { live = false; window.clearTimeout(timer) }
  }, [s, onDone])
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="sm:max-w-sm">
        <DialogHeader><DialogTitle>Enter this code</DialogTitle></DialogHeader>
        <p className="rounded-lg border py-4 text-center font-mono text-2xl tracking-widest">{s.user_code}</p>
        <dl className="divide-y rounded-lg border text-sm">
          <div className="flex justify-between gap-3 px-4 py-2.5"><dt className="text-muted-foreground">Source</dt><dd className="truncate">{s.source}</dd></div>
          {s.expires_at && <div className="flex justify-between gap-3 px-4 py-2.5"><dt className="text-muted-foreground">Code ends</dt><dd>{timeOnly(s.expires_at)}</dd></div>}
        </dl>
        <DialogFooter className="max-sm:flex-row">
          <Button variant="outline" className="max-sm:flex-1" onClick={onClose}>Cancel</Button>
          <Button className="max-sm:flex-1" asChild><a href={s.url} target="_blank" rel="noreferrer"><ExternalLink />Open</a></Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
