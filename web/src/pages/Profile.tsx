import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useState } from "react"
import { toast } from "sonner"

import { Ago } from "@/components/common"
import { BrandIcon } from "@/components/brand"
import { AgentLink } from "@/components/links"
import { QueryState } from "@/components/QueryState"
import { Page } from "@/components/shell/Page"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { api } from "@/lib/api"
import { useConfirm } from "@/lib/confirm"
import { plural, shortDate } from "@/lib/format"
import { useMe } from "@/lib/hooks"
import { Pill } from "@/modules/agents/bits"

type AgentRef = { id: string; name: string }
export type Profile = {
  name: string
  memory: { agent: AgentRef; files: string[] }[]
  connections: { kind: "microsoft" | "slack" | "mcp"; label: string; agent: AgentRef | null; key: string }[]
  sign_ins: { id: string; provider: string; label: string }[]
  chat_accounts: { platform: string; name: string | null }[]
  keys: { id: string; agent: AgentRef | null; created_at: string; expires_at: string | null; bound: boolean }[]
}
export type DeletePreview = { enabled: boolean; held: string[]; kept: string[] }

const KEY = ["profile"]

export function ProfilePage() {
  const q = useQuery({ queryKey: KEY, queryFn: () => api.get<Profile>("/api/profile") })
  return (
    <Page crumbs={[{ label: "Profile" }]}>
      <div className="mx-auto max-w-[760px] space-y-8">
        <QueryState query={q}>{(d) => <Body d={d} />}</QueryState>
      </div>
    </Page>
  )
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section aria-label={title}>
      <h2 className="mb-3 text-sm font-semibold">{title}</h2>
      <div className="divide-y rounded-lg border">{children}</div>
    </section>
  )
}

const Empty = ({ children }: { children: React.ReactNode }) => <div className="px-4 py-6 text-center text-sm text-muted-foreground">{children}</div>
const Row = ({ children }: { children: React.ReactNode }) => <div className="flex items-center gap-3 px-4 py-3 text-sm">{children}</div>

function Body({ d }: { d: Profile }) {
  const qc = useQueryClient()
  const confirm = useConfirm()
  const done = (msg: string) => { void qc.invalidateQueries({ queryKey: KEY }); toast(msg) }
  const forget = useMutation({
    mutationFn: (id: string) => api.post<{ removed: number }>(`/api/profile/memory/${id}/forget`),
    onSuccess: (r) => done(`Forgot ${plural(r.removed, "file")}`),
  })
  const disconnect = useMutation({
    mutationFn: (c: Profile["connections"][number]) => api.post("/api/profile/connections/disconnect", { kind: c.kind, agent_id: c.agent?.id, key: c.key }),
    onSuccess: () => done("Disconnected"),
  })
  const me = useMe()
  const accounts = me.data?.accounts ?? []
  const detach = useMutation({
    mutationFn: (id: string) => api.del(`/api/me/accounts/${id}`),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: ["me"] }); done("Disconnected") },
  })
  const unlink = useMutation({ mutationFn: (id: string) => api.del(`/api/profile/sign-ins/${id}`), onSuccess: () => done("Sign-in removed") })
  const revoke = useMutation({ mutationFn: (id: string) => api.del(`/api/profile/keys/${id}`), onSuccess: () => done("Key revoked") })
  const [deleting, setDeleting] = useState(false)
  return (
    <>
      <Section title="Memory">
        {!d.memory.length ? <Empty>No agent remembers anything about you</Empty> : d.memory.map((m) => (
          <Row key={m.agent.id}>
            <div className="min-w-0 flex-1">
              <div className="font-medium"><AgentLink id={m.agent.id} name={m.agent.name} /></div>
              <div className="truncate text-xs text-muted-foreground">{plural(m.files.length, "file")}: {m.files.join(", ")}</div>
            </div>
            <Button variant="ghost" size="sm" disabled={forget.isPending} aria-label={`Forget ${m.agent.name}`} onClick={async () => {
              if (await confirm({ title: `Forget what ${m.agent.name} remembers?`, body: "Its notes about you are deleted.", action: "Forget", destructive: true })) forget.mutate(m.agent.id)
            }}>Forget</Button>
          </Row>
        ))}
      </Section>

      <Section title="Connected accounts">
        {accounts.map((a) => (
          <Row key={a.id}>
            <BrandIcon brand={a.provider} />
            <span className="min-w-0 flex-1 truncate">{a.provider_label}{a.label && <span className="text-muted-foreground"> {a.label}</span>}</span>
            <span className="text-xs text-muted-foreground">{a.current ? "Signed in" : <>Used <Ago iso={a.last_used} /></>}</span>
            {!a.current && (
              <Button variant="ghost" size="sm" disabled={detach.isPending} aria-label={`Disconnect ${a.provider_label} ${a.label}`} onClick={async () => {
                if (await confirm({ title: `Disconnect ${a.provider_label}?`, action: "Disconnect", destructive: true })) detach.mutate(a.id)
              }}>Disconnect</Button>
            )}
          </Row>
        ))}
        {d.chat_accounts.map((a) => (
          <Row key={a.platform}>
            <BrandIcon brand={a.platform} />
            <span className="min-w-0 flex-1 truncate">{a.platform[0]!.toUpperCase() + a.platform.slice(1)}{a.name && <span className="text-muted-foreground"> {a.name}</span>}</span>
          </Row>
        ))}
        {d.sign_ins.map((s) => (
          <Row key={s.id}>
            <BrandIcon brand={s.provider} />
            <span className="min-w-0 flex-1 truncate">{s.label || s.provider}</span>
            <Pill>Sign-in</Pill>
            <Button variant="ghost" size="sm" disabled={d.sign_ins.length < 2 || unlink.isPending} aria-label={`Remove ${s.label || s.provider}`} onClick={async () => {
              if (await confirm({ title: "Remove this sign-in?", action: "Remove", destructive: true })) unlink.mutate(s.id)
            }}>Remove</Button>
          </Row>
        ))}
        {d.connections.map((c) => (
          <Row key={`${c.kind}:${c.agent?.id}:${c.key}`}>
            <span className="flex min-w-0 flex-1 items-center gap-1.5 truncate">{c.label}{c.agent && <><span className="text-muted-foreground">for</span><AgentLink id={c.agent.id} name={c.agent.name} /></>}</span>
            <Button variant="ghost" size="sm" disabled={disconnect.isPending} aria-label={`Disconnect ${c.label}`} onClick={async () => {
              if (await confirm({ title: `Disconnect ${c.label}?`, action: "Disconnect", destructive: true })) disconnect.mutate(c)
            }}>Disconnect</Button>
          </Row>
        ))}
        {!accounts.length && !d.chat_accounts.length && !d.sign_ins.length && !d.connections.length && <Empty>Nothing is connected</Empty>}
      </Section>

      <Section title="Coding keys">
        {!d.keys.length ? <Empty>No coding keys</Empty> : d.keys.map((k) => (
          <Row key={k.id}>
            <div className="min-w-0 flex-1">
              <div className="font-medium"><AgentLink id={k.agent?.id ?? null} name={k.agent?.name ?? null} /></div>
              <div className="text-xs text-muted-foreground">Made <Ago iso={k.created_at} />{k.expires_at ? `, ends ${shortDate(k.expires_at)}` : ""}{k.bound ? ", kept to a channel" : ""}</div>
            </div>
            <Button variant="ghost" size="sm" disabled={revoke.isPending} aria-label={`Revoke key for ${k.agent?.name ?? "agent"}`} onClick={async () => {
              if (await confirm({ title: "Revoke this key?", body: "Anything using it stops working.", action: "Revoke", destructive: true })) revoke.mutate(k.id)
            }}>Revoke</Button>
          </Row>
        ))}
      </Section>

      <Section title="Delete my data">
        <Row>
          <span className="flex-1">Remove you and everything tied to you from this workspace</span>
          <Button variant="destructive" size="sm" onClick={() => setDeleting(true)}>Delete my data</Button>
        </Row>
      </Section>
      {deleting && <DeleteDialog name={d.name} onClose={() => setDeleting(false)} />}
    </>
  )
}

function DeleteDialog({ name, onClose }: { name: string; onClose: () => void }) {
  const qc = useQueryClient()
  const [typed, setTyped] = useState("")
  const preview = useQuery({ queryKey: [...KEY, "delete"], queryFn: () => api.get<DeletePreview>("/api/profile/delete") })
  const erase = useMutation({
    mutationFn: () => api.post("/api/profile/delete", { confirm: typed }),
    onSuccess: () => { qc.clear(); location.assign("/signed-out") },
  })
  const p = preview.data
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader><DialogTitle>Delete my data</DialogTitle></DialogHeader>
        <QueryState query={preview}>{(d) => (
          <div className="space-y-4 text-sm">
            <div>
              <div className="mb-1 font-medium">Deleted</div>
              <ul className="list-disc space-y-0.5 pl-5">{d.held.length ? d.held.map((h) => <li key={h}>{h}</li>) : <li>Nothing listed</li>}</ul>
            </div>
            {d.kept.length > 0 && (
              <div>
                <div className="mb-1 font-medium">Kept</div>
                <ul className="list-disc space-y-0.5 pl-5">{d.kept.map((h) => <li key={h}>{h}</li>)}</ul>
              </div>
            )}
            {d.enabled ? (
              <div className="space-y-1.5">
                <Label htmlFor="confirm-name">Type {name} to confirm</Label>
                <Input id="confirm-name" autoComplete="off" value={typed} onChange={(e) => setTyped(e.target.value)} />
              </div>
            ) : <p className="text-muted-foreground">Deleting is paused. Ask an admin.</p>}
          </div>
        )}</QueryState>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>Cancel</Button>
          <Button variant="destructive" disabled={!p?.enabled || typed.trim().toLowerCase() !== name.trim().toLowerCase() || erase.isPending} onClick={() => erase.mutate()}>Delete</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
