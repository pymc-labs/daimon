import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { BrandIcon } from "@/components/brand"
import { Ago, Avatar, CopyButton } from "@/components/common"
import { useConfirm } from "@/components/confirm"
import { CreditMeter } from "@/components/CreditMeter"
import { Drawer } from "@/components/Drawer"
import { LoadMore, QueryState, Rows } from "@/components/QueryState"
import { Facet, FilterBar, rowProps, SearchInput, useListParam, useParam } from "@/components/filters"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { api } from "@/lib/api"
import { usePaged } from "@/lib/hooks"
import type { Role } from "@/lib/types"
import { cn } from "@/lib/utils"
import type { AdminPerson } from "./types"
import { ROLE, Tag } from "./shared"

const usePeople = () => usePaged<AdminPerson>(["admin", "people"], "/api/admin/people")
// People

export function People() {
  const query = usePeople()
  const { data } = query
  const [q, setQ] = useParam("q")
  const [role, setRole] = useListParam("role")
  const [, setPerson] = useParam("person")
  const rows = (data ?? []).filter((p) => (!q || `${p.name} ${p.email}`.toLowerCase().includes(q.toLowerCase())) && (!role.length || role.includes(p.role)))
  return (
    <>
      <FilterBar className="px-0 pt-0">
        <SearchInput value={q} onChange={(v) => setQ(v || null)} className="w-56" />
        <Facet label="Role" value={role} onChange={setRole} options={Object.entries(ROLE).map(([value, label]) => ({ value, label }))} />
        <span className="ml-auto text-xs text-muted-foreground tabular">{rows.length} people</span>
      </FilterBar>
      <QueryState query={query} isEmpty={() => !rows.length} empty="No people">{() => (
        <Table>
          <TableHeader>
            <TableRow className="hover:bg-transparent">
              <TableHead>Name</TableHead><TableHead>Role</TableHead><TableHead className="max-lg:hidden">Groups</TableHead>
              <TableHead className="max-md:hidden">Sign-in</TableHead>
              <TableHead className="text-right">Last seen</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((p) => (
              <TableRow key={p.id} {...rowProps(() => setPerson(p.id))}>
                <TableCell>
                  <span className="flex items-center gap-2.5">
                    <Avatar name={p.name} />
                    <span className={cn("truncate font-medium", p.status === "suspended" && "text-muted-foreground line-through decoration-faint")}>{p.name}</span>
                    <span className="truncate text-muted-foreground max-xl:hidden">{p.email}</span>
                    {p.status === "suspended" && <Tag>Suspended</Tag>}
                  </span>
                </TableCell>
                <TableCell className="text-muted-foreground">{ROLE[p.role]}</TableCell>
                <TableCell className="max-w-56 truncate text-muted-foreground max-lg:hidden">{p.groups.join(", ") || "–"}</TableCell>
                <TableCell className="max-md:hidden">
                  <span className="flex gap-1.5">{p.identities.map((i) => <BrandIcon key={i.provider} brand={i.provider} icon={i.icon} />)}</span>
                </TableCell>
                <TableCell className="text-right text-muted-foreground"><Ago iso={p.last_seen} /></TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}</QueryState>
      <LoadMore list={query} />
      <Drawer param="person" title="Person" ids={rows.map((p) => p.id)}>{(id) => <PersonView id={id} />}</Drawer>
    </>
  )
}

function Section({ title, children, aside }: { title: string; children: React.ReactNode; aside?: React.ReactNode }) {
  return (
    <section className="border-t px-5 py-4">
      <div className="mb-2 flex items-center justify-between"><h3 className="text-xs font-medium text-muted-foreground">{title}</h3>{aside}</div>
      {children}
    </section>
  )
}

function PersonView({ id }: { id: string }) {
  const qc = useQueryClient()
  const q = useQuery({ queryKey: ["admin", "person", id], queryFn: () => api.get<AdminPerson>(`/api/admin/people/${id}`) })
  const save = useMutation({
    mutationFn: (b: Partial<AdminPerson>) => api.patch<AdminPerson>(`/api/admin/people/${id}`, b),
    onSuccess: (person) => {
      qc.setQueryData(["admin", "person", id], person)
      void qc.invalidateQueries({ queryKey: ["admin", "people"] })
      void qc.invalidateQueries({ queryKey: ["admin", "audit"] })
      void qc.invalidateQueries({ queryKey: ["me"] })
      void qc.invalidateQueries({ queryKey: ["chat", "agents"] })
    },
  })
  return <QueryState query={q} className="p-5" skeleton={<div className="p-5"><Rows n={2} /></div>}>{(p) => <PersonBody p={p} save={save} />}</QueryState>
}

function PersonBody({ p, save }: { p: AdminPerson; save: { mutate: (b: Partial<AdminPerson>) => void } }) {
  const confirm = useConfirm()
  return (
    <div>
      <div className="flex items-center gap-3 px-5 py-5">
        <Avatar name={p.name} className="size-10 text-sm" />
        <div className="min-w-0">
          <div className="truncate text-base font-semibold">{p.name}</div>
          <div className="truncate text-sm text-muted-foreground">{p.email}</div>
        </div>
        {p.status === "suspended" && <span className="ml-auto"><Tag>Suspended</Tag></span>}
      </div>
      <Section title="Role">
        <Select value={p.role} onValueChange={(v) => save.mutate({ role: v as Role })} disabled={p.role === "owner"}>
          <SelectTrigger className="w-48"><SelectValue /></SelectTrigger>
          <SelectContent>{Object.entries(ROLE).map(([v, l]) => <SelectItem key={v} value={v}>{l}</SelectItem>)}</SelectContent>
        </Select>
      </Section>
      <Section title="Groups">
        <div className="flex flex-wrap gap-1.5">{p.groups.length ? p.groups.map((g) => <Tag key={g}>{g}</Tag>) : <span className="text-sm text-muted-foreground">None</span>}</div>
      </Section>
      <Section title="Sign-in">
        <div className="space-y-1">
          {p.identities.map((i) => (
            <div key={i.provider} className="flex h-8 items-center gap-2.5 text-sm">
              <BrandIcon brand={i.provider} icon={i.icon} />
              <span>{i.provider_label}</span>
              <span className="truncate text-muted-foreground">{i.label}</span>
            </div>
          ))}
        </div>
      </Section>
      {p.credit && (
        <Section title="Credit">
          <CreditMeter meter={{ ...p.credit, window: p.role === "visitor" ? "total" : "month", resets_at: null }} />
        </Section>
      )}
      <Section title="Last seen"><span className="text-sm"><Ago iso={p.last_seen} /></span></Section>
      {p.role === "visitor" && p.status === "active" && <SignInLink person={p} />}
      {p.role !== "owner" && (
        <div className="border-t px-5 py-4">
          {p.status === "active" ? (
            <Button variant="outline" size="sm" className="text-destructive" onClick={async () => {
              if (await confirm({ title: `Suspend ${p.name}?`, body: "Their chats stop on every platform.", action: "Suspend", destructive: true })) save.mutate({ status: "suspended" })
            }}>Suspend</Button>
          ) : <Button variant="outline" size="sm" onClick={() => save.mutate({ status: "active" })}>Restore</Button>}
        </div>
      )}
    </div>
  )
}

/** A fresh one-time sign-in link for a visitor who lost theirs; Daimon sends no email. */
function SignInLink({ person }: { person: AdminPerson }) {
  const make = useMutation({ mutationFn: () => api.post<{ link: string }>(`/api/admin/people/${person.id}/link`) })
  return (
    <Section title="Sign-in link">
      {make.data ? (
        <div className="flex gap-2">
          <Input readOnly value={make.data.link} className="h-8 font-mono text-[12.5px]" onFocus={(e) => e.target.select()} />
          <CopyButton text={make.data.link} />
        </div>
      ) : <Button variant="outline" size="sm" onClick={() => make.mutate()} disabled={make.isPending}>Create link</Button>}
    </Section>
  )
}
