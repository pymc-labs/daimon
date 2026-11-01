import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Check, Lock, Plus } from "lucide-react"
import { AgentAvatar } from "@/components/common"
import { QueryState } from "@/components/QueryState"
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { api } from "@/lib/api"
import { useGroups } from "@/lib/hooks"
import type { AgentAccess } from "./types"
import { Tag } from "./shared"

// Agent access

const WHO: Record<AgentAccess["who"], string> = { everyone: "Everyone", groups: "Groups", invites: "Invites only" }

export function AgentAccessView() {
  const q = useQuery({ queryKey: ["admin", "agent-access"], queryFn: () => api.get<AgentAccess[]>("/api/admin/agent-access") })
  return <QueryState query={q} empty="No agents">{(data) => <AccessTable data={data} />}</QueryState>
}

const KEY = ["admin", "agent-access"]

function AccessTable({ data }: { data: AgentAccess[] }) {
  const qc = useQueryClient()
  // The switch flips at once; a refusal puts the old value back.
  const save = useMutation({
    mutationFn: ({ id, ...b }: Partial<AgentAccess> & { id: string }) => api.patch(`/api/admin/agent-access/${id}`, b),
    onMutate: async ({ id, ...b }) => {
      await qc.cancelQueries({ queryKey: KEY })
      const before = qc.getQueryData<AgentAccess[]>(KEY)
      qc.setQueryData<AgentAccess[]>(KEY, (rows) => rows?.map((r) => (r.agent.id === id ? { ...r, ...b } : r)))
      return { before }
    },
    onError: (_e, _v, ctx) => qc.setQueryData(KEY, ctx?.before),
    onSettled: () => { void qc.invalidateQueries({ queryKey: KEY }); void qc.invalidateQueries({ queryKey: ["chat", "agents"] }) },
  })
  return (
    <Table>
      <TableHeader>
        <TableRow className="hover:bg-transparent">
          <TableHead>Agent</TableHead><TableHead className="w-28">Web chat</TableHead><TableHead className="w-44">Who</TableHead><TableHead className="max-md:hidden">Groups</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {data.map((a) => (
          <TableRow key={a.agent.id}>
            <TableCell>
              <span className="flex items-center gap-2.5">
                <AgentAvatar src={a.agent.avatar} name={a.agent.name} />
                <span className="font-medium">{a.agent.name}</span>
                <span className="text-muted-foreground max-lg:hidden">{a.agent.model}</span>
              </span>
            </TableCell>
            <TableCell>
              {a.fixed_to ? <Lock className="size-4 text-faint" aria-label="Locked" />
                : <Switch checked={a.web} onCheckedChange={(web) => save.mutate({ id: a.agent.id, web })} aria-label={`Web chat for ${a.agent.name}`} />}
            </TableCell>
            <TableCell>
              {a.fixed_to ? <span className="text-sm text-muted-foreground">Only in {a.fixed_to}</span> : (
                <Select value={a.who} onValueChange={(who) => save.mutate({ id: a.agent.id, who: who as AgentAccess["who"] })} disabled={!a.web}>
                  <SelectTrigger size="sm" className="w-36"><SelectValue /></SelectTrigger>
                  <SelectContent>{Object.entries(WHO).map(([v, l]) => <SelectItem key={v} value={v}>{l}</SelectItem>)}</SelectContent>
                </Select>
              )}
            </TableCell>
            <TableCell className="max-md:hidden">
              {a.web && a.who === "groups" && <GroupPicker value={a.groups} onChange={(groups) => save.mutate({ id: a.agent.id, groups })} />}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

function GroupPicker({ value, onChange }: { value: string[]; onChange: (v: string[]) => void }) {
  const { data } = useGroups()
  // Everyone is the "Everyone" choice above; a group is a named subset.
  const groups = [...new Set([...(data ?? []).filter((g) => g.id !== "everyone").map((g) => g.name), ...value])]
  return (
    <span className="flex flex-wrap items-center gap-1.5">
      {value.map((g) => <Tag key={g}>{g}</Tag>)}
      <DropdownMenu>
        <DropdownMenuTrigger className="rounded p-0.5 text-muted-foreground hover:bg-accent" aria-label="Edit groups"><Plus className="size-3.5" /></DropdownMenuTrigger>
        <DropdownMenuContent align="start">
          {groups.map((g) => (
            <DropdownMenuItem key={g} onSelect={(e) => { e.preventDefault(); onChange(value.includes(g) ? value.filter((x) => x !== g) : [...value, g]) }}>
              {g}{value.includes(g) && <Check className="ml-auto" />}
            </DropdownMenuItem>
          ))}
        </DropdownMenuContent>
      </DropdownMenu>
    </span>
  )
}
