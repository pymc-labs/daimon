import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Lock } from "lucide-react"
import { toast } from "sonner"
import { AgentAvatar } from "@/components/common"
import { SettingsList } from "@/components/settings"
import { QueryState } from "@/components/QueryState"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"
import { api } from "@/lib/api"
import { useChatAgents } from "@/lib/hooks"
import { Access, Providers } from "./Access"
import type { SettingsPatch, WorkspaceSettings } from "./types"

// Workspace

const MODULE_LABEL = { chat: "Chat", agents: "Agents", channels: "Channels", routines: "Routines", credits: "Credits", integrations: "Integrations" }

export function Workspace() {
  const q = useQuery({ queryKey: KEY, queryFn: () => api.get<WorkspaceSettings>("/api/admin/settings") })
  return <QueryState query={q}>{(s) => <Settings s={s} />}</QueryState>
}

const KEY = ["admin", "settings"]

/** What a patch changes in the cached settings, so a switch moves before the server answers. */
const applied = (s: WorkspaceSettings, b: SettingsPatch): WorkspaceSettings => ({
  ...s,
  modules: b.modules ?? s.modules,
  default_agent: b.default_agent ?? s.default_agent,
  access: {
    ...s.access,
    ...(b.dm_enabled !== undefined && { dm_enabled: b.dm_enabled }),
    ...(b.dm_memory_read_only !== undefined && { dm_memory_read_only: b.dm_memory_read_only }),
    ...(b.plain_text_mentions !== undefined && { plain_text_mentions: b.plain_text_mentions }),
  },
})

function Settings({ s }: { s: WorkspaceSettings }) {
  const { data: agents } = useChatAgents()
  const qc = useQueryClient()
  const save = useMutation({
    mutationFn: (b: SettingsPatch) => api.patch<WorkspaceSettings>("/api/admin/settings", b),
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
    <div className="mx-auto max-w-3xl space-y-8">
      <SettingsList title="Modules" rows={[
        ...(Object.keys(MODULE_LABEL) as (keyof typeof MODULE_LABEL)[]).map((k) => ({
          key: k, label: MODULE_LABEL[k],
          control: <Switch aria-label={MODULE_LABEL[k]} checked={s.modules[k]} onCheckedChange={(v) => save.mutate({ modules: { ...s.modules, [k]: v } })} />,
        })),
        { key: "admin", label: "Admin", control: <Lock className="size-4 text-faint" aria-label="Always on" /> },
      ]} />
      <SettingsList title="Web chat" rows={[{
        label: "Web chat agent",
        control: (
          <Select value={s.default_agent} onValueChange={(v) => save.mutate({ default_agent: v })}>
            <SelectTrigger size="sm" className="w-48"><SelectValue /></SelectTrigger>
            <SelectContent>
              {agents?.map((a) => <SelectItem key={a.id} value={a.id}><AgentAvatar src={a.avatar} name={a.name} className="size-4 rounded" />{a.name}</SelectItem>)}
            </SelectContent>
          </Select>
        ),
      }]} />
      <Access access={s.access} save={save.mutate} />
      {s.providers && <Providers providers={s.providers} save={save.mutate} />}
    </div>
  )
}
