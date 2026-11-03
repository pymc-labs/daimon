import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { IdentityIcon } from "@/components/brand"
import { Ago, Avatar, CopyButton } from "@/components/common"
import { useConfirm } from "@/lib/confirm"
import { CreditMeter } from "@/components/CreditMeter"
import { Drawer } from "@/components/Drawer"
import { LoadMore, QueryState, Rows } from "@/components/QueryState"
import { Facet, FilterBar, SearchInput } from "@/components/filters"
import { rowProps, useListParam, useParam } from "@/lib/filters"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Link } from "react-router"
import { AgentLink } from "@/components/links"
import { PanelHead } from "@/components/panel"
import { useAgents } from "@/modules/agents/api"
import type { Allowance, SpendRow } from "@/modules/credits/types"
import { plural, usd } from "@/lib/format"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { api } from "@/lib/api"
import { useCan, usePaged } from "@/lib/hooks"
import type { Role } from "@/lib/types"
import { cn } from "@/lib/utils"
import type { AdminPerson } from "./types"
import { ROLE } from "./roles"
import { FilesRow } from "./FilesRow"
import { Facts, PanelSection as Section, Tag } from "./shared"

const usePeople = () => usePaged<AdminPerson>(["admin", "people"], "/api/admin/people")
// People

export function People() {
  const query = usePeople()
  const { data } = query
  const [q, setQ] = useParam("q")
  const [role, setRole] = useListParam("role")
  const [group, setGroup] = useListParam("group")
  const [, setPerson] = useParam("person")
  const rows = (data ?? []).filter((p) => (!q || `${p.name} ${p.email}`.toLowerCase().includes(q.toLowerCase())) && (!role.length || role.includes(p.role)) && (!group.length || p.groups.some((g) => group.includes(g))))
  const groups = [...new Set((data ?? []).flatMap((p) => p.groups))].sort()
  return (
    <>
      <FilterBar className="px-0 pt-0">
        <SearchInput value={q} onChange={(v) => setQ(v || null)} className="w-56" />
        <Facet label="Role" value={role} onChange={setRole} options={Object.entries(ROLE).map(([value, label]) => ({ value, label }))} />
        {groups.length > 0 && <Facet label="Group" value={group} onChange={setGroup} options={groups.map((g) => ({ value: g, label: g }))} />}
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
                  <span className="flex gap-1.5">{p.identities.map((i) => <IdentityIcon key={i.provider} identity={i} />)}</span>
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
  const can = useCan()
  const agents = useAgents()
  const allowances = useQuery({ queryKey: ["credits", "allowances"], queryFn: () => api.get<Allowance[]>("/api/credits/allowances"), enabled: can("credits.manage") })
  const spend = useQuery({
    queryKey: ["credits", "spend", "person", "this"], queryFn: () => api.get<SpendRow[]>("/api/credits/spend?by=person&period=this"),
    enabled: can("credits.manage"),
  })
  const owned = (agents.data ?? []).filter((a) => a.owner.id === p.id)
  const theirs = (allowances.data ?? []).filter((a) => a.subject.kind === "person" && a.subject.id === p.id)
  const spent = spend.data?.find((r) => r.id === p.id)
  return (
    <div>
      <PanelHead
        avatar={<Avatar name={p.name} className="size-10 text-sm" />}
        title={p.name}
        subtitle={p.email}
        tags={p.status === "suspended" ? <Tag>Suspended</Tag> : undefined}
        actions={p.status === "suspended" && <Button variant="outline" size="sm" onClick={() => save.mutate({ status: "active" })}>Restore</Button>}
        menu={p.role !== "owner" && p.status === "active" ? [{
          label: "Suspend", destructive: true,
          onSelect: async () => { if (await confirm({ title: `Suspend ${p.name}?`, body: "Their threads stop on every platform.", action: "Suspend", destructive: true })) save.mutate({ status: "suspended" }) },
        }] : undefined}
      />
      <Section>
        <Facts rows={[
          ["Role", (
            <Select key="role" value={p.role} onValueChange={(v) => save.mutate({ role: v as Role })} disabled={p.role === "owner"}>
              <SelectTrigger size="sm" className="w-40"><SelectValue /></SelectTrigger>
              <SelectContent>{Object.entries(ROLE).map(([v, l]) => <SelectItem key={v} value={v}>{l}</SelectItem>)}</SelectContent>
            </Select>
          )],
          ["Groups", p.groups.length ? <span key="groups" className="flex flex-wrap gap-1.5">{p.groups.map((g) => <Tag key={g}>{g}</Tag>)}</span> : "None"],
          ["Sign-in", (
            <span key="signin" className="flex flex-col gap-1">
              {p.identities.map((i) => (
                <span key={i.provider} className="flex items-center gap-2">
                  <IdentityIcon identity={i} />{i.provider_label}<span className="truncate text-muted-foreground">{i.label}</span>
                </span>
              ))}
            </span>
          )],
          ["Credit", p.credit ? <CreditMeter key="credit" meter={{ ...p.credit, window: p.role === "visitor" ? "total" : "month", resets_at: null }} /> : null],
          ["Last seen", <Ago key="seen" iso={p.last_seen} />],
        ]} />
        {p.role === "visitor" && p.invite_id && <FilesRow invite={p.invite_id} on={!!p.allow_files} />}
      </Section>
      {owned.length > 0 && (
        <Section title="Agents">
          <ul className="space-y-1.5 text-sm">{owned.map((a) => <li key={a.id}><AgentLink id={a.id} name={a.name} avatar={a.avatar} /></li>)}</ul>
        </Section>
      )}
      {theirs.length > 0 && (
        <Section title="Allowances">
          <ul className="space-y-1.5 text-sm">
            {theirs.map((a) => (
              <li key={a.id}>
                <Link to={`/credits/allowances?allowance=${a.id}`} className="hover:underline">
                  {usd(a.amount_usd)}{a.window === "month" ? " / month" : " total"}, {a.agent?.name ?? "any agent"}
                </Link>
              </li>
            ))}
          </ul>
        </Section>
      )}
      {spent && (
        <Section title="Spend this month">
          <Link to={`/credits/spend?period=this&q=${encodeURIComponent(p.name)}`} className="text-sm hover:underline">{usd(spent.usd)}, {plural(spent.turns, "reply", "replies")}</Link>
        </Section>
      )}
      {p.role === "visitor" && p.status === "active" && <SignInLink person={p} />}
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
