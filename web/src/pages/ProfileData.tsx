import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { CircleAlert, CircleCheck, ExternalLink } from "lucide-react"
import { useEffect, useState } from "react"
import { toast } from "sonner"
import { Ago } from "@/components/common"
import { QueryState } from "@/components/QueryState"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { api } from "@/lib/api"
import { useConfirm } from "@/lib/confirm"
import type { Connectable, Connected, Connection, Started } from "@/modules/data/types"
import { Empty, Row, Section } from "./profile-parts"

const BASE = "/api/profile/data"
const KEY = ["profile", "data"]

/** The data sources a person's accounts are linked to, and the ones they connected themselves. */
export function ProfileData() {
  const q = useQuery({ queryKey: KEY, queryFn: () => api.get<Connected>(BASE) })
  const qc = useQueryClient()
  const confirm = useConfirm()
  const [starting, setStarting] = useState<Started | null>(null)
  const start = useMutation({
    mutationFn: (c: Connectable) => api.post<Started>(`${BASE}/connect`, { source_id: c.source_id }),
    onSuccess: (r) => { if (r.flow === "code") window.location.assign(r.url); else setStarting(r) },
  })
  const disconnect = useMutation({
    mutationFn: (c: Connection) => api.del(`${BASE}/connections/${c.source_id}`),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: KEY }); toast("Disconnected") },
  })
  return (
    <Section title="Data accounts">
      <QueryState query={q}>
        {(d) => {
          const have = new Set(d.connections.map((c) => c.source_id))
          const open = d.connectable.filter((c) => !have.has(c.source_id))
          return (
            <>
              {d.accounts.map((a) => (
                <Row key={`${a.provider}|${a.workspace}|${a.name}`}>
                  <CircleCheck className="size-3.5 text-success" aria-label="Linked" />
                  <span className="min-w-0 flex-1 truncate">{a.name}</span>
                  <span className="text-muted-foreground max-sm:hidden">{a.provider}</span>
                </Row>
              ))}
              {d.connections.map((c) => (
                <Row key={c.source_id}>
                  {c.status === "ok" ? <CircleCheck className="size-3.5 text-success" aria-label="Connected" /> : <CircleAlert className="size-3.5 text-destructive" aria-label="Needs attention" />}
                  <span className="min-w-0 flex-1 truncate">{c.source}</span>
                  {c.last_ok_at && <span className="text-xs text-muted-foreground max-sm:hidden"><Ago iso={c.last_ok_at} /></span>}
                  <Button variant="ghost" size="sm" disabled={disconnect.isPending} aria-label={`Disconnect ${c.source}`} onClick={async () => {
                    if (await confirm({ title: `Disconnect ${c.source}?`, action: "Disconnect", destructive: true })) disconnect.mutate(c)
                  }}>Disconnect</Button>
                </Row>
              ))}
              {open.map((c) => (
                <Row key={c.source_id}>
                  <span className="min-w-0 flex-1 truncate">{c.name}</span>
                  <Button size="sm" variant="outline" disabled={start.isPending} onClick={() => start.mutate(c)}>Connect</Button>
                </Row>
              ))}
              {!d.accounts.length && !d.connections.length && !open.length && <Empty>Nothing is linked</Empty>}
              {start.error && <p role="alert" className="px-4 py-3 text-right text-sm text-destructive">{start.error.message}</p>}
            </>
          )
        }}
      </QueryState>
      {starting && <DeviceCode s={starting} onClose={() => setStarting(null)} onDone={() => { setStarting(null); void qc.invalidateQueries({ queryKey: KEY }); toast("Connected") }} />}
    </Section>
  )
}

function DeviceCode({ s, onClose, onDone }: { s: Started; onClose: () => void; onDone: () => void }) {
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
        <DialogFooter className="max-sm:flex-row">
          <Button variant="outline" className="max-sm:flex-1" onClick={onClose}>Cancel</Button>
          <Button className="max-sm:flex-1" asChild><a href={s.url} target="_blank" rel="noreferrer"><ExternalLink />Open</a></Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
