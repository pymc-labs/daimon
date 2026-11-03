import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Check, ChevronDown, Ticket, Users, UserRound } from "lucide-react"
import { useEffect, useState } from "react"
import { toast } from "sonner"
import { Link } from "react-router"
import { AgentAvatar } from "@/components/common"
import { Empty, FilterBar, SearchInput } from "@/components/filters"
import { AgentLink, PersonLink } from "@/components/links"
import { Facts, PanelHead, PanelSection, Tag } from "@/components/panel"
import { useConfirm } from "@/lib/confirm"
import { CreditMeter } from "@/components/CreditMeter"
import { Drawer } from "@/components/Drawer"
import { rowProps, useParam } from "@/lib/filters"
import { QueryState } from "@/components/QueryState"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Command, CommandGroup, CommandInput, CommandItem, CommandList } from "@/components/ui/command"
import { Input } from "@/components/ui/input"
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { api } from "@/lib/api"
import { ENTITY_FALLBACK, shortDate, usd } from "@/lib/format"
import { useChatAgents, useGroups, usePeopleSearch } from "@/lib/hooks"
import type { Allowance, Subject, Window } from "./types"
import { Field } from "./shared"

const KIND_ICON = { person: UserRound, group: Users, invite: Ticket }
const KIND_LABEL = { person: "Person", group: "Group", invite: "Invite" }

export function Allowances() {
  const q = useQuery({ queryKey: ["credits", "allowances"], queryFn: () => api.get<Allowance[]>("/api/credits/allowances") })
  return <QueryState query={q} empty="No allowances">{(data) => <AllowanceTable data={data} />}</QueryState>
}

/** What an allowance change touches: the lists, the person's own meter, and the People credit column. */
const changed = (qc: ReturnType<typeof useQueryClient>) => {
  for (const key of [["credits"], ["me"], ["admin", "people"]]) void qc.invalidateQueries({ queryKey: key })
}

function AllowanceTable({ data }: { data: Allowance[] }) {
  const [search, setSearch] = useParam("q")
  const [, setAllowance] = useParam("allowance")
  const needle = (search ?? "").toLowerCase()
  const shown = data.filter((a) => !needle || `${a.subject.name} ${a.agent?.name ?? ""}`.toLowerCase().includes(needle))
  return (
    <div className="mx-auto max-w-[1040px]">
      <FilterBar className="px-0 pt-0">
        <SearchInput value={search} onChange={(v) => setSearch(v || null)} className="w-48" />
        <span className="ml-auto text-xs text-muted-foreground tabular">{shown.length} of {data.length}</span>
      </FilterBar>
      {!shown.length ? <Empty>Nothing matches</Empty> : (
        <Table>
          <TableHeader>
            <TableRow className="hover:bg-transparent">
              <TableHead>For</TableHead><TableHead className="max-md:hidden">Agent</TableHead><TableHead className="text-right">Amount</TableHead>
              <TableHead className="w-48 max-sm:hidden">Used</TableHead><TableHead className="max-lg:hidden">Expires</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {shown.map((a) => {
              const Icon = KIND_ICON[a.subject.kind]
              return (
                <TableRow key={a.id} {...rowProps(() => setAllowance(a.id))}>
                  <TableCell>
                    <span className="flex items-center gap-2.5">
                      <span className="flex size-6 items-center justify-center rounded-full bg-accent"><Icon className="size-3.5 text-muted-foreground" /></span>
                      <span className="truncate font-medium">{a.subject.name || (a.subject.kind === "group" ? "Deleted group" : ENTITY_FALLBACK.person)}</span>
                      <span className="text-xs text-muted-foreground">{KIND_LABEL[a.subject.kind]}</span>
                      {a.cap && <Tag>Cap</Tag>}
                    </span>
                  </TableCell>
                  <TableCell className="max-md:hidden">
                    {a.agent ? <span className="flex items-center gap-2"><AgentAvatar src={a.agent.avatar} name={a.agent.name} className="size-5" />{a.agent.name}</span>
                      : <span className="text-muted-foreground">Any agent</span>}
                  </TableCell>
                  <TableCell className="text-right whitespace-nowrap">
                    <span className="font-medium tabular">{usd(a.amount_usd)}</span>
                    <span className="text-muted-foreground">{a.window === "month" ? " / month" : " total"}</span>
                  </TableCell>
                  <TableCell className="max-sm:hidden">
                    {a.subject.kind === "group" ? <span className="text-muted-foreground">Each of {a.members} members</span>
                      : <CreditMeter compact meter={{ left_usd: a.amount_usd - a.used_usd, total_usd: a.amount_usd, window: a.window, resets_at: null }} />}
                  </TableCell>
                  <TableCell className="text-muted-foreground max-lg:hidden">{a.expires_at ? shortDate(a.expires_at) : "Never"}</TableCell>
                </TableRow>
              )
            })}
          </TableBody>
        </Table>
      )}
      <Drawer param="allowance" title="Allowance" ids={data.map((a) => a.id)}>
        {(id) => {
          const a = data.find((x) => x.id === id)
          return a ? <Detail a={a} /> : null
        }}
      </Drawer>
    </div>
  )
}

function SubjectName({ s }: { s: Subject }) {
  if (s.kind === "group") return <Link to={`/admin?group=${encodeURIComponent(s.id)}`} className="truncate hover:underline">{s.name || "Deleted group"}</Link>
  if (s.kind === "person") return <PersonLink id={s.id} name={s.name || null} />
  return <span className="truncate">{s.name}</span>
}

function Detail({ a }: { a: Allowance }) {
  const qc = useQueryClient()
  const confirm = useConfirm()
  const [, setAllowance] = useParam("allowance")
  const [editing, setEditing] = useState(false)
  const [amount, setAmount] = useState(String(a.amount_usd))
  const Icon = KIND_ICON[a.subject.kind]
  const del = useMutation({
    mutationFn: () => api.del(`/api/credits/allowances/${a.id}`),
    onSuccess: () => { changed(qc); setAllowance(null); toast("Allowance removed") },
  })
  const save = useMutation({
    mutationFn: () => api.patch(`/api/credits/allowances/${a.id}`, { amount_usd: Number(amount) }),
    onSuccess: () => { changed(qc); setEditing(false); toast("Allowance saved") },
  })
  const dirty = Number(amount) !== a.amount_usd
  const per = a.window === "month" ? " / month" : " total"
  return (
    <form className="flex min-h-full flex-col" onSubmit={(e) => { e.preventDefault(); if (dirty && Number(amount) > 0) save.mutate() }}>
      <PanelHead
        icon={<Icon className="size-4 text-muted-foreground" />}
        title={<SubjectName s={a.subject} />}
        subtitle={KIND_LABEL[a.subject.kind]}
        tags={a.cap ? <Tag>Cap</Tag> : undefined}
        actions={!editing && <Button type="button" variant="outline" size="sm" onClick={() => { setAmount(String(a.amount_usd)); setEditing(true) }}>Edit</Button>}
        menu={[{
          label: "Remove", destructive: true, disabled: del.isPending,
          onSelect: async () => { if (await confirm({ title: "Remove allowance?", body: a.subject.name, action: "Remove", destructive: true })) del.mutate() },
        }]}
      />
      <PanelSection>
        <Facts rows={[
          ["Amount", editing
            ? <span key="amount" className="flex items-center gap-2"><Input className="h-8 w-28" inputMode="decimal" aria-label="Amount" value={amount} onChange={(e) => setAmount(e.target.value.replace(/[^\d.]/g, ""))} /><span className="text-muted-foreground">{per.trim()}</span></span>
            : `${usd(a.amount_usd)}${per}`],
          ["Applies to", a.subject.kind === "group" ? `Each of ${a.members} members` : null],
          ["Used", a.subject.kind === "group" ? null : usd(a.used_usd)],
          ["Agent", a.agent ? <AgentLink key="agent" id={a.agent.id} name={a.agent.name} avatar={a.agent.avatar} /> : "Any agent"],
          ["Expires", a.expires_at ? shortDate(a.expires_at) : "Never"],
        ]} />
      </PanelSection>
      {editing && (
        <div className="sticky bottom-0 mt-auto flex justify-end gap-2 border-t bg-popover px-5 py-3">
          <Button type="button" variant="outline" onClick={() => setEditing(false)}>Discard</Button>
          <Button type="submit" disabled={!dirty || !Number(amount) || save.isPending}>Save</Button>
        </div>
      )}
    </form>
  )
}

/** A group or a person, found by name; the choice is what the allowance is for. */
function SubjectPicker({ value, onChange, everyoneOnly }: { value: Subject | null; onChange: (s: Subject) => void; everyoneOnly?: boolean }) {
  const [open, setOpen] = useState(false)
  const [text, setText] = useState("")
  const [term, setTerm] = useState("")
  useEffect(() => {
    const t = setTimeout(() => setTerm(text.trim()), 200)
    return () => clearTimeout(t)
  }, [text])
  const groups = useGroups()
  const people = usePeopleSearch(term)
  const shown = (groups.data ?? []).filter((g) => (!everyoneOnly || g.id === "everyone") && g.name.toLowerCase().includes(text.trim().toLowerCase()))
  const pick = (s: Subject) => { onChange(s); setOpen(false) }
  const Icon = value ? KIND_ICON[value.kind] : null
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button variant="outline" className="w-full justify-between font-normal">
          <span className="flex items-center gap-2 truncate">{Icon && <Icon className="text-muted-foreground" />}{value?.name ?? "Choose"}</span>
          <ChevronDown className="size-4 text-faint" />
        </Button>
      </PopoverTrigger>
      <PopoverContent className="w-[var(--radix-popover-trigger-width)] p-0" align="start">
        <Command shouldFilter={false}>
          <CommandInput placeholder="Find a group or person" value={text} onValueChange={setText} />
          <CommandList className="max-h-64">
            {shown.length > 0 && (
              <CommandGroup heading="Groups">
                {shown.map((g) => (
                  <CommandItem key={g.id} value={`group:${g.id}`} onSelect={() => pick({ kind: "group", id: g.id, name: g.name })}>
                    <Users className="text-muted-foreground" />{g.name}
                    {value?.kind === "group" && value.id === g.id && <Check className="ml-auto" />}
                  </CommandItem>
                ))}
              </CommandGroup>
            )}
            {!!people.data?.length && (
              <CommandGroup heading="People">
                {people.data.map((p) => (
                  <CommandItem key={p.id} value={`person:${p.id}`} onSelect={() => pick({ kind: "person", id: p.id, name: p.name })}>
                    <UserRound className="text-muted-foreground" />{p.name}
                    {value?.kind === "person" && value.id === p.id && <Check className="ml-auto" />}
                  </CommandItem>
                ))}
              </CommandGroup>
            )}
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  )
}

export function NewAllowance({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const { data: agents } = useChatAgents()
  const [cap, setCap] = useState(false)
  const [subject, setSubject] = useState<Subject | null>(null)
  const [agent, setAgent] = useState("any")
  const [amount, setAmount] = useState("50")
  const [window, setWindow] = useState<Window>("month")
  const [expires, setExpires] = useState("")
  const qc = useQueryClient()
  const save = useMutation({
    mutationFn: () => api.post("/api/credits/allowances", {
      subject, agent: cap ? null : agents?.find((a) => a.id === agent) ?? null,
      amount_usd: Number(amount), window: cap ? "month" : window, expires_at: !cap && expires ? new Date(expires).toISOString() : null, cap,
    }),
    onSuccess: () => { changed(qc); onOpenChange(false); toast(cap ? "Cap added" : "Allowance added") },
  })
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader><DialogTitle>{cap ? "New cap" : "New allowance"}</DialogTitle></DialogHeader>
        <div className="space-y-4">
          <Field label="Type">
            <Select value={cap ? "cap" : "allowance"} onValueChange={(v) => { setCap(v === "cap"); setSubject(null) }}>
              <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="allowance">Allowance</SelectItem>
                <SelectItem value="cap">Cap</SelectItem>
              </SelectContent>
            </Select>
          </Field>
          <Field label="For"><SubjectPicker value={subject} onChange={setSubject} everyoneOnly={cap} /></Field>
          {!cap && (
            <Field label="Agent">
              <Select value={agent} onValueChange={setAgent}>
                <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="any">Any agent</SelectItem>
                  {agents?.map((a) => <SelectItem key={a.id} value={a.id}><AgentAvatar src={a.avatar} name={a.name} className="size-4 rounded" />{a.name}</SelectItem>)}
                </SelectContent>
              </Select>
            </Field>
          )}
          <div className="grid grid-cols-2 gap-3">
            <Field label="Amount">
              <Input inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value.replace(/[^\d.]/g, ""))} />
            </Field>
            {!cap && (
              <Field label="Resets">
                <Select value={window} onValueChange={(v) => setWindow(v as Window)}>
                  <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="month">Every month</SelectItem>
                    <SelectItem value="total">Never</SelectItem>
                  </SelectContent>
                </Select>
              </Field>
            )}
          </div>
          {!cap && (
            <Field label="Expires">
              <Input type="date" value={expires} onChange={(e) => setExpires(e.target.value)} />
            </Field>
          )}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button onClick={() => save.mutate()} disabled={!subject || !Number(amount) || save.isPending}>{cap ? "Add cap" : "Add allowance"}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
