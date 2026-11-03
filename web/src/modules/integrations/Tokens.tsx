import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Plus } from "lucide-react"
import { useState } from "react"
import { toast } from "sonner"
import { Ago, CopyButton, Mono } from "@/components/common"
import { QueryState } from "@/components/QueryState"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { api } from "@/lib/api"
import { useConfirm } from "@/lib/confirm"
import { shortDate } from "@/lib/format"
import { useCan } from "@/lib/hooks"
import { Pill } from "../agents/bits"
import type { Minted, Scope, Tokens as Data } from "./types"

const KEY = ["integrations", "tokens"]

export function Tokens() {
  const q = useQuery({ queryKey: KEY, queryFn: () => api.get<Data>("/api/integrations/tokens") })
  return <QueryState query={q}>{(d) => <Body d={d} />}</QueryState>
}

function Body({ d }: { d: Data }) {
  const can = useCan()("integrations.manage")
  const [creating, setCreating] = useState(false)
  const qc = useQueryClient()
  const confirm = useConfirm()
  const names = Object.fromEntries(d.scopes.map((s) => [s.id, s.label]))
  const revoke = useMutation({
    mutationFn: (id: string) => api.del(`/api/integrations/tokens/${id}`),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: KEY }); toast("Token revoked") },
  })
  return (
    <>
      <div className="mb-3 flex items-center justify-between gap-3">
        <h2 className="text-sm font-semibold">API tokens</h2>
        {can && d.configured && <Button size="sm" onClick={() => setCreating(true)}><Plus />New token</Button>}
      </div>
      {!d.configured ? <div className="rounded-lg border px-4 py-10 text-center text-sm text-muted-foreground">API tokens aren't set up on this server</div>
        : !d.tokens.length ? <div className="rounded-lg border px-4 py-10 text-center text-sm text-muted-foreground">No tokens yet</div> : (
          <Table>
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead>Name</TableHead><TableHead className="max-sm:hidden">Can</TableHead>
                <TableHead className="max-md:hidden">Created</TableHead><TableHead>Expires</TableHead><TableHead className="w-20" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {d.tokens.map((t) => (
                <TableRow key={t.id}>
                  <TableCell className="font-medium">{t.label ?? <span className="font-normal text-muted-foreground">Unnamed</span>}</TableCell>
                  <TableCell className="max-sm:hidden"><span className="flex flex-wrap gap-1">{t.scopes.map((s) => <Pill key={s}>{names[s] ?? s}</Pill>)}</span></TableCell>
                  <TableCell className="text-muted-foreground max-md:hidden"><Ago iso={t.created_at} /></TableCell>
                  <TableCell className="text-muted-foreground">{t.expires_at ? shortDate(t.expires_at) : "Never"}</TableCell>
                  <TableCell className="text-right">
                    {can && (
                      <Button variant="ghost" size="sm" disabled={revoke.isPending} onClick={async () => {
                        if (await confirm({ title: "Revoke this token?", body: "Anything using it stops working.", action: "Revoke", destructive: true })) revoke.mutate(t.id)
                      }}>Revoke</Button>
                    )}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      {creating && <NewToken scopes={d.scopes} days={d.days} onClose={() => setCreating(false)} />}
    </>
  )
}

/** Mint a token and show it once: closing the dialog drops it. */
export function NewToken({ scopes, days, onClose }: { scopes: Scope[]; days: number; onClose: () => void }) {
  const [label, setLabel] = useState("")
  const [picked, setPicked] = useState<string[]>([])
  const qc = useQueryClient()
  const mint = useMutation({
    mutationFn: () => api.post<Minted>("/api/integrations/tokens", { scopes: picked, label: label.trim() || undefined }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: KEY }),
  })
  const made = mint.data
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader><DialogTitle>{made ? "Copy your token" : "New API token"}</DialogTitle></DialogHeader>
        {made ? (
          <div className="space-y-2">
            <div className="flex items-center gap-1 rounded-md border bg-muted/40 py-1 pr-1 pl-3">
              <Mono className="min-w-0 flex-1 break-all">{made.token}</Mono>
              <CopyButton text={made.token} label="Copy token" />
            </div>
            <p className="text-sm text-muted-foreground">Shown once. Expires {shortDate(made.expires_at)}.</p>
          </div>
        ) : (
          <form id="mint" className="space-y-4" onSubmit={(e) => { e.preventDefault(); if (picked.length) mint.mutate() }}>
            <div className="space-y-1.5">
              <Label htmlFor="label">Name</Label>
              <Input id="label" autoComplete="off" maxLength={80} value={label} onChange={(e) => setLabel(e.target.value)} />
            </div>
            <fieldset className="space-y-2">
              <legend className="mb-1.5 text-sm font-medium">Can</legend>
              {scopes.map((s) => (
                <Label key={s.id} className="flex items-center gap-2.5 font-normal">
                  <Checkbox checked={picked.includes(s.id)} onCheckedChange={(c) => setPicked(c ? [...picked, s.id] : picked.filter((x) => x !== s.id))} />
                  {s.label}
                </Label>
              ))}
            </fieldset>
            <p className="text-xs text-muted-foreground">Lasts {days} days.</p>
          </form>
        )}
        <DialogFooter>
          {made ? <Button onClick={onClose}>Done</Button> : (
            <>
              <Button variant="outline" onClick={onClose}>Cancel</Button>
              <Button type="submit" form="mint" disabled={!picked.length || mint.isPending}>Create</Button>
            </>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
