import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Lock } from "lucide-react"
import { toast } from "sonner"
import { AppIcon, BrandIcon } from "@/components/brand"
import { AgentAvatar } from "@/components/common"
import { QueryState } from "@/components/QueryState"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import { Switch } from "@/components/ui/switch"
import { api } from "@/lib/api"
import { useChatAgents } from "@/lib/hooks"
import { cn } from "@/lib/utils"
import type { AppStatuses, WorkspaceSettings } from "./types"

// Workspace

const MODULE_LABEL = { chat: "Chat", agents: "Agents", credits: "Credits" }

export function Workspace() {
  const q = useQuery({ queryKey: KEY, queryFn: () => api.get<WorkspaceSettings>("/api/admin/settings") })
  return <QueryState query={q}>{(s) => <Settings s={s} />}</QueryState>
}

const KEY = ["admin", "settings"]
type Patch = Partial<Omit<WorkspaceSettings, "signin">> & { signin?: Record<string, boolean> }

/** What a patch changes in the cached settings, so a switch moves before the server answers. */
const applied = (s: WorkspaceSettings, b: Patch): WorkspaceSettings => ({
  ...s, ...b, signin: s.signin.map((m) => (b.signin && m.id in b.signin ? { ...m, enabled: b.signin[m.id]! } : m)),
})

function Settings({ s }: { s: WorkspaceSettings }) {
  const { data: agents } = useChatAgents()
  // Probed apart from the settings: an app that is slow to answer must not hold them up.
  const status = useQuery({ queryKey: ["admin", "apps", "status"], queryFn: () => api.get<AppStatuses>("/api/admin/apps/status"), enabled: s.apps.length > 0 })
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
      <Block title="Sign-in">
        {s.signin.map((m) => (
          <Row key={m.id} label={m.label} icon={<BrandIcon brand={m.id} icon={m.icon} />}>
            <Switch checked={m.enabled} onCheckedChange={(v) => save.mutate({ signin: { [m.id]: v } })} />
          </Row>
        ))}
      </Block>
      <Block title="Default agent">
        <Row label="New chats start with">
          <Select value={s.default_agent} onValueChange={(v) => save.mutate({ default_agent: v })}>
            <SelectTrigger size="sm" className="w-48"><SelectValue /></SelectTrigger>
            <SelectContent>
              {agents?.map((a) => <SelectItem key={a.id} value={a.id}><AgentAvatar src={a.avatar} name={a.name} className="size-4 rounded" />{a.name}</SelectItem>)}
            </SelectContent>
          </Select>
        </Row>
      </Block>
      <Block title="Embedded apps">
        {s.apps.map((a) => (
          <Row key={a.key} label={a.name} icon={<AppIcon icon={a.icon} />}>
            <span className="font-mono text-xs text-muted-foreground">{a.url}</span>
            <AppStatus status={status.isError ? "error" : status.data?.[a.key]} />
          </Row>
        ))}
      </Block>
    </div>
  )
}

function AppStatus({ status }: { status?: AppStatuses[string] }) {
  if (!status) return <Skeleton className="h-4 w-16" />
  return (
    <span className={cn("flex items-center gap-1.5 text-xs", status === "connected" ? "text-success" : "text-destructive")}>
      <span className={cn("size-1.5 rounded-full", status === "connected" ? "bg-success" : "bg-destructive")} />
      {status === "connected" ? "Connected" : "Error"}
    </span>
  )
}

function Row({ children, label, icon }: { children: React.ReactNode; label: string; icon?: React.ReactNode }) {
  return <div className="flex h-11 items-center gap-2.5 px-4 text-sm">{icon}<span className="flex-1">{label}</span>{children}</div>
}

function Block({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section>
      <h2 className="mb-2 text-sm font-semibold">{title}</h2>
      <div className="divide-y rounded-lg border">{children}</div>
    </section>
  )
}
