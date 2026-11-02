import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Lock } from "lucide-react"
import { toast } from "sonner"
import { AgentAvatar } from "@/components/common"
import { QueryState } from "@/components/QueryState"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"
import { api } from "@/lib/api"
import { useChatAgents } from "@/lib/hooks"
import type { WorkspaceSettings } from "./types"

// Workspace

const MODULE_LABEL = { chat: "Chat", agents: "Agents", credits: "Credits" }

export function Workspace() {
  const q = useQuery({ queryKey: KEY, queryFn: () => api.get<WorkspaceSettings>("/api/admin/settings") })
  return <QueryState query={q}>{(s) => <Settings s={s} />}</QueryState>
}

const KEY = ["admin", "settings"]
type Patch = Partial<WorkspaceSettings>

/** What a patch changes in the cached settings, so a switch moves before the server answers. */
const applied = (s: WorkspaceSettings, b: Patch): WorkspaceSettings => ({ ...s, ...b })

function Settings({ s }: { s: WorkspaceSettings }) {
  const { data: agents } = useChatAgents()
  const qc = useQueryClient()
  const save = useMutation({
    mutationFn: (b: Patch) => api.patch<WorkspaceSettings>("/api/admin/settings", b),
    onMutate: async (b) => {
      await qc.cancelQueries({ queryKey: KEY })
      const before = qc.getQueryData<WorkspaceSettings>(KEY)
      qc.setQueryData<WorkspaceSettings>(KEY, (cur) => cur && applied(cur, b))
      return { before }
    },
    onError: (_e, _b, ctx) => qc.setQueryData(KEY, ctx?.before),
    onSuccess: (saved) => { qc.setQueryData(KEY, saved); toast("Saved") },
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ["me"] })
      void qc.invalidateQueries({ queryKey: ["chat", "agents"] })
    },
  })
  return (
    <div className="mx-auto max-w-[720px] space-y-8">
      <Block title="Modules">
        {(Object.keys(MODULE_LABEL) as (keyof typeof MODULE_LABEL)[]).map((k) => (
          <Row key={k} label={MODULE_LABEL[k]}>
            <Switch checked={s.modules[k]} onCheckedChange={(v) => save.mutate({ modules: { ...s.modules, [k]: v } })} />
          </Row>
        ))}
        <Row label="Admin"><Lock className="size-4 text-faint" aria-label="Always on" /></Row>
      </Block>
      <Block title="Default agent">
        <Row label="New threads start with">
          <Select value={s.default_agent} onValueChange={(v) => save.mutate({ default_agent: v })}>
            <SelectTrigger size="sm" className="w-48"><SelectValue /></SelectTrigger>
            <SelectContent>
              {agents?.map((a) => <SelectItem key={a.id} value={a.id}><AgentAvatar src={a.avatar} name={a.name} className="size-4 rounded" />{a.name}</SelectItem>)}
            </SelectContent>
          </Select>
        </Row>
      </Block>
    </div>
  )
}

function Row({ children, label }: { children: React.ReactNode; label: string }) {
  return <div className="flex h-11 items-center gap-2.5 px-4 text-sm"><span className="flex-1">{label}</span>{children}</div>
}

function Block({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section>
      <h2 className="mb-2 text-sm font-semibold">{title}</h2>
      <div className="divide-y rounded-lg border">{children}</div>
    </section>
  )
}
