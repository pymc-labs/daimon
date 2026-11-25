import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Plus } from "lucide-react"
import { useState } from "react"
import { toast } from "sonner"
import { QueryState } from "@/components/QueryState"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Switch } from "@/components/ui/switch"
import { api } from "@/lib/api"
import { useConfirm } from "@/lib/confirm"
import { useCan } from "@/lib/hooks"
import { Pill } from "../agents/bits"
import type { Connection, ConnectionTool, Connections as Data } from "./types"

const KEY = ["integrations", "connections"]
const BASE = "/api/integrations/connections"
const note = "rounded-lg border px-4 py-10 text-center text-sm text-muted-foreground"

export function Connections() {
  const q = useQuery({ queryKey: KEY, queryFn: () => api.get<Data>(BASE) })
  return <QueryState query={q}>{(d) => <Body d={d} />}</QueryState>
}

/** One mutation shape for every change: call, refresh the list, surface a refusal. */
function useChange<T>(fn: (v: T) => Promise<unknown>, done?: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: fn,
    onSuccess: () => { void qc.invalidateQueries({ queryKey: KEY }); if (done) toast(done) },
    onError: (e) => toast.error(e instanceof Error ? e.message : "Something went wrong"),
  })
}

function Body({ d }: { d: Data }) {
  const can = useCan()("integrations.manage") && d.enabled
  const [adding, setAdding] = useState(false)
  return (
    <>
      <div className="mb-3 flex items-center justify-between gap-3">
        <h2 className="text-sm font-semibold">MCP connections</h2>
        {can && <Button size="sm" onClick={() => setAdding(true)}><Plus />Add connection</Button>}
      </div>
      {!d.enabled && <div className={`${note} mb-3`}>Shared connections are turned off for this workspace</div>}
      {d.enabled && !d.connections.length ? <div className={note}>No connections yet</div> : (
        <div className="space-y-4">{d.connections.map((c) => <Item key={c.id} c={c} can={can} />)}</div>
      )}
      {adding && <Add onClose={() => setAdding(false)} />}
    </>
  )
}

function Item({ c, can }: { c: Connection; can: boolean }) {
  const confirm = useConfirm()
  const url = `${BASE}/${c.id}`
  const active = c.status === "active"
  const refresh = useChange(() => api.post(`${url}/refresh`), "Tools refreshed")
  const pause = useChange(() => api.patch(url, { status: active ? "paused" : "active" }))
  const remove = useChange(() => api.del(url), "Connection removed")
  return (
    <section className="rounded-lg border">
      <header className="flex flex-wrap items-center gap-2 border-b px-4 py-3">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="font-medium">{c.name}</span>
            <Pill>{c.status === "active" ? "Active" : c.status === "paused" ? "Paused" : "Error"}</Pill>
            {c.declared && <Pill>Declared</Pill>}
            {c.scope && <Pill>{c.scope}</Pill>}
          </div>
          <div className="truncate font-mono text-[12px] text-muted-foreground">{c.url}</div>
          {c.error && <div className="mt-1 text-xs text-destructive">{c.error}</div>}
        </div>
        {can && (
          <div className="flex gap-1">
            <Button variant="ghost" size="sm" disabled={refresh.isPending} onClick={() => refresh.mutate(undefined)}>Refresh</Button>
            <Button variant="ghost" size="sm" disabled={pause.isPending} onClick={() => pause.mutate(undefined)}>{active ? "Pause" : "Resume"}</Button>
            {!c.declared && <Button variant="ghost" size="sm" disabled={remove.isPending} onClick={async () => {
              if (await confirm({ title: `Remove ${c.name}?`, body: "Its tools stop being offered to agents and clients.", action: "Remove", destructive: true })) remove.mutate(undefined)
            }}>Remove</Button>}
          </div>
        )}
      </header>
      {!c.tools.length ? <div className="px-4 py-6 text-sm text-muted-foreground">No tools found yet</div> : (
        <ul className="divide-y">{c.tools.map((t) => <Tool key={t.name} c={c} t={t} can={can} />)}</ul>
      )}
    </section>
  )
}

/** Third-party tool text is untrusted: the admin reads it here before turning the tool on. */
function Tool({ c, t, can }: { c: Connection; t: ConnectionTool; can: boolean }) {
  const url = `${BASE}/${c.id}/tools/${encodeURIComponent(t.name)}`
  const turn = useChange((v: { enabled?: boolean; seen?: string; writes?: boolean }) => api.patch(url, v))
  const review = !t.enabled || t.changed
  return (
    <li className="space-y-1.5 px-4 py-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-[13px] font-medium">{c.name}_{t.name}</span>
        {t.enabled && !t.changed && <Pill>On</Pill>}
        {t.changed && <Pill className="text-warning">Changed</Pill>}
        {t.scope && <Pill>{t.scope}</Pill>}
        <span className="ml-auto flex items-center gap-3">
          {can && (
            <Label className="flex items-center gap-2 font-normal">
              <Switch checked={t.writes} disabled={turn.isPending} onCheckedChange={(writes) => turn.mutate({ writes })} />
              Changes data
            </Label>
          )}
          {can && review && <Button size="sm" disabled={turn.isPending} onClick={() => turn.mutate({ enabled: true, seen: t.seen })}>{t.changed ? "Approve change" : "Turn on"}</Button>}
          {can && t.enabled && <Button variant="ghost" size="sm" disabled={turn.isPending} onClick={() => turn.mutate({ enabled: false })}>Turn off</Button>}
        </span>
      </div>
      {review && (
        <div className="rounded-md bg-muted/40 px-3 py-2 text-sm">
          <p className="whitespace-pre-wrap break-words">{t.seen_description || "No description"}</p>
          <pre className="mt-1 max-h-64 overflow-auto whitespace-pre-wrap break-words font-mono text-[12px] text-muted-foreground">{JSON.stringify(t.seen_schema, null, 2)}</pre>
        </div>
      )}
    </li>
  )
}

function Add({ onClose }: { onClose: () => void }) {
  const [f, setF] = useState({ name: "", url: "", token: "", scope: "" })
  const set = (k: keyof typeof f) => (e: React.ChangeEvent<HTMLInputElement>) => setF({ ...f, [k]: e.target.value })
  const qc = useQueryClient()
  const add = useMutation({
    mutationFn: () => api.post(BASE, { name: f.name.trim(), url: f.url.trim(), token: f.token || undefined, scope: f.scope.trim() || undefined }),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: KEY }); onClose() },
    onError: (e) => toast.error(e instanceof Error ? e.message : "Couldn't add the connection"),
  })
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader><DialogTitle>Add connection</DialogTitle></DialogHeader>
        <form id="add" className="space-y-4" onSubmit={(e) => { e.preventDefault(); add.mutate() }}>
          <div className="space-y-1.5"><Label htmlFor="c-name">Name</Label><Input id="c-name" autoComplete="off" maxLength={32} value={f.name} onChange={set("name")} /></div>
          <div className="space-y-1.5"><Label htmlFor="c-url">Server URL</Label><Input id="c-url" autoComplete="off" placeholder="https://mcp.example.com/mcp" value={f.url} onChange={set("url")} /></div>
          <div className="space-y-1.5"><Label htmlFor="c-token">Access token</Label><Input id="c-token" type="password" autoComplete="off" value={f.token} onChange={set("token")} /></div>
          <div className="space-y-1.5"><Label htmlFor="c-scope">Scope (optional)</Label><Input id="c-scope" autoComplete="off" value={f.scope} onChange={set("scope")} /></div>
        </form>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>Cancel</Button>
          <Button type="submit" form="add" disabled={!f.name.trim() || !f.url.trim() || add.isPending}>Add</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
