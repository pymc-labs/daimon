import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { AgentAvatar } from "@/components/common"
import { SettingsList } from "@/components/settings"
import { QueryState } from "@/components/QueryState"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { api } from "@/lib/api"
import { useChatAgents } from "@/lib/hooks"
import { Access, Providers } from "./Access"
import type { SettingsPatch, WorkspaceSettings } from "./types"

// Workspace

export function Workspace() {
  const q = useQuery({ queryKey: KEY, queryFn: () => api.get<WorkspaceSettings>("/api/admin/settings") })
  return <QueryState query={q}>{(s) => <Settings s={s} />}</QueryState>
}

const KEY = ["admin", "settings"]

/** What a patch changes in the cached settings, so a switch moves before the server answers. */
const applied = (s: WorkspaceSettings, b: SettingsPatch): WorkspaceSettings => ({
  ...s,
  default_agent: b.default_agent ?? s.default_agent,
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
