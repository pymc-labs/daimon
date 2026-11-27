import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { CircleAlert, CircleCheck } from "lucide-react"
import { useState } from "react"
import { toast } from "sonner"
import { BrandIcon } from "@/components/brand"
import { Ago } from "@/components/common"
import { Button } from "@/components/ui/button"
import { Switch } from "@/components/ui/switch"
import { api } from "@/lib/api"
import { useConfirm } from "@/lib/confirm"
import { Pill } from "@/modules/agents/bits"
import type { Connectable, Connected, Connection, PersonalPart, Started } from "@/modules/data/types"
import { DeviceCode } from "./DeviceCode"
import { Row } from "./profile-parts"

const BASE = "/api/profile/data"
const KEY = ["profile", "data"]
const PARTS: Record<PersonalPart, string> = { drive: "Read my Google Drive as me", calendar: "Read my calendar as me" }

/**
 * The data sources a person's accounts are linked to, and the ones they connected themselves,
 * as rows for the Connected accounts list. Link notices from admins show once, then are read.
 */
export function useDataAccounts(enabled: boolean) {
  const q = useQuery({
    queryKey: KEY,
    queryFn: () => api.get<Connected>(BASE),
    enabled,
    // A notice is read once the server sends it, so a refetch must not drop the ones shown.
    structuralSharing: (old, next) => {
      const was = (old as Connected | undefined)?.notices ?? []
      return { ...(next as Connected), notices: [...was, ...(next as Connected).notices] }
    },
  })
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
  const setPart = useMutation({
    mutationFn: ({ part, on }: { part: PersonalPart; on: boolean }) => api.put(`${BASE}/personal/${part}`, { on }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: KEY }),
  })
  const d = q.data
  const notices = d?.notices ?? []
  const have = new Set(d?.connections.map((c) => c.source_id))
  const open = d?.connectable.filter((c) => !have.has(c.source_id)) ?? []
  const parts = (Object.keys(PARTS) as PersonalPart[]).filter((p) => d?.personal[p] !== undefined)
  const count = notices.length + (d ? d.accounts.length + d.connections.length + open.length + parts.length : 0)
  const rows = d && (
    <>
      {notices.map((n, i) => (
        <Row key={`${n.identity}|${i}`}>
          <BrandIcon brand={n.provider} />
          <span className="min-w-0 flex-1 truncate">An admin {n.kind === "linked" ? "linked" : "unlinked"} {n.name || n.identity}{n.kind === "linked" ? " to" : " from"} your account</span>
          <Pill>New</Pill>
        </Row>
      ))}
      {d.accounts.map((a) => (
        <Row key={`${a.provider}|${a.workspace}|${a.name}`}>
          <BrandIcon brand={a.provider} />
          <span className="min-w-0 flex-1 truncate">{a.provider[0]!.toUpperCase() + a.provider.slice(1)}<span className="text-muted-foreground"> {a.name}</span></span>
          {a.linked_at && <span className="text-xs text-muted-foreground max-sm:hidden">Linked <Ago iso={a.linked_at} /></span>}
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
          <Button size="sm" variant="outline" disabled={start.isPending} aria-label={`Connect ${c.name}`} onClick={() => start.mutate(c)}>Connect</Button>
        </Row>
      ))}
      {parts.map((part) => (
        <Row key={part}>
          <span className="min-w-0 flex-1 truncate">{PARTS[part]}</span>
          <Switch checked={d.personal[part] ?? false} disabled={setPart.isPending} aria-label={PARTS[part]} onCheckedChange={(on) => setPart.mutate({ part, on })} />
        </Row>
      ))}
      {start.error && <p role="alert" className="px-4 py-3 text-right text-sm text-destructive">{start.error.message}</p>}
    </>
  )
  const dialog = starting && <DeviceCode s={starting} onClose={() => setStarting(null)} onDone={() => { setStarting(null); void qc.invalidateQueries({ queryKey: KEY }); toast("Connected") }} />
  return { rows, count, dialog }
}
