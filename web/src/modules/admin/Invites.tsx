import { useMutation, useQueryClient } from "@tanstack/react-query"
import { useState } from "react"
import { toast } from "sonner"
import { AgentAvatar, CopyButton } from "@/components/common"
import { Drawer } from "@/components/Drawer"
import { rowProps, useParam } from "@/lib/filters"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { api } from "@/lib/api"
import { shortDate, usd } from "@/lib/format"
import { useChatAgents, usePaged } from "@/lib/hooks"
import { cn } from "@/lib/utils"
import { LoadMore, QueryState } from "@/components/QueryState"
import type { Invite } from "./types"
import { ROLE } from "./roles"
import { Facts, Field, PanelSection } from "./shared"

// Invites

const STATUS: Record<Invite["status"], string> = { pending: "Pending", accepted: "Accepted", expired: "Expired" }

export function Invites() {
  const q = usePaged<Invite>(["admin", "invites"], "/api/admin/invites")
  return <QueryState query={q} empty="No invites">{(data) => <><InviteTable data={data} /><LoadMore list={q} /></>}</QueryState>
}

function InviteTable({ data }: { data: Invite[] }) {
  const qc = useQueryClient()
  const [, setInvite] = useParam("invite")
  const revoke = useMutation({
    mutationFn: (i: Invite) => api.del(`/api/admin/invites/${i.id}`),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["admin", "invites"] }); setInvite(null); toast("Invite revoked") },
  })
  return (
    <>
    <Table>
      <TableHeader>
        <TableRow className="hover:bg-transparent">
          <TableHead>Email</TableHead><TableHead>Role</TableHead><TableHead className="max-md:hidden">Agents</TableHead>
          <TableHead className="text-right">Credit</TableHead><TableHead className="max-lg:hidden">Expires</TableHead>
          <TableHead>Status</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {data.map((i) => (
          <TableRow key={i.id} {...rowProps(() => setInvite(i.id))}>
            <TableCell className="font-medium">{i.email}</TableCell>
            <TableCell className="text-muted-foreground">{ROLE[i.role]}</TableCell>
            <TableCell className="max-md:hidden">
              {i.agents.length ? (
                <span className="flex items-center gap-1.5">
                  {i.agents.map((a) => <AgentAvatar key={a.id} src={a.avatar} name={a.name} className="size-5" />)}
                  <span className="truncate text-muted-foreground">{i.agents.map((a) => a.name).join(", ")}</span>
                </span>
              ) : <span className="text-muted-foreground">Any agent</span>}
            </TableCell>
            <TableCell className="text-right tabular">{usd(i.credit_usd)}</TableCell>
            <TableCell className="text-muted-foreground max-lg:hidden">{shortDate(i.expires_at)}</TableCell>
            <TableCell>
              <span className={cn("text-sm", i.status === "accepted" ? "text-success" : i.status === "expired" ? "text-faint" : "text-muted-foreground")}>{STATUS[i.status]}</span>
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
    <Drawer param="invite" title="Invite" ids={data.map((i) => i.id)}>
      {(id) => {
        const i = data.find((x) => x.id === id)
        return i && (
          <div>
            <div className="px-5 py-5">
              <div className="truncate text-base font-semibold">{i.email}</div>
              <div className="text-sm text-muted-foreground">{STATUS[i.status]}</div>
            </div>
            <PanelSection title="Details">
              <Facts rows={[["Role", ROLE[i.role]], ["Credit", usd(i.credit_usd)], ["Expires", shortDate(i.expires_at)], ["Invited by", i.by]]} />
            </PanelSection>
            <PanelSection title="Agents">
              {i.agents.length ? (
                <div className="space-y-1">
                  {i.agents.map((a) => <div key={a.id} className="flex h-8 items-center gap-2 text-sm"><AgentAvatar src={a.avatar} name={a.name} />{a.name}</div>)}
                </div>
              ) : <span className="text-sm text-muted-foreground">Any agent</span>}
            </PanelSection>
            {i.status === "pending" && (
              <div className="border-t px-5 py-4">
                <Button variant="outline" size="sm" className="text-destructive" disabled={revoke.isPending} onClick={() => revoke.mutate(i)}>Revoke</Button>
              </div>
            )}
          </div>
        )
      }}
    </Drawer>
    </>
  )
}

export function NewInvite() {
  const [dialog, setDialog] = useParam("dialog")
  const open = dialog === "invite"
  const { data: agents } = useChatAgents()
  const [email, setEmail] = useState("")
  const [role, setRole] = useState<Invite["role"]>("visitor")
  const [picked, setPicked] = useState<string[]>([])
  const [credit, setCredit] = useState("5")
  const [days, setDays] = useState("14")
  const [made, setMade] = useState<Invite | null>(null)
  const qc = useQueryClient()
  const create = useMutation({
    mutationFn: () => api.post<Invite>("/api/admin/invites", { email, role, agents: role === "visitor" ? picked : [], credit_usd: Number(credit), days: Number(days) }),
    onSuccess: (inv) => { void qc.invalidateQueries({ queryKey: ["admin"] }); setMade(inv) },
  })
  const close = (o: boolean) => { if (!o) { setDialog(null); setMade(null); setEmail("") } }
  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader><DialogTitle>{made ? "Invite ready" : "Invite"}</DialogTitle></DialogHeader>
        {made ? (
          <>
            <div className="space-y-1.5">
              <Label>One-time link for {made.email}</Label>
              <div className="flex items-center gap-1 rounded-md border bg-muted px-3 py-2">
                <span className="min-w-0 flex-1 truncate font-mono text-xs">{made.link}</span>
                <CopyButton text={made.link!} />
              </div>
            </div>
            <div className="text-xs text-muted-foreground">Expires {shortDate(made.expires_at)}</div>
            <DialogFooter><Button onClick={() => close(false)}>Done</Button></DialogFooter>
          </>
        ) : (
          <>
            <div className="space-y-4">
              <Field label="Email"><Input type="email" placeholder="name@company.com" value={email} onChange={(e) => setEmail(e.target.value)} /></Field>
              <Field label="Role">
                <ToggleGroup type="single" value={role} onValueChange={(v) => v && setRole(v as Invite["role"])} variant="segmented" size="sm">
                  <ToggleGroupItem value="visitor" className="px-3 text-xs">Visitor</ToggleGroupItem>
                  <ToggleGroupItem value="member" className="px-3 text-xs">Member</ToggleGroupItem>
                </ToggleGroup>
              </Field>
              {role === "visitor" && (
                <Field label="Agents">
                  <div className="divide-y rounded-md border">
                    {agents?.map((a) => (
                      <label key={a.id} className="flex h-9 cursor-pointer items-center gap-2.5 px-3 text-sm hover:bg-accent">
                        <Checkbox checked={picked.includes(a.id)} onCheckedChange={(c) => setPicked((p) => (c ? [...p, a.id] : p.filter((x) => x !== a.id)))} />
                        <AgentAvatar src={a.avatar} name={a.name} className="size-5" />
                        {a.name}
                        <span className="ml-auto text-xs text-muted-foreground">{a.model}</span>
                      </label>
                    ))}
                  </div>
                </Field>
              )}
              <div className="grid grid-cols-2 gap-3">
                <Field label={role === "visitor" ? "Credit" : "Credit a month"}>
                  <Input inputMode="decimal" value={credit} onChange={(e) => setCredit(e.target.value.replace(/[^\d.]/g, ""))} />
                </Field>
                <Field label="Link expires">
                  <Select value={days} onValueChange={setDays}>
                    <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
                    <SelectContent>{["1", "7", "14", "30"].map((d) => <SelectItem key={d} value={d}>{d === "1" ? "1 day" : `${d} days`}</SelectItem>)}</SelectContent>
                  </Select>
                </Field>
              </div>
            </div>
            <DialogFooter>
              <Button variant="outline" onClick={() => close(false)}>Cancel</Button>
              <Button onClick={() => create.mutate()} disabled={!email.includes("@") || (role === "visitor" && !picked.length) || create.isPending}>Create link</Button>
            </DialogFooter>
          </>
        )}
      </DialogContent>
    </Dialog>
  )
}
