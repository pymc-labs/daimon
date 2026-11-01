import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Check, ChevronDown, MoreHorizontal, Ticket, Users, UserRound } from "lucide-react"
import { useEffect, useState } from "react"
import { toast } from "sonner"
import { AgentAvatar } from "@/components/common"
import { useConfirm } from "@/components/confirm"
import { CreditMeter } from "@/components/CreditMeter"
import { QueryState } from "@/components/QueryState"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu"
import { Command, CommandGroup, CommandInput, CommandItem, CommandList } from "@/components/ui/command"
import { Input } from "@/components/ui/input"
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { api } from "@/lib/api"
import { shortDate, usd } from "@/lib/format"
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
  const qc = useQueryClient()
  const confirm = useConfirm()
  const del = useMutation({
    mutationFn: (a: Allowance) => api.del(`/api/credits/allowances/${a.id}`),
    onSuccess: () => { changed(qc); toast("Allowance removed") },
  })
  return (
    <div className="mx-auto max-w-[1040px]">
      <Table>
        <TableHeader>
          <TableRow className="hover:bg-transparent">
            <TableHead>For</TableHead><TableHead className="max-md:hidden">Agent</TableHead><TableHead className="text-right">Amount</TableHead>
            <TableHead className="w-48 max-sm:hidden">Used</TableHead><TableHead className="max-lg:hidden">Expires</TableHead><TableHead className="w-10" />
          </TableRow>
        </TableHeader>
        <TableBody>
          {data.map((a) => {
            const Icon = KIND_ICON[a.subject.kind]
            return (
              <TableRow key={a.id}>
                <TableCell>
                  <span className="flex items-center gap-2.5">
                    <span className="flex size-6 items-center justify-center rounded-full bg-accent"><Icon className="size-3.5 text-muted-foreground" /></span>
                    <span className="truncate font-medium">{a.subject.name}</span>
                    <span className="text-xs text-muted-foreground">{KIND_LABEL[a.subject.kind]}</span>
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
                <TableCell>
                  <DropdownMenu>
                    <DropdownMenuTrigger className="rounded p-1 text-muted-foreground hover:bg-accent" aria-label="Allowance actions"><MoreHorizontal className="size-4" /></DropdownMenuTrigger>
                    <DropdownMenuContent align="end">
                      <DropdownMenuItem variant="destructive" onSelect={async () => {
                        if (await confirm({ title: "Remove allowance?", body: a.subject.name, action: "Remove", destructive: true })) del.mutate(a)
                      }}>Remove</DropdownMenuItem>
                    </DropdownMenuContent>
                  </DropdownMenu>
                </TableCell>
              </TableRow>
            )
          })}
        </TableBody>
      </Table>
    </div>
  )
}

/** A group or a person, found by name; the choice is what the allowance is for. */
function SubjectPicker({ value, onChange }: { value: Subject | null; onChange: (s: Subject) => void }) {
  const [open, setOpen] = useState(false)
  const [text, setText] = useState("")
  const [term, setTerm] = useState("")
  useEffect(() => {
    const t = setTimeout(() => setTerm(text.trim()), 200)
    return () => clearTimeout(t)
  }, [text])
  const groups = useGroups()
  const people = usePeopleSearch(term)
  const shown = (groups.data ?? []).filter((g) => g.name.toLowerCase().includes(text.trim().toLowerCase()))
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
  const [subject, setSubject] = useState<Subject | null>(null)
  const [agent, setAgent] = useState("any")
  const [amount, setAmount] = useState("50")
  const [window, setWindow] = useState<Window>("month")
  const [expires, setExpires] = useState("")
  const qc = useQueryClient()
  const save = useMutation({
    mutationFn: () => api.post("/api/credits/allowances", {
      subject, agent: agents?.find((a) => a.id === agent) ?? null,
      amount_usd: Number(amount), window, expires_at: expires ? new Date(expires).toISOString() : null,
    }),
    onSuccess: () => { changed(qc); onOpenChange(false); toast("Allowance added") },
  })
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader><DialogTitle>New allowance</DialogTitle></DialogHeader>
        <div className="space-y-4">
          <Field label="For"><SubjectPicker value={subject} onChange={setSubject} /></Field>
          <Field label="Agent">
            <Select value={agent} onValueChange={setAgent}>
              <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="any">Any agent</SelectItem>
                {agents?.map((a) => <SelectItem key={a.id} value={a.id}><AgentAvatar src={a.avatar} name={a.name} className="size-4 rounded" />{a.name}</SelectItem>)}
              </SelectContent>
            </Select>
          </Field>
          <div className="grid grid-cols-2 gap-3">
            <Field label="Amount">
              <Input inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value.replace(/[^\d.]/g, ""))} />
            </Field>
            <Field label="Resets">
              <Select value={window} onValueChange={(v) => setWindow(v as Window)}>
                <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="month">Every month</SelectItem>
                  <SelectItem value="total">Never</SelectItem>
                </SelectContent>
              </Select>
            </Field>
          </div>
          <Field label="Expires">
            <Input type="date" value={expires} onChange={(e) => setExpires(e.target.value)} />
          </Field>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button onClick={() => save.mutate()} disabled={!subject || !Number(amount) || save.isPending}>Add allowance</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
