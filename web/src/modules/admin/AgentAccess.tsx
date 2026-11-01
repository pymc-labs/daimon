import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Lock } from "lucide-react"
import { AgentAvatar } from "@/components/common"
import { Drawer } from "@/components/Drawer"
import { QueryState } from "@/components/QueryState"
import { rowProps, useParam } from "@/components/filters"
import { Checkbox } from "@/components/ui/checkbox"
import { Label } from "@/components/ui/label"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { api } from "@/lib/api"
import { channelLabel } from "@/lib/format"
import { useGroups } from "@/lib/hooks"
import type { AgentAccess } from "./types"
import { PanelSection, Tag } from "./shared"

// Agent access

const WHO: Record<AgentAccess["who"], string> = { everyone: "Everyone", groups: "Groups", invites: "Invites only" }
const KEY = ["admin", "agent-access"]

/** Where an agent may be reached from the web, in a few words. */
function whoText(a: AgentAccess): string {
  if (a.fixed_to) return `Only in ${channelLabel(a.fixed_name)}`
  if (!a.web) return "Nobody"
  return a.who === "groups" ? (a.groups.join(", ") || "No groups") : WHO[a.who]
}

export function AgentAccessView() {
  const q = useQuery({ queryKey: KEY, queryFn: () => api.get<AgentAccess[]>("/api/admin/agent-access") })
  return <QueryState query={q} empty="No agents">{(data) => <AccessTable data={data} />}</QueryState>
}

function useSaveAccess() {
  const qc = useQueryClient()
  // The change shows at once; a refusal puts the old value back.
  return useMutation({
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
}

function AccessTable({ data }: { data: AgentAccess[] }) {
  const [, setAgent] = useParam("agent")
  const save = useSaveAccess()
  return (
    <>
      <Table>
        <TableHeader>
          <TableRow className="hover:bg-transparent">
            <TableHead>Agent</TableHead><TableHead className="w-28">Web chat</TableHead><TableHead>Who</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {data.map((a) => (
            <TableRow key={a.agent.id} {...rowProps(() => setAgent(a.agent.id))}>
              <TableCell>
                <span className="flex items-center gap-2.5">
                  <AgentAvatar src={a.agent.avatar} name={a.agent.name} />
                  <span className="font-medium">{a.agent.name}</span>
                  <span className="text-muted-foreground max-lg:hidden">{a.agent.model}</span>
                </span>
              </TableCell>
              <TableCell>
                {a.fixed_to ? <Lock className="size-4 text-faint" aria-label="Locked" /> : <span className="text-muted-foreground">{a.web ? "On" : "Off"}</span>}
              </TableCell>
              <TableCell className="max-w-64 truncate text-muted-foreground">{whoText(a)}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
      <Drawer param="agent" title="Agent access" ids={data.map((a) => a.agent.id)}>
        {(id) => {
          const a = data.find((r) => r.agent.id === id)
          return a ? <AccessBody a={a} save={(b) => save.mutate({ id, ...b })} /> : null
        }}
      </Drawer>
    </>
  )
}

function AccessBody({ a, save }: { a: AgentAccess; save: (b: Partial<AgentAccess>) => void }) {
  const { data: groups } = useGroups()
  // Everyone is the "Everyone" choice; a group is a named subset.
  const names = [...new Set([...(groups ?? []).filter((g) => g.id !== "everyone").map((g) => g.name), ...a.groups])]
  const options = Object.entries(WHO).filter(([v]) => v !== "groups" || names.length > 0 || a.who === "groups")
  return (
    <div>
      <div className="flex items-center gap-3 px-5 py-5">
        <AgentAvatar src={a.agent.avatar} name={a.agent.name} className="size-10 rounded-lg text-sm" />
        <div className="min-w-0">
          <div className="truncate text-base font-semibold">{a.agent.name}</div>
          <div className="truncate text-sm text-muted-foreground">{a.agent.model}</div>
        </div>
      </div>
      {a.fixed_to ? (
        <PanelSection title="Runs in">
          <div className="flex items-center gap-2 text-sm"><Lock className="size-4 text-faint" aria-label="Locked" />Only in {channelLabel(a.fixed_name)}</div>
        </PanelSection>
      ) : (
        <>
          <PanelSection title="Web chat">
            <Switch checked={a.web} onCheckedChange={(web) => save({ web })} aria-label={`Web chat for ${a.agent.name}`} />
          </PanelSection>
          <PanelSection title="Who">
            <Select value={a.who} onValueChange={(who) => save({ who: who as AgentAccess["who"] })} disabled={!a.web}>
              <SelectTrigger className="w-48"><SelectValue /></SelectTrigger>
              <SelectContent>{options.map(([v, l]) => <SelectItem key={v} value={v}>{l}</SelectItem>)}</SelectContent>
            </Select>
          </PanelSection>
          {a.web && a.who === "groups" && (
            <PanelSection title="Groups">
              {names.length ? (
                <div className="space-y-1">
                  {names.map((g) => (
                    <Label key={g} className="h-8 gap-2.5 font-normal">
                      <Checkbox
                        checked={a.groups.includes(g)}
                        onCheckedChange={(on) => save({ groups: on ? [...a.groups, g] : a.groups.filter((x) => x !== g) })}
                      />
                      {g}
                    </Label>
                  ))}
                </div>
              ) : <Tag>No groups</Tag>}
            </PanelSection>
          )}
        </>
      )}
    </div>
  )
}
